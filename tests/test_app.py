"""HTTP-layer tests for the deployed service.

These exist because of a bug the unit tests could not have caught.
``tests/test_webhook.py`` calls :class:`~mergegate.webhook.WebhookReceiver`
directly, so it never crossed the FastAPI boundary — and the endpoint was
entirely broken there: ``Request`` was imported inside ``build_router`` while
the module uses postponed annotations, so FastAPI could not resolve the
annotation, treated ``request`` as a query parameter, and returned 422 for
every delivery without ever running signature verification.

It failed closed, so nothing was mis-authenticated. But a webhook that rejects
GitHub is a webhook that never fires, and only a request through the app would
have shown it. Anything that can break between the handler and the transport
gets asserted here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mergegate.app import create_app
from mergegate.settlement import TaskState
from mergegate.store import MemoryTaskStore
from mergegate.webhook import GitHubPush, WebhookReceiver

from .test_store import _machine  # the same funded task fixture

SECRET = "test-webhook-secret"
REPO = "4KInc/mergegate-demo-task"
SHA = "1" * 40


def _client(store: MemoryTaskStore | None = None) -> TestClient:
    store = store or MemoryTaskStore()

    def resolve(push: GitHubPush) -> Any:
        return store.get(push.repository)

    receiver = WebhookReceiver(secret=SECRET, repository=REPO, resolve=resolve)
    return TestClient(create_app(receiver=receiver))


def _push_body(sha: str = SHA, repo: str = REPO) -> bytes:
    return json.dumps(
        {
            "repository": {"full_name": repo},
            "after": sha,
            "before": "0" * 40,
            "ref": "refs/heads/main",
            "deleted": False,
            "forced": False,
        }
    ).encode()


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_health() -> None:
    assert _client().get("/health").json() == {"status": "ok"}


def test_status_states_scope_and_custody() -> None:
    """The service's own description must carry the same limits the receipts do."""
    body = _client().get("/api/status").json()
    assert "not code quality, security, or mergeworthiness" in body["scope"]
    assert "escrow authority" in body["custody"]
    assert "non-custodial" not in json.dumps(body).lower()


# -- the regression that motivated this file ---------------------------------


def test_endpoint_actually_runs_the_handler() -> None:
    """A 422 here means FastAPI never reached the handler — the original bug."""
    r = _client().post(
        "/webhooks/github",
        content=_push_body(),
        headers={"X-GitHub-Event": "push", "X-GitHub-Delivery": "d1"},
    )
    assert r.status_code != 422, "FastAPI did not route the request body to the handler"
    assert r.status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {"X-GitHub-Event": "push", "X-GitHub-Delivery": "d1"},
        {"X-GitHub-Event": "push", "X-GitHub-Delivery": "d1", "X-Hub-Signature-256": "sha256=bad"},
        {"X-GitHub-Event": "push", "X-GitHub-Delivery": "d1", "X-Hub-Signature-256": "nonsense"},
    ],
)
def test_unauthenticated_deliveries_are_rejected(headers: dict[str, str]) -> None:
    r = _client().post("/webhooks/github", content=_push_body(), headers=headers)
    assert r.status_code == 401


def test_rejection_body_is_not_an_oracle() -> None:
    """A missing signature and a wrong one must look identical to the caller."""
    client = _client()
    missing = client.post(
        "/webhooks/github",
        content=_push_body(),
        headers={"X-GitHub-Event": "push", "X-GitHub-Delivery": "d1"},
    )
    wrong = client.post(
        "/webhooks/github",
        content=_push_body(),
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "d2",
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
        },
    )
    assert missing.status_code == wrong.status_code == 401
    assert missing.json() == wrong.json() == {"error": "rejected"}


def test_signature_is_checked_against_raw_bytes() -> None:
    """Re-serializing before verifying would compare against bytes GitHub never
    sent. Same JSON, different byte order — the signature must still hold."""
    body = (
        b'{"repository": {"full_name": "' + REPO.encode() + b'"}, '
        b'"after": "' + SHA.encode() + b'", "ref": "refs/heads/main"}'
    )
    r = _client().post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "d1",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert r.status_code == 202


def test_authentic_push_reaches_the_state_machine() -> None:
    store = MemoryTaskStore()
    machine = _machine()
    machine.task_id = REPO
    store.put(machine)

    body = _push_body()
    r = _client(store).post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "d1",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert r.status_code == 202
    assert r.json()["disposition"] == "processed"
    assert store.get(REPO).state is TaskState.SUBMITTED  # type: ignore[union-attr]


def test_redelivery_is_deduplicated_over_http() -> None:
    """GitHub redelivers. Twice through the transport must settle one event."""
    store = MemoryTaskStore()
    machine = _machine()
    machine.task_id = REPO
    store.put(machine)
    client = _client(store)

    body = _push_body()
    headers = {
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": "same-delivery",
        "X-Hub-Signature-256": _sign(body),
    }
    first = client.post("/webhooks/github", content=body, headers=headers)
    second = client.post("/webhooks/github", content=body, headers=headers)

    assert first.json()["outcome"] == "applied"
    assert second.json()["outcome"] == "duplicate"


def test_ping_is_acknowledged() -> None:
    """GitHub sends ping when the hook is created; rejecting it shows a red X."""
    body = b'{"zen": "hello"}'
    r = _client().post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": "d1",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert r.status_code == 202
    assert r.json()["disposition"] == "ignored"


def test_delivery_for_another_repository_is_refused() -> None:
    body = _push_body(repo="someone/else")
    r = _client().post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "d1",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert r.status_code == 400
