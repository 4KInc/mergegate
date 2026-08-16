"""P0.4 / P0.5: artifact binding and idempotent settlement.

The failure these guard against costs real money: GitHub redelivers webhooks and
delivers them out of order, and a state machine that treats each delivery as new
double-releases escrow.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mergegate.mandate import MandateError, PaymentMandate, SettlementAction, expire_mandate
from mergegate.settlement import (
    Outcome,
    SettlementError,
    TaskState,
    TaskStateMachine,
    settlement_key,
)
from mergegate.verifier.manifest import CommandResult, VerificationManifest

from .conftest import BASE_SHA, IMAGE

CONTRACT_HASH = "sha256:" + "c" * 64
SHA_A = "1" * 40
SHA_B = "2" * 40


def _mandate(deadline: datetime | None = None) -> PaymentMandate:
    return PaymentMandate(
        task_id="task-001",
        contract_hash=CONTRACT_HASH,
        buyer_agent="0xBUYER",
        provider_agent="0xPROVIDER",
        amount_usdc="250.00",
        asset="USDC",
        chain="base",
        deadline=deadline or (datetime.now(UTC) + timedelta(hours=6)),
        nonce="nonce-1",
    )


def _manifest(submission_sha: str, *, passing: bool = True) -> VerificationManifest:
    return VerificationManifest(
        task_id="task-001",
        contract_hash=CONTRACT_HASH,
        grader_hash="sha256:" + "d" * 64,
        base_sha=BASE_SHA,
        submission_sha=submission_sha,
        tree_hash="sha256:" + "e" * 64,
        verifier_image_digest=IMAGE,
        commands=(
            CommandResult(
                argv=("pytest",),
                exit_code=0 if passing else 1,
                stdout_digest="sha256:" + "0" * 64,
                stderr_digest="sha256:" + "1" * 64,
                duration_ms=10,
            ),
        ),
    )


@pytest.fixture
def machine() -> TaskStateMachine:
    return TaskStateMachine(task_id="task-001", contract_hash=CONTRACT_HASH, mandate=_mandate())


def _drive_to_verified(machine: TaskStateMachine, sha: str, *, passing: bool = True) -> None:
    machine.on_submission(submission_sha=sha, delivery_id=f"d-sub-{sha}")
    machine.on_verification_started(submission_sha=sha, delivery_id=f"d-start-{sha}")
    machine.on_verification_completed(
        manifest=_manifest(sha, passing=passing), delivery_id=f"d-done-{sha}"
    )


# -- the happy path -----------------------------------------------------------


def test_pass_flow_settles_once(machine: TaskStateMachine) -> None:
    _drive_to_verified(machine, SHA_A)
    before = machine.state
    assert before is TaskState.VERIFIED_PASS

    result = machine.on_settlement(
        manifest=_manifest(SHA_A), now=datetime.now(UTC), delivery_id="d-settle"
    )
    assert result.applied
    after = machine.state
    assert after is TaskState.SETTLED
    assert result.directive is not None
    assert result.directive.action is SettlementAction.RELEASE
    assert result.directive.recipient == "0xPROVIDER"


def test_fail_flow_refunds(machine: TaskStateMachine) -> None:
    _drive_to_verified(machine, SHA_A, passing=False)
    result = machine.on_settlement(
        manifest=_manifest(SHA_A, passing=False), now=datetime.now(UTC), delivery_id="d-settle"
    )
    assert machine.state is TaskState.REFUNDED
    assert result.directive is not None
    assert result.directive.action is SettlementAction.REFUND
    assert result.directive.recipient == "0xBUYER"


# -- P0.5: duplicates -------------------------------------------------------


def test_duplicate_delivery_is_ignored(machine: TaskStateMachine) -> None:
    first = machine.on_submission(submission_sha=SHA_A, delivery_id="d1")
    second = machine.on_submission(submission_sha=SHA_A, delivery_id="d1")
    assert first.applied
    assert second.outcome is Outcome.DUPLICATE
    assert machine.state is TaskState.SUBMITTED


def test_replayed_settlement_does_not_pay_twice(machine: TaskStateMachine) -> None:
    """The core P0.5 assertion: exactly one settlement action."""
    _drive_to_verified(machine, SHA_A)
    first = machine.on_settlement(
        manifest=_manifest(SHA_A), now=datetime.now(UTC), delivery_id="d-settle"
    )
    assert first.applied

    # Same delivery redelivered.
    replay = machine.on_settlement(
        manifest=_manifest(SHA_A), now=datetime.now(UTC), delivery_id="d-settle"
    )
    assert replay.outcome is Outcome.DUPLICATE

    # A *different* delivery ID for the same settlement must also be refused.
    forged = machine.on_settlement(
        manifest=_manifest(SHA_A), now=datetime.now(UTC), delivery_id="d-settle-again"
    )
    assert forged.outcome is Outcome.REJECTED
    assert "refusing a second settlement" in forged.detail
    assert machine.state is TaskState.SETTLED


def test_a_full_replay_of_every_event_settles_exactly_once(
    machine: TaskStateMachine,
) -> None:
    """Replay the whole webhook sequence twice, count the settlements."""
    events = [
        ("sub", "d1"),
        ("start", "d2"),
        ("done", "d3"),
        ("settle", "d4"),
    ]
    settlements = 0
    for _round in range(2):
        for kind, delivery in events:
            if kind == "sub":
                machine.on_submission(submission_sha=SHA_A, delivery_id=delivery)
            elif kind == "start":
                machine.on_verification_started(submission_sha=SHA_A, delivery_id=delivery)
            elif kind == "done":
                machine.on_verification_completed(manifest=_manifest(SHA_A), delivery_id=delivery)
            else:
                outcome = machine.on_settlement(
                    manifest=_manifest(SHA_A), now=datetime.now(UTC), delivery_id=delivery
                )
                if outcome.applied:
                    settlements += 1

    assert settlements == 1
    assert machine.state is TaskState.SETTLED


def test_terminal_state_rejects_everything_later(machine: TaskStateMachine) -> None:
    _drive_to_verified(machine, SHA_A)
    machine.on_settlement(manifest=_manifest(SHA_A), now=datetime.now(UTC), delivery_id="d-settle")

    assert machine.on_submission(submission_sha=SHA_B, delivery_id="x1").outcome is (
        Outcome.REJECTED
    )
    assert (
        machine.on_verification_started(submission_sha=SHA_A, delivery_id="x2").outcome
        is Outcome.REJECTED
    )
    assert (
        machine.on_verification_completed(manifest=_manifest(SHA_A), delivery_id="x3").outcome
        is Outcome.REJECTED
    )
    assert machine.state is TaskState.SETTLED


# -- P0.4: artifact binding / force-push --------------------------------------


def test_force_push_invalidates_a_prior_pass(machine: TaskStateMachine) -> None:
    """P0.4 done-when: a head-SHA change invalidates the prior PASS.

    The attack: get SHA-A verified, force-push SHA-B, and have the settlement
    pay out for B on A's result.
    """
    _drive_to_verified(machine, SHA_A)
    before = machine.state
    assert before is TaskState.VERIFIED_PASS

    pushed = machine.on_submission(submission_sha=SHA_B, delivery_id="d-force")
    assert pushed.applied
    after = machine.state
    assert after is TaskState.SUBMITTED
    assert machine.verified_sha == ""
    assert "supersedes" in pushed.detail

    # Settling now must fail: there is no verified result for the eligible SHA.
    blocked = machine.on_settlement(
        manifest=_manifest(SHA_B), now=datetime.now(UTC), delivery_id="d-settle"
    )
    assert blocked.outcome is Outcome.REJECTED
    assert machine.state is TaskState.SUBMITTED


def test_stale_result_for_a_superseded_sha_is_dropped(machine: TaskStateMachine) -> None:
    """Out-of-order delivery: A's verification lands after B was pushed."""
    machine.on_submission(submission_sha=SHA_A, delivery_id="d1")
    machine.on_submission(submission_sha=SHA_B, delivery_id="d2")

    late = machine.on_verification_completed(manifest=_manifest(SHA_A), delivery_id="d3")
    assert late.outcome is Outcome.STALE
    assert "superseded" in late.detail
    assert machine.verified_sha == ""


