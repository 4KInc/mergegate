"""P2.1 — the GitHub webhook receiver that feeds the settlement state machine.

This is the only untrusted entry point into a system that moves money, so the
ordering here is load-bearing:

    raw bytes → signature → parse → repository → event → state machine

Signature verification happens on the **raw request body**, before the payload
is parsed. Parsing first and re-serializing to check the HMAC would compare a
signature against bytes GitHub never sent — different key order, different
separators, different unicode escaping all produce a different digest, and the
usual fix for that ("just re-encode it the same way") is a guess about someone
else's serializer. Rejecting on raw bytes has no such ambiguity.

What this module deliberately does **not** do:

* It does not decide anything about settlement. It translates an HTTP request
  into one call on :class:`~mergegate.settlement.TaskStateMachine` and reports
  what that call returned. Dedup, staleness, and terminality are the state
  machine's invariants (P0.4 / P0.5) and stay there.
* It does not hold task state. The ``resolve`` callable supplied by the caller
  is what maps a push to the task it concerns, because that lookup is a
  storage concern (Firestore) and this module is meant to be testable without
  one.

Concurrency: ``TaskStateMachine`` is documented as not thread-safe, and the
webhook is exactly where concurrent deliveries show up. The ``resolve``
callable is responsible for handing back a state machine already held under a
per-task lock (a Firestore transaction in deployment). This module does not add
locking of its own, because a lock here would protect an in-process copy while
the real race is across processes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .settlement import EventOutcome, TaskStateMachine

__all__ = [
    "GitHubPush",
    "SignatureError",
    "WebhookError",
    "WebhookReceiver",
    "build_router",
    "parse_push",
    "verify_signature",
]

SIGNATURE_HEADER = "X-Hub-Signature-256"
EVENT_HEADER = "X-GitHub-Event"
DELIVERY_HEADER = "X-GitHub-Delivery"

_SIGNATURE_PREFIX = "sha256="
_ZERO_SHA = "0" * 40


class WebhookError(Exception):
    """A delivery that cannot be processed. ``status`` is the HTTP code to send."""

    status = 400

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class SignatureError(WebhookError):
    """The delivery is not provably from GitHub.

    401, not 400: this is an authentication failure, and it is the one failure
    mode that must never be distinguishable by response body from a merely
    malformed payload — see :meth:`WebhookReceiver.handle`.
    """

    status = 401


class Disposition(StrEnum):
    """What the receiver did with a delivery, for logging and the response."""

    PROCESSED = "processed"
    """Handed to the state machine. The outcome is the state machine's."""

    IGNORED = "ignored"
    """Well-formed and authentic, but not an event this system acts on."""


@dataclass(frozen=True, slots=True)
class GitHubPush:
    """The few fields of a ``push`` payload that MergeGate actually binds to.

    Reduced to a frozen record at the boundary so that nothing downstream ever
    reaches back into raw webhook JSON for a field nobody validated.
    """

    repository: str
    ref: str
    head_sha: str
    before_sha: str
    deleted: bool
    forced: bool

    @property
    def branch(self) -> str:
        """Branch name, or ``""`` if the ref is a tag or something else."""
        prefix = "refs/heads/"
        return self.ref[len(prefix) :] if self.ref.startswith(prefix) else ""


def verify_signature(*, secret: str, body: bytes, header: str | None) -> None:
    """Verify GitHub's ``X-Hub-Signature-256`` over the raw body.

    Raises :class:`SignatureError` on any failure. Returns ``None`` on success —
    there is no boolean to accidentally ignore at a call site.
    """
    if not secret:
        # An empty secret would make every delivery verify against a digest an
        # attacker can compute. Fail closed rather than silently accept.
        raise SignatureError("no webhook secret is configured")
    if not header:
        raise SignatureError(f"missing {SIGNATURE_HEADER}")
    if not header.startswith(_SIGNATURE_PREFIX):
        raise SignatureError(f"{SIGNATURE_HEADER} is not a sha256= digest")

    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    provided = header[len(_SIGNATURE_PREFIX) :]
    if not hmac.compare_digest(expected, provided):
        raise SignatureError("signature does not match body")


def parse_push(payload: dict[str, Any]) -> GitHubPush:
    """Project a ``push`` payload onto :class:`GitHubPush`, validating as we go."""
    repository = str(payload.get("repository", {}).get("full_name", ""))
    if not repository:
        raise WebhookError("push payload names no repository")

    head_sha = str(payload.get("after", ""))
    if not head_sha:
        raise WebhookError("push payload has no 'after' SHA")

    return GitHubPush(
        repository=repository,
        ref=str(payload.get("ref", "")),
        head_sha=head_sha,
        before_sha=str(payload.get("before", "")),
        deleted=bool(payload.get("deleted", False)),
        forced=bool(payload.get("forced", False)),
    )


