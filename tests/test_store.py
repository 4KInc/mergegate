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


# -- Firestore document ids ---------------------------------------------------


def test_repository_task_ids_become_legal_document_ids() -> None:
    """The bug that produced a 500 on the first real push.

    Task ids are repository full names, which always contain a slash. Firestore
    reads a slash as a collection/document boundary, so
    "mergegate_tasks/4KInc/demo-task" is a collection reference and .document()
    rejects it — every real lookup failed.
    """
    from mergegate.store import document_id

    assert "/" not in document_id("4KInc/mergegate-demo-task")
    assert document_id("4KInc/mergegate-demo-task") == "4KInc~mergegate-demo-task"


def test_document_ids_stay_distinct() -> None:
    from mergegate.store import document_id

    assert document_id("a/b") != document_id("a/c")
    assert document_id("owner/repo") != document_id("other/repo")


def test_reserved_document_ids_are_refused() -> None:
    """Fail at the boundary rather than producing an id Firestore rejects later."""
    import pytest

    from mergegate.store import document_id

    for bad in ("", ".", "..", "__reserved__"):
        with pytest.raises(ValueError):
            document_id(bad)