def test_settlement_must_match_the_verified_artifact(machine: TaskStateMachine) -> None:
    _drive_to_verified(machine, SHA_A)
    mismatched = machine.on_settlement(
        manifest=_manifest(SHA_B), now=datetime.now(UTC), delivery_id="d-settle"
    )
    assert mismatched.outcome is Outcome.STALE
    assert machine.state is TaskState.VERIFIED_PASS


def test_result_for_another_contract_is_rejected(machine: TaskStateMachine) -> None:
    machine.on_submission(submission_sha=SHA_A, delivery_id="d1")
    foreign = VerificationManifest(
        task_id="task-001",
        contract_hash="sha256:" + "f" * 64,
        grader_hash="sha256:" + "d" * 64,
        base_sha=BASE_SHA,
        submission_sha=SHA_A,
        tree_hash="sha256:" + "e" * 64,
        verifier_image_digest=IMAGE,
    )
    result = machine.on_verification_completed(manifest=foreign, delivery_id="d2")
    assert result.outcome is Outcome.REJECTED


def test_resubmitting_the_same_sha_is_not_a_new_artifact(machine: TaskStateMachine) -> None:
    machine.on_submission(submission_sha=SHA_A, delivery_id="d1")
    again = machine.on_submission(submission_sha=SHA_A, delivery_id="d2")
    assert again.outcome is Outcome.DUPLICATE


