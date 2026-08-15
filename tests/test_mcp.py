"""The MCP server, tested at the protocol boundary.

The dispatch is a pure function over dicts, so these drive real JSON-RPC
messages rather than mocking a client library. That catches the things that
actually break MCP integrations: answering a notification, dropping the id,
or a tool schema that no longer matches what the tool reads.

Network calls are stubbed at ``_get``. What is being tested is the protocol and
the tool logic, not urllib.
"""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mergegate import mcp


def _public_b64(private: Ed25519PrivateKey) -> str:
    raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _result(response: dict[str, Any]) -> Any:
    """Unwrap a tools/call result back into the payload the tool returned."""
    return json.loads(response["result"]["content"][0]["text"])


# -- protocol -----------------------------------------------------------------


def test_initialize_echoes_the_clients_protocol_version() -> None:
    """A client that asks for a version it can speak must not be told a
    different one, or it will negotiate down for no reason."""
    response = mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
    )
    assert response is not None
    assert response["result"]["protocolVersion"] == "2024-11-05"
    assert response["result"]["serverInfo"]["name"] == "mergegate"


def test_notifications_get_no_reply() -> None:
    """Replying to a notification is a protocol violation, and some clients
    treat the stray response as fatal."""
    assert mcp.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_returns_a_jsonrpc_error() -> None:
    response = mcp.handle_message({"jsonrpc": "2.0", "id": 7, "method": "resources/list"})
    assert response is not None
    assert response["error"]["code"] == -32601
    assert response["id"] == 7


def test_every_advertised_tool_is_implemented() -> None:
    """A tool in tools/list with no implementation is an error the model only
    discovers by calling it."""
    for tool in mcp.TOOLS:
        assert tool["name"] in mcp._IMPLEMENTATIONS
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"].strip()


def test_no_tool_can_move_money() -> None:
    """The deliberate scope limit. An MCP server is driven by whatever the
    model decides to call, so a funding tool here would be a wallet-draining
    primitive reachable by prompt injection."""
    forbidden = ("fund", "pay", "release", "refund", "transfer", "settle", "mandate", "sign")
    for tool in mcp.TOOLS:
        assert not any(word in tool["name"] for word in forbidden), tool["name"]


def test_serve_reads_newline_delimited_messages() -> None:
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        + "\n\n"  # blank lines are skipped, not fatal
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        + "\n"
    )
    stdout = io.StringIO()

    mcp.serve(stdin=stdin, stdout=stdout)

    lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert [m["id"] for m in lines] == [1, 2]  # the notification produced nothing


def test_garbage_input_does_not_kill_the_server() -> None:
    stdin = io.StringIO("{not json\n" + json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}))
    stdout = io.StringIO()

    mcp.serve(stdin=stdin, stdout=stdout)

    assert json.loads(stdout.getvalue().strip())["id"] == 3


# -- tools --------------------------------------------------------------------


def test_list_receipts_filters_by_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp,
        "_get",
        lambda url, **kw: {
            "receipts": [
                {"id": "a", "task_id": "owner/one"},
                {"id": "b", "task_id": "owner/two"},
            ],
            "source_error": "",
        },
    )
    response = mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "mergegate_list_receipts",
                "arguments": {"task_id": "owner/two"},
            },
        }
    )
    assert response is not None
    payload = _result(response)
    assert payload["count"] == 1
    assert payload["receipts"][0]["id"] == "b"


def test_list_receipts_surfaces_a_datastore_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent waiting to be paid must be able to tell 'not paid yet' from
    'the datastore is unreachable'. Collapsing both to an empty list would make
    it conclude it was never paid."""
    monkeypatch.setattr(
        mcp, "_get", lambda url, **kw: {"receipts": [], "source_error": "ServiceUnavailable: 503"}
    )
    response = mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mergegate_list_receipts", "arguments": {}},
        }
    )
    assert response is not None
    assert "503" in _result(response)["source_error"]


def test_verify_receipt_checks_a_real_receipt(
    signed_receipt: tuple[dict[str, Any], Ed25519PrivateKey], monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope, private = signed_receipt
    monkeypatch.setenv(mcp.PUBLIC_KEY_VAR, _public_b64(private))

    response = mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mergegate_verify_receipt", "arguments": {"envelope": envelope}},
        }
    )
    assert response is not None
    payload = _result(response)
    assert payload["verified"] is True
    assert payload["checks_passed"] == payload["checks_total"]


def test_verify_receipt_catches_tampering(
    signed_receipt: tuple[dict[str, Any], Ed25519PrivateKey], monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope, private = signed_receipt
    envelope["body"]["binding"]["settlement_recipient"] = "0xATTACKER"
    monkeypatch.setenv(mcp.PUBLIC_KEY_VAR, _public_b64(private))

    response = mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mergegate_verify_receipt", "arguments": {"envelope": envelope}},
        }
    )
    assert response is not None
    assert _result(response)["verified"] is False


def test_verify_without_a_pinned_key_refuses_rather_than_fetching(
    signed_receipt: tuple[dict[str, Any], Ed25519PrivateKey], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The honesty test. Fetching the key from the service being checked would
    make every forged receipt verify, so an unset key must produce 'cannot
    verify' and never a green result."""
    envelope, _ = signed_receipt
    monkeypatch.delenv(mcp.PUBLIC_KEY_VAR, raising=False)

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not reach the network for a key")

    monkeypatch.setattr(mcp, "_get", explode)

    response = mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mergegate_verify_receipt", "arguments": {"envelope": envelope}},
        }
    )
    assert response is not None
    payload = _result(response)
    assert payload["verified"] is False
    assert "no pinned key" in payload["reason"]


def test_tool_errors_come_back_as_results_not_transport_errors() -> None:
    """The model has to see the mistake to correct it; a JSON-RPC error is
    handled by the client and never reaches the model as tool output."""
    response = mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mergegate_verify_receipt", "arguments": {}},
        }
    )
    assert response is not None
    assert "error" not in response
    assert response["result"]["isError"] is True


def test_unknown_tool_is_reported_to_the_model() -> None:
    response = mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mergegate_drain_wallet", "arguments": {}},
        }
    )
    assert response is not None
    assert response["result"]["isError"] is True
