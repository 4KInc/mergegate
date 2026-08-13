"""P0.7 — the bound, independently-verifiable settlement receipt.

A signed receipt saying "PASS" is not a moat. Anyone can sign a string. The
value is in the **binding**: one object that ties together which code, judged by
which tests, in which environment, under whose mandate, settling which payment —
so that a third party holding only the receipt can re-derive the whole chain and
detect any substitution.

What is bound:

* ``contract_hash`` — the terms, fixed at funding
* ``grader_hash`` — the buyer's test bundle
* ``base_sha`` / ``submission_sha`` / ``tree_hash`` — the exact artifact graded
* ``verifier_image_digest`` — the environment it was graded in
* ``command_output_digest`` / ``result_digest`` — what the run produced
* ``mandate_hash`` — the payment authorization it executes
* ``settlement_key`` — the idempotency key that made it terminal (P0.5)
* ``decision``, ``settlement_tx``, ``verifier_fee_tx`` — the money that moved

The receipt carries the full verification manifest and the mandate alongside
those digests, which is what makes it *self*-verifying: the offline verifier
recomputes ``result_digest`` and ``command_output_digest`` from the manifest it
was given and checks they match the bound values. A receipt whose digests were
edited to describe a different run fails, and so does one whose manifest was
swapped for a friendlier one.

Signing and canonicalization come from the shared engine — MergeGate does not
implement its own crypto.

**What offline verification does and does not establish.** Thirteen of the bound
fields are cross-checked against the embedded manifest and mandate, so editing
any of them fails even for an attacker who holds the signing key —
``tests/test_receipt.py`` proves this by re-signing each tampered variant. Five
fields have nothing inside the receipt to check them against and rest on the
signature alone: ``settlement_tx``, ``verifier_fee_tx``, ``reason``,
``settlement_asset``, and ``settlement_chain``. Confirming those means comparing
the receipt against the chain, which no offline verifier can do. A receipt
proves the decision was the deterministic result of the mandate and the verdict;
confirming the money actually moved requires looking at Base.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .engine import canonicalize
from .hashing import BINDING_DOMAIN, hash_object
from .settlement import settlement_key

__all__ = [
    "ReceiptBinding",
    "ReceiptVerificationResult",
    "build_receipt",
    "sign_receipt",
    "verify_receipt",
]

RECEIPT_SCHEMA_VERSION = "mergegate.receipt/v1"


@dataclass(frozen=True, slots=True)
class ReceiptBinding:
    """Every field the receipt commits to, in one place."""

    task_id: str
    contract_hash: str
    grader_hash: str
    base_sha: str
    submission_sha: str
    tree_hash: str
    verifier_image_digest: str
    command_output_digest: str
    result_digest: str
    mandate_hash: str
    settlement_key: str
    decision: str
    settlement_action: str
    settlement_recipient: str
    settlement_amount_usdc: str
    settlement_asset: str
    settlement_chain: str
    reason: str
    settlement_tx: str = ""
    verifier_fee_tx: str = ""
    """P2.2 — the x402 micro-fee escrow paid the verifier for this run. Empty
    until that path is wired; the field exists so the shape does not change
    later and invalidate earlier receipts' schema."""

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "contract_hash": self.contract_hash,
            "grader_hash": self.grader_hash,
            "base_sha": self.base_sha,
            "submission_sha": self.submission_sha,
            "tree_hash": self.tree_hash,
            "verifier_image_digest": self.verifier_image_digest,
            "command_output_digest": self.command_output_digest,
            "result_digest": self.result_digest,
            "mandate_hash": self.mandate_hash,
            "settlement_key": self.settlement_key,
            "decision": self.decision,
            "settlement_action": self.settlement_action,
            "settlement_recipient": self.settlement_recipient,
            "settlement_amount_usdc": self.settlement_amount_usdc,
            "settlement_asset": self.settlement_asset,
            "settlement_chain": self.settlement_chain,
            "reason": self.reason,
            "settlement_tx": self.settlement_tx,
            "verifier_fee_tx": self.verifier_fee_tx,
        }

    @property
    def binding_hash(self) -> str:
        return hash_object(BINDING_DOMAIN, self.to_canonical_dict())