# -- ordering guards ----------------------------------------------------------


def test_cannot_settle_without_a_verified_result(machine: TaskStateMachine) -> None:
    machine.on_submission(submission_sha=SHA_A, delivery_id="d1")
    result = machine.on_settlement(
        manifest=_manifest(SHA_A), now=datetime.now(UTC), delivery_id="d2"
    )
    assert result.outcome is Outcome.REJECTED
    assert "no verified result" in result.detail


def test_cannot_start_verification_before_a_submission(machine: TaskStateMachine) -> None:
    result = machine.on_verification_started(submission_sha=SHA_A, delivery_id="d1")
    assert result.outcome is Outcome.STALE


# -- settlement key -----------------------------------------------------------


def test_settlement_key_is_field_separated() -> None:
    """Concatenating without length prefixes lets two different splits collide."""
    a = settlement_key(task_id="ab", submission_sha="c", contract_hash="d", terminal_verdict="PASS")
    b = settlement_key(task_id="a", submission_sha="bc", contract_hash="d", terminal_verdict="PASS")
    assert a != b


def test_settlement_key_changes_with_the_verdict() -> None:
    common = {"task_id": "t", "submission_sha": SHA_A, "contract_hash": CONTRACT_HASH}
    assert settlement_key(**common, terminal_verdict="PASS") != settlement_key(
        **common, terminal_verdict="FAIL"
    )


# -- transaction recording ----------------------------------------------------


def test_settlement_tx_cannot_be_overwritten(machine: TaskStateMachine) -> None:
    _drive_to_verified(machine, SHA_A)
    machine.on_settlement(manifest=_manifest(SHA_A), now=datetime.now(UTC), delivery_id="d-settle")
    machine.record_settlement_tx("0xabc")
    machine.record_settlement_tx("0xabc")  # idempotent

    with pytest.raises(SettlementError, match="refusing to overwrite"):
        machine.record_settlement_tx("0xdef")


def test_cannot_record_a_tx_before_settling(machine: TaskStateMachine) -> None:
    with pytest.raises(SettlementError, match="no settlement has been executed"):
        machine.record_settlement_tx("0xabc")


# -- P0.6: the mandate decides, not the executor ------------------------------


def test_a_late_pass_refunds(machine: TaskStateMachine) -> None:
    """The mandate said 'before T'. A PASS that arrives after T fails it."""
    expired = TaskStateMachine(
        task_id="task-001",
        contract_hash=CONTRACT_HASH,
        mandate=_mandate(deadline=datetime.now(UTC) - timedelta(minutes=1)),
    )
    _drive_to_verified(expired, SHA_A)
    result = expired.on_settlement(
        manifest=_manifest(SHA_A), now=datetime.now(UTC), delivery_id="d-settle"
    )
    assert expired.state is TaskState.REFUNDED
    assert result.directive is not None
    assert "deadline passed" in result.directive.reason


# -- expiry -------------------------------------------------------------------
#
# The exit that did not exist. A task the verifier never answered used to sit in
# VERIFYING forever with escrow funded: "never releases funds incorrectly" was
# true, "always reaches a terminal state" was not.


def _expired_machine() -> TaskStateMachine:
    return TaskStateMachine(
        task_id="task-001",
        contract_hash=CONTRACT_HASH,
        mandate=_mandate(deadline=datetime.now(UTC) - timedelta(hours=1)),
    )


