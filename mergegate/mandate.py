"""P0.1 / P0.6: the buyer's conditional payment mandate, and its execution.

The mandate is the payment decision, and the buyer agent makes it **at funding
time**:

    pay exactly X USDC to provider Y if and only if contract C evaluates PASS
    before deadline T

Everything afterwards is execution, not judgement. :func:`execute_mandate` is a
total function of the mandate and the verification manifest: same inputs, same
directive, every time. It consults no model, no operator, and no clock beyond
the deadline comparison it is handed. That is what "no LLM in the
payment-authority path" means concretely: there is no point in this module where
a decision could be inserted.

The mandate binds ``contract_hash``, so it authorizes payment against exactly
one set of terms. A contract that changed after funding no longer hashes to what
the buyer signed, and the mandate simply does not apply to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from .hashing import MANDATE_DOMAIN, hash_object

__all__ = [
    "MandateError",
    "PaymentMandate",
    "SettlementAction",
    "SettlementDirective",
    "execute_mandate",
]

MANDATE_SCHEMA_VERSION = "mergegate.mandate/v1"


class MandateError(ValueError):
    """The mandate does not apply to what it is being executed against."""


class SettlementAction(StrEnum):
    RELEASE = "release"
    """Escrow pays the provider. The contract evaluated PASS in time."""

    REFUND = "refund"
    """Escrow returns to the buyer. Every non-PASS outcome lands here."""


@dataclass(frozen=True, slots=True)
class PaymentMandate:
    """A pre-signed conditional authorization to move escrowed funds."""

    task_id: str
    contract_hash: str
    buyer_agent: str
    provider_agent: str
    amount_usdc: str
    asset: str
    chain: str
    deadline: datetime
    nonce: str
    """Distinguishes two otherwise-identical mandates. Without it, re-funding a
    task on identical terms would produce a colliding mandate hash."""

    schema_version: str = MANDATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.deadline.tzinfo is None:
            raise MandateError("mandate deadline must be timezone-aware")
        if not self.nonce:
            raise MandateError("mandate requires a nonce")

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "contract_hash": self.contract_hash,
            "buyer_agent": self.buyer_agent,
            "provider_agent": self.provider_agent,
            "amount_usdc": self.amount_usdc,
            "asset": self.asset,
            "chain": self.chain,
            "deadline": self.deadline.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "nonce": self.nonce,
        }

    @property
    def mandate_hash(self) -> str:
        return hash_object(MANDATE_DOMAIN, self.to_canonical_dict())

    def statement(self) -> str:
        """The mandate in the words the dashboard and receipt display.

        Generated from the fields rather than written alongside them, so the
        prose cannot drift from what was actually authorized.
        """
        return (
            f"pay exactly {self.amount_usdc} {self.asset} to {self.provider_agent} "
            f"if and only if contract {self.contract_hash} evaluates PASS "
            f"before {self.deadline.astimezone(UTC).isoformat().replace('+00:00', 'Z')}"
        )


@dataclass(frozen=True, slots=True)
class SettlementDirective:
    """What the mandate says to do, and why. Not a recommendation."""

    action: SettlementAction
    recipient: str
    amount_usdc: str
    asset: str
    chain: str
    reason: str
    """Names the specific condition that decided it. The refund receipt quotes
    this, so it must identify the failed term, not merely report failure."""

    mandate_hash: str
    contract_hash: str
    submission_sha: str


def execute_mandate(
    *,
    mandate: PaymentMandate,
    manifest: object,
    now: datetime,
) -> SettlementDirective:
    """Evaluate the pre-signed mandate against a verification result.

    ``manifest`` is a :class:`~mergegate.verifier.manifest.VerificationManifest`;
    it is typed loosely here only to keep the payment layer from importing the
    verifier package.

    Raises :class:`MandateError` when the mandate does not apply to this
    manifest at all: a hash mismatch is an integrity failure of the caller's
    own inputs, and must not be silently downgraded to a refund.
    """
    contract_hash = getattr(manifest, "contract_hash", "")
    submission_sha = getattr(manifest, "submission_sha", "")
    task_id = getattr(manifest, "task_id", "")

    if contract_hash != mandate.contract_hash:
        raise MandateError(
            f"mandate authorizes payment against contract {mandate.contract_hash}, "
            f"but the verification result is for {contract_hash}. The mandate does "
            "not apply."
        )
    if task_id != mandate.task_id:
        raise MandateError(f"mandate is for task {mandate.task_id}, result is for task {task_id}")
    if now.tzinfo is None:
        raise MandateError("evaluation time must be timezone-aware")

    def refund(reason: str) -> SettlementDirective:
        return SettlementDirective(
            action=SettlementAction.REFUND,
            recipient=mandate.buyer_agent,
            amount_usdc=mandate.amount_usdc,
            asset=mandate.asset,
            chain=mandate.chain,
            reason=reason,
            mandate_hash=mandate.mandate_hash,
            contract_hash=mandate.contract_hash,
            submission_sha=submission_sha,
        )

    # Deadline first: a PASS that arrives late still fails the condition, which
    # said "before T". Evaluating the verdict first and the clock second would
    # make a late pass releasable.
    if now > mandate.deadline:
        return refund(
            f"deadline passed: mandate required PASS before "
            f"{mandate.deadline.astimezone(UTC).isoformat()}"
        )

    verdict = getattr(manifest, "verdict", None)
    verdict_value = getattr(verdict, "value", str(verdict))

    if verdict_value != "PASS":
        failed_terms = tuple(getattr(manifest, "failed_terms", ()) or ())
        rejection = str(getattr(manifest, "rejection_reason", "") or "")
        if rejection:
            return refund(f"contract evaluated FAIL: {rejection}")
        if failed_terms:
            return refund(f"contract evaluated FAIL, failed terms: {', '.join(failed_terms)}")
        return refund("contract evaluated FAIL: pinned commands did not all succeed")

    return SettlementDirective(
        action=SettlementAction.RELEASE,
        recipient=mandate.provider_agent,
        amount_usdc=mandate.amount_usdc,
        asset=mandate.asset,
        chain=mandate.chain,
        reason="contract evaluated PASS before the deadline",
        mandate_hash=mandate.mandate_hash,
        contract_hash=mandate.contract_hash,
        submission_sha=submission_sha,
    )
