"""P0.7: the bound receipt verifies offline, and tampering breaks it.

The point of these tests is not that a signature checks out. It is that the
receipt cannot be edited into describing a different run, a different artifact,
a different mandate, or a different payment while still verifying: including
edits that keep the object internally plausible.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mergegate.mandate import PaymentMandate, execute_mandate
from mergegate.receipt import build_receipt, sign_receipt, verify_receipt
from mergegate.verifier.manifest import CommandResult, VerificationManifest

from .conftest import BASE_SHA, IMAGE

CONTRACT_HASH = "sha256:" + "c" * 64
SHA = "1" * 40


@pytest.fixture
def signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _manifest(*, passing: bool = True) -> VerificationManifest:
    return VerificationManifest(
        task_id="task-001",
        contract_hash=CONTRACT_HASH,
        grader_hash="sha256:" + "d" * 64,
        base_sha=BASE_SHA,
        submission_sha=SHA,
        tree_hash="sha256:" + "e" * 64,
        verifier_image_digest=IMAGE,
        commands=(
            CommandResult(
                argv=("pytest", "-q"),
                exit_code=0 if passing else 1,
                stdout_digest="sha256:" + "a" * 64,
                stderr_digest="sha256:" + "b" * 64,
                duration_ms=1200,
            ),
        ),
        failed_terms=() if passing else ("protected_path",),
        rejection_reason="" if passing else ".github/workflows/deploy.yml modified",
    )


def _mandate() -> PaymentMandate:
    return PaymentMandate(
        task_id="task-001",
        contract_hash=CONTRACT_HASH,
        buyer_agent="0xBUYER",
        provider_agent="0xPROVIDER",
        amount_usdc="250.00",
        asset="USDC",
        chain="base",
        deadline=datetime.now(UTC) + timedelta(hours=6),
        nonce="nonce-1",
    )


def _receipt(
    key: Ed25519PrivateKey, *, passing: bool = True, settlement_tx: str = "0xsettle"
) -> dict[str, Any]:
    manifest = _manifest(passing=passing)
    mandate = _mandate()
    directive = execute_mandate(mandate=mandate, manifest=manifest, now=datetime.now(UTC))
    body = build_receipt(
        manifest=manifest,
        mandate=mandate,
        directive=directive,
        issued_at=datetime.now(UTC),
        settlement_tx=settlement_tx,
        verifier_fee_tx="0xfee",
    )
    return sign_receipt(body, private_key=key, kid="mergegate-test-key")


# -- the receipt verifies from itself alone -----------------------------------


def test_pass_receipt_verifies_offline(signing_key: Ed25519PrivateKey) -> None:
    """P0.7 done-when: an offline verifier re-checks the full chain and passes."""
    envelope = _receipt(signing_key)
    result = verify_receipt(envelope, public_key=signing_key.public_key())
    assert result.valid, result.summary()
    assert len(result.checks) > 10


def test_refund_receipt_names_the_failed_term(signing_key: Ed25519PrivateKey) -> None:
    """P2.1: the refund receipt must name the exact failed contract term."""
    envelope = _receipt(signing_key, passing=False)
    result = verify_receipt(envelope, public_key=signing_key.public_key())
    assert result.valid, result.summary()

    binding = envelope["body"]["binding"]
    assert binding["decision"] == "FAIL"
    assert binding["settlement_action"] == "refund"
    assert binding["settlement_recipient"] == "0xBUYER"
    assert ".github/workflows/deploy.yml" in binding["reason"]


def test_receipt_survives_a_json_round_trip(signing_key: Ed25519PrivateKey) -> None:
    """A receipt is only useful if it verifies after being written and read."""
    envelope = _receipt(signing_key)
    reloaded = json.loads(json.dumps(envelope))
    assert verify_receipt(reloaded, public_key=signing_key.public_key()).valid


def test_receipt_binds_every_required_field(signing_key: Ed25519PrivateKey) -> None:
    binding = _receipt(signing_key)["body"]["binding"]
    for field in (
        "contract_hash",
        "grader_hash",
        "base_sha",
        "submission_sha",
        "tree_hash",
        "verifier_image_digest",
        "command_output_digest",
        "result_digest",
        "mandate_hash",
        "settlement_key",
        "decision",
        "settlement_tx",
        "verifier_fee_tx",
    ):
        assert binding.get(field), f"{field} is not bound into the receipt"


def test_receipt_states_its_scope_and_custody(signing_key: Ed25519PrivateKey) -> None:
    """P2.3 / P2.4: the claim limits travel with the artifact."""
    body = _receipt(signing_key)["body"]
    assert "not code quality, security, or mergeworthiness" in body["scope"]
    assert "escrow authority" in body["custody"]
    assert "non-custodial" not in json.dumps(body).lower()


# -- tampering ----------------------------------------------------------------


def test_a_wrong_public_key_fails(signing_key: Ed25519PrivateKey) -> None:
    envelope = _receipt(signing_key)
    other = Ed25519PrivateKey.generate()
    result = verify_receipt(envelope, public_key=other.public_key())
    assert not result.valid
    assert any("signature" in f for f in result.failures)


@pytest.mark.parametrize(
    "field",
    [
        "contract_hash",
        "grader_hash",
        "base_sha",
        "submission_sha",
        "tree_hash",
        "verifier_image_digest",
        "result_digest",
        "command_output_digest",
        "mandate_hash",
        "settlement_key",
        "decision",
        "settlement_amount_usdc",
        "settlement_recipient",
    ],
)
def test_tampering_with_any_bound_field_fails(signing_key: Ed25519PrivateKey, field: str) -> None:
    """P0.7 done-when: a tampered field fails verification."""
    envelope = _receipt(signing_key)
    tampered = copy.deepcopy(envelope)
    tampered["body"]["binding"][field] = "sha256:" + "9" * 64
    result = verify_receipt(tampered, public_key=signing_key.public_key())
    assert not result.valid, f"tampering with {field} was not detected"


@pytest.mark.parametrize(
    "field,value",
    [
        ("contract_hash", "sha256:" + "9" * 64),
        ("grader_hash", "sha256:" + "9" * 64),
        ("base_sha", "9" * 40),
        ("submission_sha", "9" * 40),
        ("tree_hash", "sha256:" + "9" * 64),
        ("verifier_image_digest", "evil/image@sha256:" + "9" * 64),
        ("result_digest", "sha256:" + "9" * 64),
        ("command_output_digest", "sha256:" + "9" * 64),
        ("mandate_hash", "sha256:" + "9" * 64),
        ("settlement_key", "sha256:" + "9" * 64),
        ("decision", "PASS"),
        ("settlement_recipient", "0xATTACKER"),
        ("settlement_amount_usdc", "9999.00"),
    ],
)
def test_bound_fields_are_caught_even_by_an_attacker_holding_the_key(
    field: str, value: str
) -> None:
    """The signature is not what makes these fields safe.

     Each field below is cross-checked against the embedded manifest or mandate,
     so editing it and re-signing still fails. Without this test, the tampering
     cases above would only be proving that Ed25519 works.

     Fields deliberately *not* covered here: ``settlement_tx``,
     ``verifier_fee_tx``, ``reason``, ``settlement_asset``, ``settlement_chain``
    : have nothing inside the receipt to check them against and rest on the
     signature alone. Confirming those means comparing the receipt to the chain,
     which is outside what an offline verifier can do.
    """
    key = Ed25519PrivateKey.generate()
    envelope = _receipt(key, passing=False)

    body = copy.deepcopy(envelope["body"])
    body["binding"][field] = value
    resigned = sign_receipt(body, private_key=key, kid="mergegate-test-key")

    result = verify_receipt(resigned, public_key=key.public_key())
    assert any(name == "signature" and ok for name, ok in result.checks)
    assert not result.valid, f"{field} is not independently bound: signature is its only guard"


def test_swapping_the_manifest_is_detected(signing_key: Ed25519PrivateKey) -> None:
    """The digests must actually describe the embedded run.

    Without recomputing result_digest from the manifest, a receipt could carry
    a friendly-looking manifest that has nothing to do with the bound digests.
    """
    envelope = _receipt(signing_key, passing=False)
    tampered = copy.deepcopy(envelope)
    tampered["body"]["manifest"] = _manifest(passing=True).to_canonical_dict()

    result = verify_receipt(tampered, public_key=signing_key.public_key())
    assert not result.valid
    assert any("result_digest" in f for f in result.failures)


def test_flipping_the_verdict_to_release_is_detected(
    signing_key: Ed25519PrivateKey,
) -> None:
    """The most valuable forgery: turn a refund into a release."""
    envelope = _receipt(signing_key, passing=False)
    tampered = copy.deepcopy(envelope)
    binding = tampered["body"]["binding"]
    binding["decision"] = "PASS"
    binding["settlement_action"] = "release"
    binding["settlement_recipient"] = "0xPROVIDER"

    result = verify_receipt(tampered, public_key=signing_key.public_key())
    assert not result.valid


def test_redirecting_payment_is_detected(signing_key: Ed25519PrivateKey) -> None:
    """A receipt paying someone the mandate never named is invalid."""
    envelope = _receipt(signing_key)
    tampered = copy.deepcopy(envelope)
    tampered["body"]["binding"]["settlement_recipient"] = "0xATTACKER"

    result = verify_receipt(tampered, public_key=signing_key.public_key())
    assert not result.valid


def test_swapping_the_mandate_is_detected(signing_key: Ed25519PrivateKey) -> None:
    envelope = _receipt(signing_key)
    tampered = copy.deepcopy(envelope)
    tampered["body"]["mandate"]["amount_usdc"] = "9999.00"

    result = verify_receipt(tampered, public_key=signing_key.public_key())
    assert not result.valid
    assert any("mandate_hash" in f for f in result.failures)


def test_a_resigned_tampered_receipt_still_fails_internal_consistency() -> None:
    """The strongest case: an attacker who holds the signing key.

    A valid signature over an inconsistent body is still an invalid receipt.
    The binding checks do not depend on the signature being wrong.
    """
    key = Ed25519PrivateKey.generate()
    envelope = _receipt(key, passing=False)

    body = copy.deepcopy(envelope["body"])
    body["binding"]["decision"] = "PASS"
    body["binding"]["settlement_action"] = "release"
    body["binding"]["settlement_recipient"] = "0xPROVIDER"
    resigned = sign_receipt(body, private_key=key, kid="mergegate-test-key")

    result = verify_receipt(resigned, public_key=key.public_key())
    assert not result.valid
    assert any("signature" in name and ok for name, ok in result.checks)
    assert any("decision" in f or "settlement_key" in f for f in result.failures)


def test_receipt_hash_tampering_is_detected(signing_key: Ed25519PrivateKey) -> None:
    envelope = _receipt(signing_key)
    tampered = copy.deepcopy(envelope)
    tampered["receipt_hash"] = "sha256:" + "0" * 64
    result = verify_receipt(tampered, public_key=signing_key.public_key())
    assert not result.valid
    assert any("receipt_hash" in f for f in result.failures)


def test_verifier_reports_every_failure_not_just_the_first(
    signing_key: Ed25519PrivateKey,
) -> None:
    """Debugging a bad receipt should not be one round trip per problem."""
    envelope = _receipt(signing_key)
    tampered = copy.deepcopy(envelope)
    tampered["body"]["binding"]["tree_hash"] = "sha256:" + "9" * 64
    tampered["body"]["binding"]["grader_hash"] = "sha256:" + "8" * 64

    result = verify_receipt(tampered, public_key=signing_key.public_key())
    assert len(result.failures) >= 2