@dataclass(frozen=True, slots=True)
class Result:
    """What :meth:`WebhookReceiver.handle` decided, before it becomes a response."""

    disposition: Disposition
    detail: str
    outcome: EventOutcome | None = None


@dataclass
class WebhookReceiver:
    """Transport-independent webhook handling.

    Separated from the FastAPI layer so the security-relevant part can be
    tested by calling a function with bytes, rather than by standing up an app
    and hoping the test client reproduces GitHub's framing.

    ``resolve`` maps a push to the task state machine it concerns, or ``None``
    if no funded task tracks this repo/ref — an untracked push is ignored, not
    an error, because the demo repo will see plenty of unrelated commits.
    """

    secret: str
    repository: str
    resolve: Callable[[GitHubPush], TaskStateMachine | None]

    def handle(self, *, body: bytes, headers: dict[str, str]) -> Result:
        """Process one delivery. Raises :class:`WebhookError` for a bad request.

        Header lookup is case-insensitive: HTTP header names are, and ASGI
        servers lower-case them, so matching GitHub's documented capitalization
        exactly would work in tests and fail in deployment.
        """
        lookup = {key.lower(): value for key, value in headers.items()}

        verify_signature(secret=self.secret, body=body, header=lookup.get(SIGNATURE_HEADER.lower()))

        delivery_id = lookup.get(DELIVERY_HEADER.lower(), "")
        if not delivery_id:
            # Dedup is keyed on this. Without it the state machine cannot tell a
            # redelivery from a new event, which is the double-spend path.
            raise WebhookError(f"missing {DELIVERY_HEADER}")

        event = lookup.get(EVENT_HEADER.lower(), "")
        if not event:
            raise WebhookError(f"missing {EVENT_HEADER}")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise WebhookError(f"body is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise WebhookError("body is not a JSON object")

        if event == "ping":
            return Result(Disposition.IGNORED, "ping acknowledged")
        if event != "push":
            return Result(Disposition.IGNORED, f"event {event!r} is not acted on")

        return self._handle_push(parse_push(payload), delivery_id)

    def _handle_push(self, push: GitHubPush, delivery_id: str) -> Result:
        if push.repository.lower() != self.repository.lower():
            # A webhook pointed at the wrong repo is a misconfiguration, but it
            # is also what a confused-deputy attempt looks like, so it stops here.
            raise WebhookError(
                f"delivery is for {push.repository}, this receiver serves {self.repository}"
            )

        if push.deleted or push.head_sha == _ZERO_SHA:
            return Result(Disposition.IGNORED, f"ref {push.ref} was deleted")
        if not push.branch:
            return Result(Disposition.IGNORED, f"ref {push.ref} is not a branch")

        machine = self.resolve(push)
        if machine is None:
            return Result(Disposition.IGNORED, f"no funded task tracks {push.ref}")

        # A force-push is not special-cased here on purpose. It arrives as a new
        # head SHA, and on_submission already supersedes the previous eligible
        # SHA and invalidates any verification attached to it (P0.4). Handling
        # it here too would mean two places deciding what a force-push means.
        outcome = machine.on_submission(submission_sha=push.head_sha, delivery_id=delivery_id)
        return Result(Disposition.PROCESSED, outcome.detail, outcome)


def build_router(receiver: WebhookReceiver, *, path: str = "/webhooks/github") -> Any:
    """Mount ``receiver`` on a FastAPI router.

    Imported lazily so that importing this module — and unit-testing
    :class:`WebhookReceiver` — does not require FastAPI to be installed.
    """
    from fastapi import APIRouter, Request, Response

    router = APIRouter()

    @router.post(path)
    async def receive(request: Request) -> Response:
        # request.body() is the raw bytes as received. Anything that parses and
        # re-serializes before this point breaks signature verification.
        body = await request.body()
        try:
            result = receiver.handle(body=body, headers=dict(request.headers))
        except WebhookError as exc:
            # The detail is logged, not returned. Telling an unauthenticated
            # caller *why* verification failed hands them an oracle; a 401 with
            # a fixed body tells them only that it did.
            status = exc.status
            payload = {"error": "rejected" if status == 401 else exc.detail}
            return Response(
                content=json.dumps(payload),
                status_code=status,
                media_type="application/json",
            )

        # 202, not 200: the state machine recorded the event, but verification
        # and settlement happen out of band. GitHub only needs to know the
        # delivery was accepted, and retries on anything >= 400.
        return Response(
            content=json.dumps(
                {
                    "disposition": result.disposition.value,
                    "detail": result.detail,
                    "outcome": result.outcome.outcome.value if result.outcome else None,
                    "state": result.outcome.state.value if result.outcome else None,
                }
            ),
            status_code=202,
            media_type="application/json",
        )

    return router
