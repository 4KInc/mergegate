"""Durable settlement state.

:class:`~mergegate.settlement.TaskStateMachine` enforces "exactly one terminal
settlement", but it holds that state in memory. On Cloud Run that is not enough:
instances cold-start and scale out, so a redelivered webhook arriving at a fresh
instance would meet an empty ``_seen_deliveries`` and be treated as new. The
guarantee would hold in tests and fail in production — the worst shape for a
guarantee about money.

So state is persisted per task, and every event is applied inside a **Firestore
transaction** keyed on that task. The transaction is what makes read-modify-write
atomic across concurrent deliveries; the state machine's invariants are what that
atomicity protects, exactly as its docstring says.

The rail's idempotency key remains the last line of defence. If this layer were
somehow bypassed, Circle would still refuse a second transfer for the same
settlement key.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from .mandate import PaymentMandate
from .settlement import EventOutcome, TaskState, TaskStateMachine

__all__ = [
    "TaskStore",
    "FirestoreTaskStore",
    "MemoryTaskStore",
    "to_document",
    "from_document",
    "document_id",
]

COLLECTION = "mergegate_tasks"

# Firestore document IDs may not contain "/" — a path with slashes is parsed as
# alternating collection/document segments, so "mergegate_tasks/4KInc/demo-task"
# is a *collection* reference and .document() rejects it. Task ids are repository
# full names, which always contain a slash, so every real lookup hit this.
#
# "~" is not legal in a GitHub owner or repository name (those allow only
# alphanumerics, "-", "_", and "."), so the mapping is unambiguous and stays
# readable in the console rather than becoming an opaque hash.
_ID_SEPARATOR = "~"


def document_id(task_id: str) -> str:
    """Map a task id onto a legal Firestore document id.

    Raises on the forms Firestore reserves, rather than silently producing an id
    that fails later at write time.
    """
    if not task_id:
        raise ValueError("task_id must not be empty")
    doc_id = task_id.replace("/", _ID_SEPARATOR)
    if doc_id in (".", ".."):
        raise ValueError(f"task_id {task_id!r} maps to a reserved Firestore id")
    if doc_id.startswith("__") and doc_id.endswith("__"):
        raise ValueError(f"task_id {task_id!r} maps to a reserved __*__ Firestore id")
    if len(doc_id.encode()) > 1500:
        raise ValueError(f"task_id {task_id!r} exceeds the Firestore document id limit")
    return doc_id


def to_document(machine: TaskStateMachine) -> dict[str, Any]:
    """Serialize a state machine to a Firestore document.

    Written explicitly rather than reflected from ``__dict__`` so that adding a
    field to the state machine without persisting it is a visible omission here
    rather than silent state loss on the next cold start.
    """
    mandate = machine.mandate
    return {
        "task_id": machine.task_id,
        "contract_hash": machine.contract_hash,
        "state": machine.state.value,
        "eligible_sha": machine.eligible_sha,
        "verified_sha": machine.verified_sha,
        "settlement_tx": machine.settlement_tx,
        "settlement_key_used": machine.settlement_key_used,
        # Dedup and supersession history are the whole point of persisting.
        "seen_deliveries": sorted(machine._seen_deliveries),
        "superseded": list(machine._superseded),
        "mandate": {
            "task_id": mandate.task_id,
            "contract_hash": mandate.contract_hash,
            "buyer_agent": mandate.buyer_agent,
            "provider_agent": mandate.provider_agent,
            "amount_usdc": mandate.amount_usdc,
            "asset": mandate.asset,
            "chain": mandate.chain,
            "deadline": mandate.deadline.astimezone(UTC).isoformat(),
            "nonce": mandate.nonce,
        },
        "updated_at": datetime.now(UTC).isoformat(),
    }


def from_document(doc: dict[str, Any]) -> TaskStateMachine:
    """Rebuild a state machine from its document."""
    m = doc["mandate"]
    mandate = PaymentMandate(
        task_id=m["task_id"],
        contract_hash=m["contract_hash"],
        buyer_agent=m["buyer_agent"],
        provider_agent=m["provider_agent"],
        amount_usdc=m["amount_usdc"],
        asset=m["asset"],
        chain=m["chain"],
        deadline=datetime.fromisoformat(m["deadline"]),
        nonce=m["nonce"],
    )
    machine = TaskStateMachine(
        task_id=doc["task_id"],
        contract_hash=doc["contract_hash"],
        mandate=mandate,
        state=TaskState(doc["state"]),
        eligible_sha=doc.get("eligible_sha", ""),
        verified_sha=doc.get("verified_sha", ""),
        settlement_tx=doc.get("settlement_tx", ""),
        settlement_key_used=doc.get("settlement_key_used", ""),
    )
    machine._seen_deliveries = set(doc.get("seen_deliveries", []))
    machine._superseded = list(doc.get("superseded", []))
    return machine


class TaskStore(Protocol):
    """Load a task, apply one event atomically, persist the result."""

    def apply(
        self, task_id: str, event: Callable[[TaskStateMachine], EventOutcome]
    ) -> EventOutcome | None:
        """Run ``event`` against the stored machine inside a transaction.

        Returns ``None`` if no such task exists. The callback must be pure with
        respect to everything except the machine it is handed, because a
        contended transaction will re-run it.
        """
        ...

    def put(self, machine: TaskStateMachine) -> None: ...

    def get(self, task_id: str) -> TaskStateMachine | None: ...


@dataclass
class MemoryTaskStore:
    """In-process store for tests and local runs.

    Deliberately **not** used in deployment: it would reintroduce exactly the
    cold-start hole this module exists to close.
    """

    tasks: dict[str, TaskStateMachine] | None = None

    def __post_init__(self) -> None:
        if self.tasks is None:
            self.tasks = {}

    def get(self, task_id: str) -> TaskStateMachine | None:
        assert self.tasks is not None
        return self.tasks.get(task_id)

    def put(self, machine: TaskStateMachine) -> None:
        assert self.tasks is not None
        self.tasks[machine.task_id] = machine

    def apply(
        self, task_id: str, event: Callable[[TaskStateMachine], EventOutcome]
    ) -> EventOutcome | None:
        machine = self.get(task_id)
        if machine is None:
            return None
        outcome = event(machine)
        self.put(machine)
        return outcome


class FirestoreTaskStore:
    """Firestore-backed store. One document per task, one transaction per event."""

    def __init__(self, client: Any = None, *, collection: str = COLLECTION) -> None:
        if client is None:
            from google.cloud import firestore

            client = firestore.Client()
        self._db = client
        self._collection = collection

    def _ref(self, task_id: str) -> Any:
        return self._db.collection(self._collection).document(document_id(task_id))

    def get(self, task_id: str) -> TaskStateMachine | None:
        snap = self._ref(task_id).get()
        if not snap.exists:
            return None
        return from_document(snap.to_dict())

    def put(self, machine: TaskStateMachine) -> None:
        self._ref(machine.task_id).set(to_document(machine))

    def apply(
        self, task_id: str, event: Callable[[TaskStateMachine], EventOutcome]
    ) -> EventOutcome | None:
        from google.cloud import firestore

        ref = self._ref(task_id)

        # firestore.transactional is untyped upstream; the wrapped function
        # below is fully annotated, which is where the type safety matters.
        @firestore.transactional  # type: ignore[untyped-decorator]
        def _txn(transaction: Any) -> EventOutcome | None:
            snap = ref.get(transaction=transaction)
            if not snap.exists:
                return None
            machine = from_document(snap.to_dict())
            outcome = event(machine)
            transaction.set(ref, to_document(machine))
            return outcome

        result: EventOutcome | None = _txn(self._db.transaction())
        return result
