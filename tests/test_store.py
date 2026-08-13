"""Durability of settlement state.

The state machine's dedup lives in memory. On Cloud Run, instances cold-start
and scale out, so without persistence a redelivered webhook meets a fresh
machine and settles a second time. These tests pin the round-trip that closes
that hole.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mergegate.mandate import PaymentMandate
from mergegate.settlement import Outcome, TaskState, TaskStateMachine
from mergegate.store import MemoryTaskStore, from_document, to_document

CONTRACT_HASH = "sha256:" + "c" * 64
SHA_A = "1" * 40
SHA_B = "2" * 40


def _machine() -> TaskStateMachine:
    return TaskStateMachine(
        task_id="4KInc/mergegate-demo-task",
        contract_hash=CONTRACT_HASH,
        mandate=PaymentMandate(
            task_id="4KInc/mergegate-demo-task",
            contract_hash=CONTRACT_HASH,
            buyer_agent="0xBUYER",
            provider_agent="0xPROVIDER",
            amount_usdc="0.25",
            asset="USDC",
            chain="BASE-SEPOLIA",
            deadline=datetime.now(UTC) + timedelta(hours=6),
            nonce="n1",
        ),
    )


def test_round_trip_preserves_every_field() -> None:
    machine = _machine()
    machine.on_submission(submission_sha=SHA_A, delivery_id="d1")
    machine.on_submission(submission_sha=SHA_B, delivery_id="d2")

    restored = from_document(to_document(machine))

    assert restored.task_id == machine.task_id
    assert restored.contract_hash == machine.contract_hash
    assert restored.state is machine.state
    assert restored.eligible_sha == SHA_B
    assert restored.mandate.mandate_hash == machine.mandate.mandate_hash


def test_dedup_survives_a_cold_start() -> None:
    """The failure this module exists to prevent.

    Without persistence, a redelivered webhook hitting a fresh instance would be
    applied a second time.
    """
    machine = _machine()
    first = machine.on_submission(submission_sha=SHA_A, delivery_id="delivery-1")
    assert first.applied

    revived = from_document(to_document(machine))
    replay = revived.on_submission(submission_sha=SHA_A, delivery_id="delivery-1")
    assert replay.outcome is Outcome.DUPLICATE


def test_supersession_history_survives() -> None:
    """A stale result for a superseded SHA must still be recognised as stale
    after a restart, or a force-push could inherit the earlier verdict."""
    machine = _machine()
    machine.on_submission(submission_sha=SHA_A, delivery_id="d1")
    machine.on_submission(submission_sha=SHA_B, delivery_id="d2")

    revived = from_document(to_document(machine))
    assert SHA_A in revived._superseded


def test_terminal_state_survives() -> None:
    machine = _machine()
    machine.state = TaskState.SETTLED
    machine.settlement_tx = "0xabc"
    machine.settlement_key_used = "sha256:" + "f" * 64

    revived = from_document(to_document(machine))
    assert revived.state.is_terminal
    assert revived.settlement_tx == "0xabc"
    # A settled task must keep refusing new work after a restart.
    assert revived.on_submission(submission_sha=SHA_B, delivery_id="d9").outcome is Outcome.REJECTED


def test_memory_store_applies_and_persists() -> None:
    store = MemoryTaskStore()
    machine = _machine()
    store.put(machine)

    outcome = store.apply(
        machine.task_id, lambda m: m.on_submission(submission_sha=SHA_A, delivery_id="d1")
    )
    assert outcome is not None and outcome.applied
    reloaded = store.get(machine.task_id)
    assert reloaded is not None
    assert reloaded.eligible_sha == SHA_A


def test_apply_on_unknown_task_returns_none() -> None:
    assert (
        MemoryTaskStore().apply(
            "nope", lambda m: m.on_submission(submission_sha=SHA_A, delivery_id="d1")
        )
        is None
    )
