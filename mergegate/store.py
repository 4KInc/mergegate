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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from .mandate import PaymentMandate
from .settlement import EventOutcome, TaskState, TaskStateMachine

__all__ = [
    "ContractStore",
    "FirestoreContractStore",
    "MemoryContractStore",
    "ReceiptStore",
    "FirestoreReceiptStore",
    "MemoryReceiptStore",
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

        @firestore.transactional
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


# -- receipts -----------------------------------------------------------------

RECEIPT_COLLECTION = "mergegate_receipts"


class ReceiptStore(Protocol):
    """Where issued receipts live so the dashboard can read them.

    Separate from :class:`TaskStore` because their lifetimes differ: task state
    is mutable until settlement and then frozen, whereas a receipt is written
    once and never updated. Nothing here offers a way to modify one.
    """

    def put(self, receipt_id: str, envelope: dict[str, Any]) -> None: ...

    def all(self) -> list[tuple[str, dict[str, Any]]]: ...

    def get(self, receipt_id: str) -> dict[str, Any] | None: ...


@dataclass
class MemoryReceiptStore:
    """In-process receipts, for tests."""

    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)

    def put(self, receipt_id: str, envelope: dict[str, Any]) -> None:
        self.receipts[receipt_id] = envelope

    def all(self) -> list[tuple[str, dict[str, Any]]]:
        return sorted(self.receipts.items())

    def get(self, receipt_id: str) -> dict[str, Any] | None:
        return self.receipts.get(receipt_id)


class FirestoreReceiptStore:
    """Receipts in Firestore, one document each.

    The envelope is stored under a single ``envelope`` field rather than spread
    across top-level fields. Firestore would happily reshape nested data —
    reordering maps, coercing numbers — and the receipt's signature is over
    exact canonical bytes. Storing it as one opaque JSON string means what comes
    back out is byte-identical to what was signed, so it still verifies.
    """

    def __init__(self, client: Any = None, *, collection: str = RECEIPT_COLLECTION) -> None:
        if client is None:
            from google.cloud import firestore

            client = firestore.Client()
        self._db = client
        self._collection = collection

    def _ref(self, receipt_id: str) -> Any:
        return self._db.collection(self._collection).document(document_id(receipt_id))

    def put(self, receipt_id: str, envelope: dict[str, Any]) -> None:
        import json as _json

        self._ref(receipt_id).set(
            {
                "receipt_id": receipt_id,
                "envelope_json": _json.dumps(envelope),
                "decision": str(
                    (envelope.get("body", {}).get("binding") or {}).get("decision", "")
                ),
                "chain": str((envelope.get("body", {}).get("mandate") or {}).get("chain", "")),
                "issued_at": str(envelope.get("body", {}).get("issued_at", "")),
                "stored_at": datetime.now(UTC).isoformat(),
            }
        )

    def _decode(self, doc: dict[str, Any]) -> dict[str, Any] | None:
        import json as _json

        raw = doc.get("envelope_json")
        if not isinstance(raw, str):
            return None
        try:
            parsed = _json.loads(raw)
        except _json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def all(self) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        for snap in self._db.collection(self._collection).stream():
            doc = snap.to_dict() or {}
            envelope = self._decode(doc)
            if envelope is not None:
                out.append((str(doc.get("receipt_id") or snap.id), envelope))
        return sorted(out, key=lambda pair: pair[0])

    def get(self, receipt_id: str) -> dict[str, Any] | None:
        snap = self._ref(receipt_id).get()
        if not snap.exists:
            return None
        return self._decode(snap.to_dict() or {})


# -- funded contracts ---------------------------------------------------------

CONTRACT_COLLECTION = "mergegate_contracts"


class ContractStore(Protocol):
    """Funded contracts, so the terms can be shown alongside the receipt.

    A receipt binds ``contract_hash`` but not the terms themselves, and carries
    no funding transaction at all. Without this the contract page would have to
    describe terms it cannot produce, which is the kind of gap that turns into
    invented data.
    """

    def put(self, record: dict[str, Any]) -> None: ...

    def get(self, contract_hash: str) -> dict[str, Any] | None: ...

    def all(self) -> list[dict[str, Any]]: ...


@dataclass
class MemoryContractStore:
    contracts: dict[str, dict[str, Any]] = field(default_factory=dict)

    def put(self, record: dict[str, Any]) -> None:
        self.contracts[str(record["contract_hash"])] = record

    def get(self, contract_hash: str) -> dict[str, Any] | None:
        return self.contracts.get(contract_hash)

    def all(self) -> list[dict[str, Any]]:
        return [self.contracts[k] for k in sorted(self.contracts)]


class FirestoreContractStore:
    """Funded contracts in Firestore, keyed by contract hash.

    Terms are stored as one JSON string for the same reason receipts are: the
    hash is over exact canonical bytes, and Firestore reshaping a nested map
    would make the stored terms stop hashing to the value they are filed under.
    """

    def __init__(self, client: Any = None, *, collection: str = CONTRACT_COLLECTION) -> None:
        if client is None:
            from google.cloud import firestore

            client = firestore.Client()
        self._db = client
        self._collection = collection

    def _ref(self, contract_hash: str) -> Any:
        return self._db.collection(self._collection).document(document_id(contract_hash))

    def put(self, record: dict[str, Any]) -> None:
        import json as _json

        payload = dict(record)
        payload["terms_json"] = _json.dumps(record.get("terms", {}))
        payload.pop("terms", None)
        payload["stored_at"] = datetime.now(UTC).isoformat()
        self._ref(str(record["contract_hash"])).set(payload)

    def _decode(self, doc: dict[str, Any]) -> dict[str, Any]:
        import json as _json

        out = dict(doc)
        raw = out.pop("terms_json", "")
        try:
            out["terms"] = _json.loads(raw) if isinstance(raw, str) and raw else {}
        except _json.JSONDecodeError:
            out["terms"] = {}
        return out

    def get(self, contract_hash: str) -> dict[str, Any] | None:
        snap = self._ref(contract_hash).get()
        if not snap.exists:
            return None
        return self._decode(snap.to_dict() or {})

    def all(self) -> list[dict[str, Any]]:
        return [
            self._decode(s.to_dict() or {}) for s in self._db.collection(self._collection).stream()
        ]
