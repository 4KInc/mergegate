"""P2.2 (in progress): the verifier as an x402-priced service.

The verifier fee has been a plain USDC transfer. x402 makes the verifier a
*service that must be paid to be invoked*, which is the shape Circle's stack is
built around: a caller requests the endpoint, receives HTTP 402 with machine
readable payment requirements, pays, and retries.

This module implements the **challenge** half, which is what makes the endpoint
discoverable and priced. The wire format is taken from a live x402 v2 service
listed by ``circle services search``, not from guesswork:

    {"x402Version": 2,
     "accepts": [{"scheme": "exact", "network": "eip155:8453",
                  "asset": "<usdc>", "payTo": "<addr>", "amount": "50000", ...}]}

``amount`` is in the asset's own units, so 6-decimal USDC means 50000 is 0.05.

**Verified against Circle's own client.** ``circle services inspect`` reports
this endpoint as ``"status": "payable"`` at ``$0.05 USDC`` on Base with the
correct seller, and a real ``circle services pay`` produced a signed EIP-3009
``transferWithAuthorization`` naming this endpoint as the resource. So the
challenge half is not merely plausible, it is readable by an off-the-shelf x402
payer.

**What this module does not do, stated plainly.** It does not settle. In the
``exact`` scheme the payer *signs* an authorization and the server submits it;
MergeGate does not, so no funds move. That was confirmed rather than assumed:
after a real paid call, both the payer and payee balances were unchanged.

Settlement needs a relayer holding gas to call ``transferWithAuthorization`` on
the USDC contract, and every wallet here holds 0 ETH on Base. That is the whole
gap, and it is an operational one rather than a design one.

Until it closes, presenting a payment gets 402 again with an explicit reason.
Answering 200 would claim a fee that never moved, which is worse than charging
nothing, and the receipt continues to bind the plain USDC transfer that does.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

__all__ = ["X402Price", "payment_requirements", "USDC_DECIMALS"]

USDC_DECIMALS = 6
X402_VERSION = 2


@dataclass(frozen=True, slots=True)
class X402Price:
    """What the verifier charges to run one evaluation."""

    pay_to: str
    asset: str
    amount_usdc: str
    chain_id: int = 8453
    description: str = "MergeGate deterministic evaluation of one submission"

    @property
    def network(self) -> str:
        """CAIP-2 identifier, which is how x402 names the chain."""
        return f"eip155:{self.chain_id}"

    @property
    def amount_units(self) -> str:
        """Amount in the asset's smallest unit.

        Decimal, never float: a fee expressed as a float could round to an
        amount nobody priced.
        """
        return str(int(Decimal(self.amount_usdc) * (10**USDC_DECIMALS)))


def payment_requirements(price: X402Price, resource: str) -> dict[str, Any]:
    """The body served with HTTP 402.

    Shaped to match a live x402 v2 listing so an off-the-shelf client can read
    it without special-casing MergeGate.
    """
    return {
        "x402Version": X402_VERSION,
        "error": "payment required",
        "accepts": [
            {
                "scheme": "exact",
                "network": price.network,
                "asset": price.asset,
                "payTo": price.pay_to,
                "amount": price.amount_units,
                "maxTimeoutSeconds": 120,
                "resource": resource,
                "description": price.description,
                "mimeType": "application/json",
                "extra": {"name": "USD Coin", "version": "2"},
            }
        ],
    }