def build_receipt(
    *,
    manifest: Any,
    mandate: Any,
    directive: Any,
    issued_at: datetime,
    settlement_tx: str = "",
    verifier_fee_tx: str = "",
) -> dict[str, Any]:
    """Assemble the unsigned receipt body from a completed evaluation.

    The manifest and mandate are embedded whole, not summarized. A receipt that
    only carried digests could be verified for internal consistency but could
    not be *re-derived* — the holder would have to trust that the digests
    described what someone said they described.
    """
    binding = ReceiptBinding(
        task_id=manifest.task_id,
        contract_hash=manifest.contract_hash,
        grader_hash=manifest.grader_hash,
        base_sha=manifest.base_sha,
        submission_sha=manifest.submission_sha,
        tree_hash=manifest.tree_hash,
        verifier_image_digest=manifest.verifier_image_digest,
        command_output_digest=manifest.command_output_digest,
        result_digest=manifest.result_digest,
        mandate_hash=mandate.mandate_hash,
        settlement_key=settlement_key(
            task_id=manifest.task_id,
            submission_sha=manifest.submission_sha,
            contract_hash=manifest.contract_hash,
            terminal_verdict=manifest.verdict.value,
        ),
        decision=manifest.verdict.value,
        settlement_action=directive.action.value,
        settlement_recipient=directive.recipient,
        settlement_amount_usdc=directive.amount_usdc,
        settlement_asset=directive.asset,
        settlement_chain=directive.chain,
        reason=directive.reason,
        settlement_tx=settlement_tx,
        verifier_fee_tx=verifier_fee_tx,
    )

    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "issued_at": issued_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "binding": binding.to_canonical_dict(),
        "manifest": manifest.to_canonical_dict(),
        "mandate": mandate.to_canonical_dict(),
        "mandate_statement": mandate.statement(),
        "scope": (
            "Attests verified contract acceptance only — not code quality, "
            "security, or mergeworthiness."
        ),
        "custody": (
            "Programmable USDC escrow with policy-bound conditional settlement. "
            "MergeGate holds escrow authority."
        ),
    }


def sign_receipt(
    body: dict[str, Any], *, private_key: Ed25519PrivateKey, kid: str
) -> dict[str, Any]:
    """Sign a receipt body, producing the envelope the shared engine's format uses."""
    payload = canonicalize(body)
    signature = private_key.sign(payload)
    import hashlib

    return {
        "body": body,
        "sig": {
            "alg": "EdDSA",
            "kid": kid,
            "value": base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii"),
        },
        "receipt_hash": "sha256:" + hashlib.sha256(payload).hexdigest(),
    }


@dataclass(frozen=True, slots=True)
class ReceiptVerificationResult:
    valid: bool
    checks: tuple[tuple[str, bool], ...]
    failures: tuple[str, ...]

    def summary(self) -> str:
        if self.valid:
            return f"receipt verified — {len(self.checks)} checks passed"
        return "receipt FAILED verification: " + "; ".join(self.failures)


