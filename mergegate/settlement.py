"""P0.4 / P0.5: artifact binding and the idempotent settlement state machine.

GitHub redelivers webhooks and delivers events out of order. Without
idempotency that means double-releasing real money, so the invariant is stated
narrowly and enforced structurally:

    one task contract → one eligible submission SHA → one verification result
    → one terminal settlement action

Three independent defenses, because any one of them can be wrong:

1. **Delivery dedup.** A repeated webhook delivery ID is ignored outright.
2. **Artifact binding (P0.4).** Every event names the ``submission_sha`` it
   concerns. An event about a SHA that is not the currently eligible one is
   stale and rejected: this is what stops a force-push from inheriting a
   previous SHA's PASS.
3. **Settlement key.** ``sha256(task_id || submission_sha || contract_hash ||
   terminal_verdict)`` is recorded when settlement executes. A second attempt
   producing the same key is refused even if it arrives through a different
   event with a different delivery ID.

Terminal means terminal. Once ``SETTLED`` or ``REFUNDED``, every later event is
rejected: including a new submission. Re-running a task after a terminal
verdict requires a new contract and new funding, because the buyer's mandate
authorized exactly one payment decision.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .mandate import PaymentMandate, SettlementAction, SettlementDirective, execute_mandate

__all__ = [
    "EventOutcome",
    "Outcome",
    "SettlementError",
    "TaskState",
    "TaskStateMachine",
    "settlement_key",
]


class TaskState(StrEnum):
    FUNDED = "FUNDED"
    SUBMITTED = "SUBMITTED"
    VERIFYING = "VERIFYING"
    VERIFIED_PASS = "PASS"
    VERIFIED_FAIL = "FAIL"
    SETTLED = "SETTLED"
    REFUNDED = "REFUNDED"

    @property
    def is_terminal(self) -> bool:
        return self in (TaskState.SETTLED, TaskState.REFUNDED)


class Outcome(StrEnum):
    APPLIED = "applied"
    """The event advanced the state machine."""

    DUPLICATE = "duplicate"
    """Already seen. Ignored, not an error: redelivery is normal."""

    STALE = "stale"
    """Concerns a submission SHA that is no longer eligible."""

    REJECTED = "rejected"
    """Not permitted from the current state."""


class SettlementError(RuntimeError):
    """A settlement was attempted that the state machine cannot honour."""


@dataclass(frozen=True, slots=True)
class EventOutcome:
    outcome: Outcome
    state: TaskState
    detail: str = ""
    directive: SettlementDirective | None = None

    @property
    def applied(self) -> bool:
        return self.outcome is Outcome.APPLIED


def settlement_key(
    *, task_id: str, submission_sha: str, contract_hash: str, terminal_verdict: str
) -> str:
    """The idempotency key for one terminal settlement action.

    Components are length-prefixed rather than concatenated directly. Plain
    concatenation lets two different field splits produce identical bytes, which
    would collapse two distinct settlements onto one key.
    """
    parts = (task_id, submission_sha, contract_hash, terminal_verdict)
    payload = b"".join(len(p.encode()).to_bytes(4, "big") + p.encode() for p in parts)
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass
class TaskStateMachine:
    """Settlement state for exactly one funded task contract.

    Not thread-safe on its own. In deployment each task's events are processed
    under a per-task lock (Firestore transaction); the invariants here are what
    that lock protects, not a substitute for it.
    """

    task_id: str
    contract_hash: str
    mandate: PaymentMandate
    state: TaskState = TaskState.FUNDED
    eligible_sha: str = ""
    verified_sha: str = ""
    settlement_tx: str = ""
    settlement_key_used: str = ""
    _seen_deliveries: set[str] = field(default_factory=set)
    _superseded: list[str] = field(default_factory=list)

    # -- events ---------------------------------------------------------------

    def on_submission(self, *, submission_sha: str, delivery_id: str) -> EventOutcome:
        """A provider commit arrived.

        A *new* SHA while a previous one is verified but unsettled invalidates
        that verification (P0.4): the buyer pays for the exact artifact that was
        graded, and this is no longer that artifact.
        """
        if (dup := self._check_duplicate(delivery_id)) is not None:
            return dup
        if self.state.is_terminal:
            return self._rejected(
                f"task already {self.state.value}; a settled contract accepts no "
                "further submissions"
            )
        if submission_sha == self.eligible_sha:
            # Same SHA re-announced (GitHub does this). Not a new artifact.
            return EventOutcome(Outcome.DUPLICATE, self.state, "submission SHA unchanged")

        superseded = self.eligible_sha
        detail = ""
        if superseded:
            self._superseded.append(superseded)
            detail = (
                f"new head SHA {submission_sha} supersedes {superseded}; "
                "any prior verification is invalidated and must be re-run"
            )
            if self.state in (TaskState.VERIFIED_PASS, TaskState.VERIFIED_FAIL):
                self.verified_sha = ""

        self.eligible_sha = submission_sha
        self.state = TaskState.SUBMITTED
        return EventOutcome(Outcome.APPLIED, self.state, detail)

    def on_verification_started(self, *, submission_sha: str, delivery_id: str) -> EventOutcome:
        if (dup := self._check_duplicate(delivery_id)) is not None:
            return dup
        if self.state.is_terminal:
            return self._rejected(f"task already {self.state.value}")
        if (stale := self._check_stale(submission_sha)) is not None:
            return stale
        if self.state is not TaskState.SUBMITTED:
            return self._rejected(f"cannot start verification from {self.state.value}")
        self.state = TaskState.VERIFYING
        return EventOutcome(Outcome.APPLIED, self.state)

    def on_verification_completed(self, *, manifest: object, delivery_id: str) -> EventOutcome:
        """A verification result arrived.

        Results are accepted only for the currently eligible SHA. An
        out-of-order result for a superseded SHA is dropped, which is precisely
        the force-push case: SHA-A verified PASS, provider pushed SHA-B, and
        A's result must not settle B.
        """
        if (dup := self._check_duplicate(delivery_id)) is not None:
            return dup
        if self.state.is_terminal:
            return self._rejected(f"task already {self.state.value}")

        submission_sha = str(getattr(manifest, "submission_sha", ""))
        if (stale := self._check_stale(submission_sha)) is not None:
            return stale

        manifest_contract = str(getattr(manifest, "contract_hash", ""))
        if manifest_contract != self.contract_hash:
            return self._rejected(
                f"verification result is for contract {manifest_contract}, "
                f"this task is {self.contract_hash}"
            )

        verdict = getattr(manifest, "verdict", None)
        verdict_value = str(getattr(verdict, "value", verdict))
        self.verified_sha = submission_sha
        self.state = TaskState.VERIFIED_PASS if verdict_value == "PASS" else TaskState.VERIFIED_FAIL
        return EventOutcome(Outcome.APPLIED, self.state, f"verdict {verdict_value}")

    def on_settlement(self, *, manifest: object, now: datetime, delivery_id: str) -> EventOutcome:
        """Execute the buyer's mandate. This is the only place funds move.

        Returns a directive rather than performing the transfer: the executor
        that actually submits the transaction is handed a decision that was
        already made, so there is nowhere for discretion to enter (P0.6).
        """
        if (dup := self._check_duplicate(delivery_id)) is not None:
            return dup
        if self.state.is_terminal:
            return self._rejected(
                f"task already {self.state.value} with tx {self.settlement_tx or 'pending'}; "
                "refusing a second settlement"
            )
        if self.state not in (TaskState.VERIFIED_PASS, TaskState.VERIFIED_FAIL):
            return self._rejected(f"cannot settle from {self.state.value}: no verified result")

        submission_sha = str(getattr(manifest, "submission_sha", ""))
        if (stale := self._check_stale(submission_sha)) is not None:
            return stale
        if submission_sha != self.verified_sha:
            return self._rejected(
                f"settlement is for {submission_sha}, but the verified artifact is "
                f"{self.verified_sha}"
            )

        directive = execute_mandate(mandate=self.mandate, manifest=manifest, now=now)

        key = settlement_key(
            task_id=self.task_id,
            submission_sha=submission_sha,
            contract_hash=self.contract_hash,
            terminal_verdict=self.state.value,
        )
        if self.settlement_key_used:
            return self._rejected(f"settlement key {self.settlement_key_used} already consumed")

        self.settlement_key_used = key
        self.state = (
            TaskState.SETTLED
            if directive.action is SettlementAction.RELEASE
            else TaskState.REFUNDED
        )
        return EventOutcome(Outcome.APPLIED, self.state, directive.reason, directive)

    def record_settlement_tx(self, tx_hash: str) -> None:
        """Attach the on-chain transaction to the terminal state."""
        if not self.state.is_terminal:
            raise SettlementError(
                f"cannot record a settlement tx from {self.state.value}: "
                "no settlement has been executed"
            )
        if self.settlement_tx and self.settlement_tx != tx_hash:
            raise SettlementError(
                f"task {self.task_id} already settled with tx {self.settlement_tx}; "
                f"refusing to overwrite with {tx_hash}"
            )
        self.settlement_tx = tx_hash

    # -- helpers --------------------------------------------------------------

    def _check_duplicate(self, delivery_id: str) -> EventOutcome | None:
        if delivery_id in self._seen_deliveries:
            return EventOutcome(
                Outcome.DUPLICATE, self.state, f"delivery {delivery_id} already processed"
            )
        self._seen_deliveries.add(delivery_id)
        return None

    def _check_stale(self, submission_sha: str) -> EventOutcome | None:
        if not submission_sha:
            return EventOutcome(Outcome.REJECTED, self.state, "event names no submission SHA")
        if submission_sha != self.eligible_sha:
            return EventOutcome(
                Outcome.STALE,
                self.state,
                f"event concerns {submission_sha}, but the eligible artifact is "
                f"{self.eligible_sha or 'none'}"
                + (" (superseded)" if submission_sha in self._superseded else ""),
            )
        return None

    def _rejected(self, detail: str) -> EventOutcome:
        return EventOutcome(Outcome.REJECTED, self.state, detail)
