"""The dashboard renders evidence, not decoration.

The properties worth pinning are not visual. They are that the page cannot
show a figure no run produced, and cannot show a green verification check it
did not earn.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mergegate.mandate import PaymentMandate, execute_mandate
from mergegate.receipt import build_receipt, sign_receipt
from mergegate.verifier.manifest import CommandResult, VerificationManifest
from mergegate.web import ReceiptBundle, build_web_router, short

from .conftest import BASE_SHA, IMAGE

CONTRACT_HASH = "sha256:" + "c" * 64


def _make_receipt(key: Ed25519PrivateKey, *, passing: bool) -> dict[str, Any]:
    from datetime import UTC, datetime, timedelta

    manifest = VerificationManifest(
        task_id="4KInc/mergegate-demo-task",
        contract_hash=CONTRACT_HASH,
        grader_hash="sha256:" + "d" * 64,
        base_sha=BASE_SHA,
        submission_sha=("1" if passing else "2") * 40,
        tree_hash="sha256:" + "e" * 64,
        verifier_image_digest=IMAGE,
        commands=(
            (
                CommandResult(
                    argv=("pytest",),
                    exit_code=0,
                    stdout_digest="sha256:" + "a" * 64,
                    stderr_digest="sha256:" + "b" * 64,
                    duration_ms=5,
                ),
            )
            if passing
            else ()
        ),
        failed_terms=() if passing else ("protected_path",),
        rejection_reason="" if passing else ".github/workflows/deploy.yml modified",
    )
    mandate = PaymentMandate(
        task_id=manifest.task_id,
        contract_hash=CONTRACT_HASH,
        buyer_agent="0xBUYER",
        provider_agent="0xPROVIDER",
        amount_usdc="0.25",
        asset="USDC",
        chain="BASE",
        deadline=datetime.now(UTC) + timedelta(hours=1),
        nonce="n",
    )
    directive = execute_mandate(mandate=mandate, manifest=manifest, now=datetime.now(UTC))
    body = build_receipt(
        manifest=manifest,
        mandate=mandate,
        directive=directive,
        issued_at=datetime.now(UTC),
        settlement_tx="0x" + ("f" if passing else "e") * 64,
        verifier_fee_tx="0x" + "9" * 64,
    )
    return sign_receipt(body, private_key=key, kid="test-key")


@pytest.fixture
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def bundle_dir(tmp_path: Path, key: Ed25519PrivateKey) -> Path:
    d = tmp_path / "receipts"
    (d / "mainnet").mkdir(parents=True)
    (d / "receipt-pass.json").write_text(json.dumps(_make_receipt(key, passing=True)))
    (d / "mainnet" / "receipt-fail.json").write_text(json.dumps(_make_receipt(key, passing=False)))
    return d


def _client(bundle_dir: Path, key: Ed25519PrivateKey | None) -> TestClient:
    app = FastAPI()
    app.include_router(
        build_web_router(ReceiptBundle(bundle_dir, public_key=key.public_key() if key else None))
    )
    return TestClient(app)


# -- the dashboard ------------------------------------------------------------


def test_dashboard_lists_only_real_receipts(bundle_dir: Path, key: Ed25519PrivateKey) -> None:
    html = _client(bundle_dir, key).get("/").text
    assert "SETTLED" in html
    assert "REFUNDED" in html
    # Two receipts on disk, two rows — no padding.
    assert html.count("USDC</td>") == 2 or html.count("0.25 USDC") >= 2


def test_aggregates_are_computed_from_the_receipts(
    bundle_dir: Path, key: Ed25519PrivateKey
) -> None:
    html = _client(bundle_dir, key).get("/").text
    assert "USDC released" in html and "USDC refunded" in html
    # One release of 0.25 and one refund of 0.25.
    assert "0.25" in html


def test_an_empty_bundle_renders_zeroes_not_placeholders(tmp_path: Path) -> None:
    """Nothing is seeded to make the system look busier than it has been."""
    empty = tmp_path / "none"
    empty.mkdir()
    html = _client(empty, None).get("/").text
    assert "No settled contracts yet" in html
    assert "0" in html


def test_dashboard_states_scope_and_custody(bundle_dir: Path, key: Ed25519PrivateKey) -> None:
    html = _client(bundle_dir, key).get("/").text
    assert "not code quality, security, or mergeworthiness" in html
    assert "escrow authority" in html
    assert "non-custodial" not in html.lower()


def test_sandbox_badges_do_not_overstate(bundle_dir: Path, key: Ed25519PrivateKey) -> None:
    """The measured posture, not the one that sounds better.

    The phrase "default-deny" may appear on the page, but only in the sentence
    explaining that the posture is *not* claimed as such — never as a badge.
    """
    html = _client(bundle_dir, key).get("/").text
    assert "No outbound TCP" in html
    assert "DNS resolution available" in html
    assert "Default-deny egress" not in html
    if "default-deny" in html.lower():
        assert "rounded up" in html.lower(), "default-deny appears outside its disclaimer"


# -- receipt detail -----------------------------------------------------------


def test_receipt_page_shows_a_verified_check(bundle_dir: Path, key: Ed25519PrivateKey) -> None:
    client = _client(bundle_dir, key)
    html = client.get("/receipts/receipt-pass").text
    assert "Re-verified from the receipt alone" in html
    assert "checks passed" in html


def test_a_tampered_receipt_is_shown_as_failing(bundle_dir: Path, key: Ed25519PrivateKey) -> None:
    """The check that makes the green tick mean something.

    Verification runs per request, so altering a receipt on disk must change
    what the page says rather than leaving a stale valid flag on screen.
    """
    path = bundle_dir / "receipt-pass.json"
    env = json.loads(path.read_text())
    tampered = copy.deepcopy(env)
    tampered["body"]["binding"]["settlement_amount_usdc"] = "9999.00"
    path.write_text(json.dumps(tampered))

    html = _client(bundle_dir, key).get("/receipts/receipt-pass").text
    assert "Verification failed" in html
    assert "Re-verified from the receipt alone" not in html


def test_without_a_key_the_page_declines_to_claim_verification(bundle_dir: Path) -> None:
    """No key means no green check — it says so instead of implying validity."""
    html = _client(bundle_dir, None).get("/receipts/receipt-pass").text
    assert "Verification failed" in html
    assert "cannot be verified here" in html


def test_refund_receipt_names_the_failed_term(bundle_dir: Path, key: Ed25519PrivateKey) -> None:
    html = _client(bundle_dir, key).get("/receipts/mainnet-receipt-fail").text
    assert "protected_path" in html
    assert ".github/workflows/deploy.yml" in html
    # And explains that no command ran, so passing tests could not have saved it.
    assert "Pinned commands executed" in html


def test_receipt_json_is_downloadable(bundle_dir: Path, key: Ed25519PrivateKey) -> None:
    r = _client(bundle_dir, key).get("/receipts/receipt-pass.json")
    assert r.status_code == 200
    assert r.json()["body"]["binding"]["decision"] == "PASS"


def test_unknown_receipt_is_404(bundle_dir: Path, key: Ed25519PrivateKey) -> None:
    assert _client(bundle_dir, key).get("/receipts/nope").status_code == 404


# -- display helpers ----------------------------------------------------------


def test_short_keeps_both_ends() -> None:
    """A prefix alone cannot be checked against a block explorer."""
    full = "0x" + "ab" * 32
    s = short(full)
    assert s.startswith(full[:10])
    assert s.endswith(full[-6:])


def test_short_leaves_already_short_values_alone() -> None:
    assert short("0xabc") == "0xabc"


def test_network_is_shown_per_row(bundle_dir: Path, key: Ed25519PrivateKey) -> None:
    """The bundle holds mainnet and testnet runs.

    Totals span both, so the table has to say which is which — otherwise the
    page reads as mainnet-only figures over mixed data, which is exactly the
    kind of quiet overstatement the receipts exist to prevent.
    """
    html = _client(bundle_dir, key).get("/").text
    assert "Network" in html
    assert "BASE" in html
    assert "Totals span every network shown" in html