def verify_receipt(
    envelope: dict[str, Any], *, public_key: Ed25519PublicKey
) -> ReceiptVerificationResult:
    """Re-check the full chain from the receipt alone.

    Every check is run even after one fails, so a caller debugging a bad receipt
    learns everything that is wrong with it rather than only the first thing.
    """
    checks: list[tuple[str, bool]] = []
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok))
        if not ok:
            failures.append(detail or name)

    body = envelope.get("body")
    if not isinstance(body, dict):
        return ReceiptVerificationResult(False, (("envelope_shape", False),), ("no receipt body",))

    payload = canonicalize(body)

    # 1. Hash integrity.
    import hashlib

    expected_hash = "sha256:" + hashlib.sha256(payload).hexdigest()
    check(
        "receipt_hash",
        envelope.get("receipt_hash") == expected_hash,
        f"receipt_hash mismatch: envelope says {envelope.get('receipt_hash')}, "
        f"body hashes to {expected_hash}",
    )

    # 2. Signature over the canonical body.
    sig = envelope.get("sig") or {}
    try:
        raw = _b64url_decode(str(sig.get("value", "")))
        public_key.verify(raw, payload)
        check("signature", True)
    except (InvalidSignature, ValueError, TypeError):
        check("signature", False, "Ed25519 signature does not verify over the canonical body")

    binding = body.get("binding") or {}
    manifest = body.get("manifest") or {}
    mandate = body.get("mandate") or {}

    # 3. The manifest actually produces the bound result digest. This is the
    #    check that makes swapping the manifest detectable.
    from .hashing import OUTPUT_DOMAIN, RESULT_DOMAIN, digest

    recomputed_result = hash_object(RESULT_DOMAIN, manifest)
    check(
        "result_digest",
        binding.get("result_digest") == recomputed_result,
        f"result_digest does not match the embedded manifest "
        f"(bound {binding.get('result_digest')}, manifest yields {recomputed_result})",
    )

    recomputed_output = digest(
        OUTPUT_DOMAIN,
        canonicalize(
            [
                {
                    "argv": cmd.get("argv", []),
                    "stdout_digest": cmd.get("stdout_digest", ""),
                    "stderr_digest": cmd.get("stderr_digest", ""),
                }
                for cmd in manifest.get("commands", [])
            ]
        ),
    )
    check(
        "command_output_digest",
        binding.get("command_output_digest") == recomputed_output,
        "command_output_digest does not match the embedded command records",
    )

    # 4. The binding and the manifest agree about which artifact was graded.
    for name in (
        "contract_hash",
        "grader_hash",
        "base_sha",
        "submission_sha",
        "tree_hash",
        "verifier_image_digest",
        "task_id",
    ):
        check(
            f"binding_matches_manifest.{name}",
            binding.get(name) == manifest.get(name),
            f"binding {name} ({binding.get(name)}) disagrees with the manifest "
            f"({manifest.get(name)})",
        )

    check(
        "decision_matches_manifest",
        binding.get("decision") == manifest.get("verdict"),
        f"binding decision {binding.get('decision')} disagrees with manifest verdict "
        f"{manifest.get('verdict')}",
    )

    # 5. The mandate the receipt embeds is the one it claims to execute.
    from .hashing import MANDATE_DOMAIN

    recomputed_mandate = hash_object(MANDATE_DOMAIN, mandate)
    check(
        "mandate_hash",
        binding.get("mandate_hash") == recomputed_mandate,
        "mandate_hash does not match the embedded mandate",
    )
    check(
        "mandate_covers_this_contract",
        mandate.get("contract_hash") == binding.get("contract_hash"),
        "the embedded mandate authorizes a different contract than the one graded",
    )

    # 6. The settlement key derives from the bound fields (P0.5).
    recomputed_key = settlement_key(
        task_id=str(binding.get("task_id", "")),
        submission_sha=str(binding.get("submission_sha", "")),
        contract_hash=str(binding.get("contract_hash", "")),
        terminal_verdict=str(binding.get("decision", "")),
    )
    check(
        "settlement_key",
        binding.get("settlement_key") == recomputed_key,
        "settlement_key does not derive from the bound task, artifact, contract, and verdict",
    )

    # 7. A release must pay the provider named in the mandate, and a refund the
    #    buyer. A receipt describing a payment to anyone else is not valid
    #    regardless of how well it is signed.
    action = binding.get("settlement_action")
    if action == "release":
        check(
            "recipient_matches_mandate",
            binding.get("settlement_recipient") == mandate.get("provider_agent"),
            "release does not pay the provider named in the mandate",
        )
        check(
            "release_requires_pass",
            binding.get("decision") == "PASS",
            "escrow released on a non-PASS decision",
        )
    elif action == "refund":
        check(
            "recipient_matches_mandate",
            binding.get("settlement_recipient") == mandate.get("buyer_agent"),
            "refund does not return funds to the buyer named in the mandate",
        )
    else:
        check("settlement_action", False, f"unknown settlement action {action!r}")

    check(
        "amount_matches_mandate",
        binding.get("settlement_amount_usdc") == mandate.get("amount_usdc"),
        f"settled amount {binding.get('settlement_amount_usdc')} is not the mandated "
        f"{mandate.get('amount_usdc')}",
    )

    return ReceiptVerificationResult(
        valid=not failures,
        checks=tuple(checks),
        failures=tuple(failures),
    )


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
