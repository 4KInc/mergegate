"""Executing a settlement directive against a rail.

The executor is deliberately dumb. It receives a
:class:`~mergegate.mandate.SettlementDirective` that already says who gets paid
and why, and its only job is to move the funds and report what happened. There
is no branch in here that could decide differently, that decision was made by
the buyer at funding time and evaluated by :func:`mergegate.mandate.execute_mandate`.

The settlement key becomes the rail's idempotency key, which is what gives
double-payment protection two independent layers: the state machine refuses a
second settlement, and the rail refuses a second transfer for the same key. They
fail independently, which is the point.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..mandate import SettlementAction, SettlementDirective
from .base import RailError, SettlementRail, TransferReceipt

__all__ = ["SettlementExecutor", "ExecutedSettlement"]


@dataclass(frozen=True, slots=True)
class ExecutedSettlement:
    directive: SettlementDirective
    transfer: TransferReceipt
    verifier_fee: TransferReceipt | None = None

    @property
    def settlement_tx(self) -> str:
        return self.transfer.tx_hash

    @property
    def verifier_fee_tx(self) -> str:
        return self.verifier_fee.tx_hash if self.verifier_fee else ""


@dataclass(frozen=True, slots=True)
class SettlementExecutor:
    """Moves escrowed USDC according to a directive."""

    rail: SettlementRail
    escrow_address: str
    verifier_fee_address: str = ""
    verifier_fee_usdc: str = ""

    def execute(self, directive: SettlementDirective, *, settlement_key: str) -> ExecutedSettlement:
        """Pay out the directive, then the verifier fee.

        Ordering matters. The settlement is what the buyer authorized and what
        the provider is owed; the verifier fee is MergeGate's own charge. If the
        fee transfer fails, the settlement has still happened correctly and the
        receipt records an empty fee tx: the reverse ordering would risk taking
        our fee out of an escrow that then failed to pay the counterparty.
        """
        if not settlement_key:
            raise RailError("a settlement must carry its settlement key as the idempotency key")

        if directive.action is SettlementAction.RELEASE:
            destination = directive.recipient
        else:
            destination = directive.recipient

        transfer = self.rail.transfer(
            source=self.escrow_address,
            destination=destination,
            amount_usdc=directive.amount_usdc,
            idempotency_key=settlement_key,
        )

        fee: TransferReceipt | None = None
        if self.verifier_fee_address and self.verifier_fee_usdc:
            if self.verifier_fee_address == destination:
                # Paying the fee to the same address that just received the
                # settlement makes "escrow pays the verifier service" circular:
                # two transfers, one beneficiary. Skip rather than produce a
                # receipt that overstates what happened.
                fee = None
            else:
                try:
                    fee = self.rail.transfer(
                        source=self.escrow_address,
                        destination=self.verifier_fee_address,
                        amount_usdc=self.verifier_fee_usdc,
                        idempotency_key=f"{settlement_key}:verifier-fee",
                    )
                except RailError:
                    # The settlement already succeeded. A failed fee is a
                    # MergeGate revenue problem, not a settlement failure, and
                    # must not be reported as one.
                    fee = None

        return ExecutedSettlement(directive=directive, transfer=transfer, verifier_fee=fee)
