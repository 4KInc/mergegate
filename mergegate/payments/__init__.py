"""Settlement rails.

MergeGate settles through Circle **agent wallets** driven by the ``circle`` CLI,
not the REST Developer-Controlled Wallets API. The two are separate products
holding separate wallets; the funded Base mainnet addresses live in the former.

Everything reaches the chain through :class:`~mergegate.payments.base.SettlementRail`,
so swapping rails — REST, or a deployed escrow contract that would let MergeGate
drop the custody disclosure — is a change of one class.
"""

from __future__ import annotations

from .base import RailError, SettlementRail, TransferReceipt
from .circle_cli import CircleCliRail, resolve_circle_binary
from .executor import ExecutedSettlement, SettlementExecutor
from .fake import FakeRail

__all__ = [
    "CircleCliRail",
    "ExecutedSettlement",
    "FakeRail",
    "RailError",
    "SettlementExecutor",
    "SettlementRail",
    "TransferReceipt",
    "resolve_circle_binary",
]
