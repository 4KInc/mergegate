"""An in-memory settlement rail for tests and dry runs.

Exists so the settlement path can be exercised without spending USDC. It
enforces the same idempotency contract the real rail does, because a fake that
is more permissive than production hides exactly the bugs it should catch.

This is a test double, not a simulator: it does not model gas, confirmation
latency, or reorgs. Anything depending on those has to be proven against a real
chain.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal

from .base import RailError, TransferReceipt

__all__ = ["FakeRail"]


@dataclass
class FakeRail:
    """Records transfers and refuses to repeat one for a seen key."""

    chain: str = "BASE-FAKE"
    balances: dict[str, Decimal] = field(default_factory=dict)
    transfers: list[TransferReceipt] = field(default_factory=list)
    fail_next_with: str = ""
    """Set to a message to make the next transfer raise, for failure-path tests."""

    _by_key: dict[str, TransferReceipt] = field(default_factory=dict)

    def transfer(
        self,
        *,
        source: str,
        destination: str,
        amount_usdc: str,
        idempotency_key: str,
    ) -> TransferReceipt:
        if not idempotency_key:
            raise RailError("refusing to transfer without an idempotency key")

        if (existing := self._by_key.get(idempotency_key)) is not None:
            # Same contract as the real rail: return the original, do not resend.
            return TransferReceipt(
                tx_hash=existing.tx_hash,
                state=existing.state,
                source=existing.source,
                destination=existing.destination,
                amount_usdc=existing.amount_usdc,
                chain=existing.chain,
                explorer_url=existing.explorer_url,
                idempotency_key=idempotency_key,
                deduplicated=True,
                block_height=existing.block_height,
            )

        if self.fail_next_with:
            message, self.fail_next_with = self.fail_next_with, ""
            raise RailError(message)

        amount = Decimal(amount_usdc)
        available = self.balances.get(source, Decimal(0))
        if available < amount:
            raise RailError(f"{source} holds {available} USDC, cannot send {amount}")
        self.balances[source] = available - amount
        self.balances[destination] = self.balances.get(destination, Decimal(0)) + amount

        tx_hash = "0x" + hashlib.sha256(idempotency_key.encode()).hexdigest()
        receipt = TransferReceipt(
            tx_hash=tx_hash,
            state="COMPLETE",
            source=source,
            destination=destination,
            amount_usdc=amount_usdc,
            chain=self.chain,
            explorer_url=f"https://example.invalid/tx/{tx_hash}",
            idempotency_key=idempotency_key,
            block_height=1,
        )
        self._by_key[idempotency_key] = receipt
        self.transfers.append(receipt)
        return receipt

    def balance_usdc(self, address: str) -> str:
        return str(self.balances.get(address, Decimal(0)))

    @property
    def settled_count(self) -> int:
        """Transfers actually sent, excluding deduplicated repeats."""
        return len(self.transfers)
