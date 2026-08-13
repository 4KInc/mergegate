"""P2.1 — the webhook receiver, which is the only untrusted way into settlement.

The tests that matter here are the rejections. A receiver that accepts a forged
delivery hands an attacker the ability to announce a submission SHA, and every
downstream invariant is stated in terms of a SHA someone else chose.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest

from mergegate.mandate import PaymentMandate
from mergegate.settlement import Outcome, TaskState, TaskStateMachine
from mergegate.webhook import (
    GitHubPush,
    SignatureError,
    WebhookError,
    WebhookReceiver,
    parse_push,
    verify_signature,
)

SECRET = "a-shared-secret"
REPO = "4KInc/mergegate-demo-task"
CONTRACT_HASH = "sha256:" + "c" * 64
SHA_A = "1" * 40
SHA_B = "2" * 40


def _mandate() -> PaymentMandate:
    return PaymentMandate(
        task_id="task-001",
        contract_hash=CONTRACT_HASH,
        buyer_agent="0xBUYER",
        provider_agent="0xPROVIDER",
        amount_usdc="250.00",
        asset="USDC",
        chain="base",
        deadline=datetime.now(UTC) + timedelta(hours=6),
        nonce="nonce-1",
    )


def _machine() -> TaskStateMachine:
    return TaskStateMachine(task_id="task-001", contract_hash=CONTRACT_HASH, mandate=_mandate())


def _push_body(head_sha: str = SHA_A, *, repo: str = REPO, **overrides: object) -> bytes:
    payload: dict[str, object] = {
        "ref": "refs/heads/submission",
        "before": "0" * 40,
        "after": head_sha,
        "deleted": False,
        "forced": False,
        "repository": {"full_name": repo},
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def _headers(body: bytes, *, event: str = "push", delivery: str = "d-1") -> dict[str, str]:
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {
        "x-hub-signature-256": f"sha256={digest}",
        "x-github-event": event,
        "x-github-delivery": delivery,
    }


def _receiver(machine: TaskStateMachine | None) -> WebhookReceiver:
    return WebhookReceiver(secret=SECRET, repository=REPO, resolve=lambda push: machine)


# -- signature ----------------------------------------------------------------


def test_valid_signature_passes() -> None:
    body = b'{"hello": "world"}'
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    verify_signature(secret=SECRET, body=body, header=f"sha256={digest}")


def test_signature_over_reserialized_body_fails() -> None:
    """The exact reason verification must run on raw bytes.

    Same JSON, different serialization. A receiver that parsed first and
    re-encoded would compare against these bytes and reject real deliveries.
    """
    body = b'{"a": 1, "b": 2}'
    reserialized = json.dumps(json.loads(body), separators=(",", ":")).encode()
    assert body != reserialized
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()

    with pytest.raises(SignatureError):
        verify_signature(secret=SECRET, body=reserialized, header=f"sha256={digest}")


@pytest.mark.parametrize(
    "header",
    [None, "", "deadbeef", "sha1=deadbeef", "sha256=", "sha256=" + "f" * 64],
    ids=["missing", "empty", "bare-hex", "wrong-algo", "empty-digest", "wrong-digest"],
)
def test_bad_signatures_rejected(header: str | None) -> None:
    with pytest.raises(SignatureError):
        verify_signature(secret=SECRET, body=b"{}", header=header)


def test_empty_secret_fails_closed() -> None:
    """An unconfigured secret must not mean 'accept anything'."""
    body = b"{}"
    digest = hmac.new(b"", body, hashlib.sha256).hexdigest()
    with pytest.raises(SignatureError, match="no webhook secret"):
        verify_signature(secret="", body=body, header=f"sha256={digest}")


def test_tampered_body_rejected() -> None:
    """Signature is bound to the SHA, so an attacker cannot swap in their own."""
    body = _push_body(SHA_A)
    headers = _headers(body)
    forged = body.replace(SHA_A.encode(), SHA_B.encode())

    with pytest.raises(SignatureError):
        _receiver(_machine()).handle(body=forged, headers=headers)


# -- framing ------------------------------------------------------------------


def test_missing_delivery_id_rejected() -> None:
    """Dedup is keyed on the delivery ID; without it redelivery is undetectable."""
    body = _push_body()
    headers = _headers(body)
    del headers["x-github-delivery"]

    with pytest.raises(WebhookError, match="X-GitHub-Delivery"):
        _receiver(_machine()).handle(body=body, headers=headers)


def test_headers_are_case_insensitive() -> None:
    body = _push_body()
    headers = {key.upper(): value for key, value in _headers(body).items()}
    result = _receiver(_machine()).handle(body=body, headers=headers)
    assert result.disposition.value == "processed"


def test_malformed_json_rejected_after_signature() -> None:
    body = b"not json"
    with pytest.raises(WebhookError, match="not valid JSON"):
        _receiver(_machine()).handle(body=body, headers=_headers(body))


def test_ping_acknowledged_without_touching_state() -> None:
    machine = _machine()
    body = b"{}"
    result = _receiver(machine).handle(body=body, headers=_headers(body, event="ping"))

    assert result.disposition.value == "ignored"
    assert machine.state is TaskState.FUNDED


def test_unhandled_event_ignored() -> None:
    body = b"{}"
    result = _receiver(_machine()).handle(body=body, headers=_headers(body, event="issue_comment"))
    assert result.disposition.value == "ignored"


# -- routing ------------------------------------------------------------------


def test_push_from_another_repository_rejected() -> None:
    body = _push_body(repo="attacker/lookalike")
    with pytest.raises(WebhookError, match="this receiver serves"):
        _receiver(_machine()).handle(body=body, headers=_headers(body))


def test_branch_deletion_ignored() -> None:
    machine = _machine()
    body = _push_body("0" * 40, deleted=True)
    result = _receiver(machine).handle(body=body, headers=_headers(body))

    assert result.disposition.value == "ignored"
    assert machine.state is TaskState.FUNDED


def test_tag_push_ignored() -> None:
    body = _push_body(ref="refs/tags/v1")
    result = _receiver(_machine()).handle(body=body, headers=_headers(body))
    assert result.disposition.value == "ignored"


def test_untracked_push_ignored() -> None:
    """The demo repo sees unrelated commits; those are not an error."""
    body = _push_body()
    result = _receiver(None).handle(body=body, headers=_headers(body))
    assert result.disposition.value == "ignored"


# -- state machine hand-off ---------------------------------------------------


def test_push_advances_the_state_machine() -> None:
    machine = _machine()
    body = _push_body(SHA_A)
    result = _receiver(machine).handle(body=body, headers=_headers(body))

    assert result.outcome is not None
    assert result.outcome.outcome is Outcome.APPLIED
    assert machine.state is TaskState.SUBMITTED
    assert machine.eligible_sha == SHA_A


def test_redelivery_is_deduplicated_not_reapplied() -> None:
    """GitHub redelivers. The same delivery ID must not count twice."""
    machine = _machine()
    receiver = _receiver(machine)
    body = _push_body(SHA_A)
    headers = _headers(body, delivery="d-42")

    first = receiver.handle(body=body, headers=headers)
    second = receiver.handle(body=body, headers=headers)

    assert first.outcome is not None and first.outcome.outcome is Outcome.APPLIED
    assert second.outcome is not None and second.outcome.outcome is Outcome.DUPLICATE
    assert machine.eligible_sha == SHA_A


def test_force_push_supersedes_the_previous_sha() -> None:
    """P0.4 through the transport: a new head SHA invalidates the old artifact."""
    machine = _machine()
    receiver = _receiver(machine)

    first = _push_body(SHA_A)
    receiver.handle(body=first, headers=_headers(first, delivery="d-1"))

    second = _push_body(SHA_B, forced=True, before=SHA_A)
    result = receiver.handle(body=second, headers=_headers(second, delivery="d-2"))

    assert result.outcome is not None and result.outcome.outcome is Outcome.APPLIED
    assert machine.eligible_sha == SHA_B
    assert "supersedes" in result.detail


def test_push_after_terminal_state_is_rejected_by_the_state_machine() -> None:
    machine = _machine()
    machine.state = TaskState.SETTLED
    body = _push_body(SHA_B)

    result = _receiver(machine).handle(body=body, headers=_headers(body))

    assert result.outcome is not None
    assert result.outcome.outcome is Outcome.REJECTED
    assert machine.state is TaskState.SETTLED


# -- payload projection -------------------------------------------------------


def test_parse_push_extracts_branch() -> None:
    push = parse_push(json.loads(_push_body()))
    assert push == GitHubPush(
        repository=REPO,
        ref="refs/heads/submission",
        head_sha=SHA_A,
        before_sha="0" * 40,
        deleted=False,
        forced=False,
    )
    assert push.branch == "submission"


def test_parse_push_requires_repository_and_sha() -> None:
    with pytest.raises(WebhookError, match="names no repository"):
        parse_push({"after": SHA_A})
    with pytest.raises(WebhookError, match="no 'after' SHA"):
        parse_push({"repository": {"full_name": REPO}})