def test_a_task_that_never_got_a_verdict_expires_and_refunds() -> None:
    machine = _expired_machine()
    machine.on_submission(submission_sha=SHA_A, delivery_id="d-sub")
    machine.on_verification_started(submission_sha=SHA_A, delivery_id="d-start")
    # No on_verification_completed: this is the sealed job never returning an
    # acceptable manifest. Read into a local so the checker does not narrow
    # ``machine.state`` and then flag the post-transition assertion below as
    # unreachable.
    before = machine.state
    assert before is TaskState.VERIFYING

    result = machine.on_deadline(now=datetime.now(UTC), delivery_id="d-expire")

    assert result.applied
    assert machine.state is TaskState.EXPIRED
    assert machine.state.is_terminal
    assert result.directive is not None
    assert result.directive.action is SettlementAction.REFUND
    assert result.directive.recipient == "0xBUYER"


def test_expiry_before_the_deadline_is_refused() -> None:
    """An unreachable verifier is not an expiry.

    Before the deadline the evaluation can still be retried, so the task stays
    in VERIFYING. Treating an outage as an expiry would let infrastructure
    failure close a task that still had time to succeed.
    """
    machine = TaskStateMachine(
        task_id="task-001",
        contract_hash=CONTRACT_HASH,
        mandate=_mandate(deadline=datetime.now(UTC) + timedelta(hours=6)),
    )
    machine.on_submission(submission_sha=SHA_A, delivery_id="d-sub")

    result = machine.on_deadline(now=datetime.now(UTC), delivery_id="d-expire")

    assert not result.applied
    assert machine.state is TaskState.SUBMITTED
    assert "has not passed" in result.detail


def test_a_passing_task_can_never_be_expired() -> None:
    """The griefing vector this restriction closes.

    ``execute_mandate`` checks the deadline *before* the verdict, so a PASS
    settled after T refunds. If a graded task could also expire, then anyone
    able to delay settlement past T could convert a provider's PASS into a
    refund by doing nothing at all — stalling would become a way to not pay for
    work that was delivered and graded.
    """
    machine = _expired_machine()
    _drive_to_verified(machine, SHA_A, passing=True)
    assert machine.state is TaskState.VERIFIED_PASS

    result = machine.on_deadline(now=datetime.now(UTC), delivery_id="d-expire")

    assert not result.applied
    assert machine.state is TaskState.VERIFIED_PASS, "a graded task must not be expirable"
    assert "a verdict exists" in result.detail


def test_a_failed_task_also_settles_rather_than_expiring() -> None:
    """Same rule, and it must not depend on the verdict being favourable.

    A FAIL refunds either way, so nothing changes financially. The restriction
    is kept uniform anyway: "expiry is for tasks with no verdict" is checkable,
    where "expiry is for tasks whose verdict would not have paid out" invites
    exactly the reasoning this system exists to keep out of settlement.
    """
    machine = _expired_machine()
    _drive_to_verified(machine, SHA_A, passing=False)

    result = machine.on_deadline(now=datetime.now(UTC), delivery_id="d-expire")

    assert not result.applied
    assert machine.state is TaskState.VERIFIED_FAIL


def test_an_expired_task_accepts_nothing_further() -> None:
    machine = _expired_machine()
    machine.on_submission(submission_sha=SHA_A, delivery_id="d-sub")
    machine.on_deadline(now=datetime.now(UTC), delivery_id="d-expire")

    late = machine.on_verification_completed(manifest=_manifest(SHA_A), delivery_id="d-late")
    assert not late.applied

    resubmit = machine.on_submission(submission_sha=SHA_B, delivery_id="d-sub-2")
    assert not resubmit.applied
    assert machine.state is TaskState.EXPIRED


def test_expiring_twice_is_refused() -> None:
    machine = _expired_machine()
    machine.on_submission(submission_sha=SHA_A, delivery_id="d-sub")
    assert machine.on_deadline(now=datetime.now(UTC), delivery_id="d-expire-1").applied

    second = machine.on_deadline(now=datetime.now(UTC), delivery_id="d-expire-2")
    assert not second.applied, "expiry must be as terminal as settlement"


def test_expire_mandate_refuses_a_live_deadline() -> None:
    """The guard lives in the payment module, not only in the caller."""
    mandate = _mandate(deadline=datetime.now(UTC) + timedelta(hours=6))
    with pytest.raises(MandateError):
        expire_mandate(mandate=mandate, now=datetime.now(UTC))
