"""The settlement rail interface.

MergeGate moves USDC through exactly one narrow surface, so that the choice of
rail — Circle CLI agent wallets today, the REST API or a deployed contract
later — is a swap of one class rather than a change to the settlement logic.

The interface is deliberately smaller than what any rail offers. It can move
funds between two addresses with an idempotency key, and it can read a balance.
It cannot mint, approve, sweep, or change policy. A rail implementation that
needs more surface than this is doing something the settlement path should not
be able to do.

**Idempotency is the load-bearing parameter.** Every transfer carries the
settlement key from :mod:`mergegate.settlement` — ``sha256(task_id ||
submission_sha || contract_hash || terminal_verdict)``. That makes the rail a
second, independent guard against double-payment: even if the state machine were
somehow driven twice, the rail sees a repeated key and returns the original
transaction instead of sending a new one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["SettlementRail", "TransferReceipt", "RailError"]


class RailError(RuntimeError):
    """A transfer could not be completed. Never raised for a duplicate."""


@dataclass(frozen=True, slots=True)
class TransferReceipt:
    """The result of one on-chain transfer."""

    tx_hash: str
    state: str
    source: str
    destination: str
    amount_usdc: str
    chain: str
    explorer_url: str
    idempotency_key: str
    deduplicated: bool = False
    """True when the rail returned an existing transfer for this key rather than
    sending a new one. Surfaced rather than hidden — a deduplicated settlement
    means something upstream tried to pay twice, which is worth knowing."""

    block_height: int | None = None


@runtime_checkable
class SettlementRail(Protocol):
    """What MergeGate needs from a payment rail, and nothing more."""

    @property
    def chain(self) -> str:
        """Chain identifier as the rail names it, e.g. ``BASE``."""
        ...

    def transfer(
        self,
        *,
        source: str,
        destination: str,
        amount_usdc: str,
        idempotency_key: str,
    ) -> TransferReceipt:
        """Move USDC. Must be idempotent on ``idempotency_key``."""
        ...

    def balance_usdc(self, address: str) -> str:
        """Current USDC balance as a decimal string."""
        ...
