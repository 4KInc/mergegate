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
from mergegate.verifier.sandbox import EGRESS_PROBE
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
    # Two receipts on disk, two rows: no padding.
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
    explaining that the posture is *not* claimed as such: never as a badge.
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
    """No key means no green check; it says so instead of implying validity."""
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

    Totals span both, so the table has to say which is which: otherwise the
    page reads as mainnet-only figures over mixed data, which is exactly the
    kind of quiet overstatement the receipts exist to prevent.
    """
    html = _client(bundle_dir, key).get("/").text
    assert "Network" in html
    assert "BASE" in html
    assert "Totals span every network shown" in html


# -- live source ---------------------------------------------------------------


def test_bundle_reads_from_an_injected_source(key: Ed25519PrivateKey) -> None:
    """The dashboard is source-agnostic: Firestore in deployment, memory here."""
    from mergegate.store import MemoryReceiptStore

    store = MemoryReceiptStore()
    store.put("live-1", _make_receipt(key, passing=True))
    bundle = ReceiptBundle(public_key=key.public_key(), source=store)

    views = bundle.all()
    assert [v.id for v in views] == ["live-1"]
    assert bundle.verify(views[0])["valid"]


def test_a_failing_source_is_reported_not_swallowed(key: Ed25519PrivateKey) -> None:
    """An unreachable datastore and an empty system look identical on screen
    unless the failure is surfaced. They mean very different things."""

    class Broken:
        def all(self) -> Any:
            raise RuntimeError("firestore unavailable")

        def get(self, receipt_id: str) -> Any:
            raise RuntimeError("firestore unavailable")

    bundle = ReceiptBundle(public_key=key.public_key(), source=Broken())
    assert bundle.all() == []
    assert "firestore unavailable" in bundle.source_error

    app = FastAPI()
    app.include_router(build_web_router(bundle))
    html = TestClient(app).get("/").text
    assert "Receipt store unreachable" in html
    # The page must distinguish "could not read" from "nothing has settled".
    assert "Those are different things" in html
    assert "firestore unavailable" in html


def test_firestore_round_trip_preserves_signature_bytes(key: Ed25519PrivateKey) -> None:
    """Receipts are stored as one opaque JSON string on purpose.

    Spread across native fields, Firestore could reorder maps or coerce
    numbers, and the signature is over exact canonical bytes: the receipt
    would come back subtly different and stop verifying.
    """
    from mergegate.receipt import verify_receipt as _verify
    from mergegate.store import FirestoreReceiptStore

    envelope = _make_receipt(key, passing=True)
    captured: dict[str, Any] = {}

    class FakeDoc:
        def set(self, payload: dict[str, Any]) -> None:
            captured.update(payload)

    class FakeCollection:
        def document(self, _id: str) -> FakeDoc:
            return FakeDoc()

    class FakeDb:
        def collection(self, _name: str) -> FakeCollection:
            return FakeCollection()

    store = FirestoreReceiptStore(FakeDb())
    store.put("r1", envelope)

    restored = json.loads(captured["envelope_json"])
    assert _verify(restored, public_key=key.public_key()).valid


# -- the verifier page ---------------------------------------------------------


def test_verifier_page_shows_the_measured_probe(
    bundle_dir: Path, key: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page reports what the probe returned, including the failure it found.

    The first configuration reached the internet. Showing only the fixed state
    would present the guarantee as though it had always held.
    """
    monkeypatch.setenv("VERIFIER_IMAGE_DIGEST", IMAGE)
    html = _client(bundle_dir, key).get("/verifier").text

    assert "1.1.1.1:443" in html
    assert "loopback (control)" in html
    assert "reachable" in html and "blocked" in html

    # Both measured exit codes, asserted as values rather than as a sentence:
    # the layout moved them into a featured panel and the previous check was
    # pinned to the wording around them, not to the measurement itself.
    assert str(EGRESS_PROBE["before_exit_code"]) in html
    assert str(EGRESS_PROBE["after_exit_code"]) in html
    assert "Default Cloud Run" in html and "Sealed VPC" in html


def test_verifier_page_states_the_egress_claim_exactly(
    bundle_dir: Path, key: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mergegate.verifier.sandbox import EGRESS_DENY_TCP

    monkeypatch.setenv("VERIFIER_IMAGE_DIGEST", IMAGE)
    html = _client(bundle_dir, key).get("/verifier").text
    assert EGRESS_DENY_TCP in html
    assert "Default-deny egress" not in html


def test_verifier_page_shows_the_pinned_image(
    bundle_dir: Path, key: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VERIFIER_IMAGE_DIGEST", IMAGE)
    html = _client(bundle_dir, key).get("/verifier").text
    assert IMAGE in html
    assert "gVisor" in html


def test_verifier_page_refuses_an_unpinned_image(
    bundle_dir: Path, key: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tag would let the graded environment drift, so the page says so rather
    than rendering it as a pinned environment."""
    monkeypatch.setenv("VERIFIER_IMAGE_DIGEST", "mergegate/verifier:latest")
    html = _client(bundle_dir, key).get("/verifier").text
    assert "configuration error" in html
    assert "pinned by digest" in html


def test_verifier_page_lists_the_grading_order(
    bundle_dir: Path, key: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordering is the argument, so it has to be on the page."""
    monkeypatch.setenv("VERIFIER_IMAGE_DIGEST", IMAGE)
    html = _client(bundle_dir, key).get("/verifier").text
    assert (
        "Purge grader paths, inject the buyer&#39;s bundle" in html or "Purge grader paths" in html
    )
    assert "Allowed to write is not allowed to grade" in html
    assert "deliberately redundant" in html


def test_verifier_page_lists_the_attacks(
    bundle_dir: Path, key: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VERIFIER_IMAGE_DIGEST", IMAGE)
    html = _client(bundle_dir, key).get("/verifier").text
    for marker in ("conftest.py", "sitecustomize.py", ".git history", "Force-pushing"):
        assert marker in html


def test_receipts_page_is_not_the_contracts_page(bundle_dir: Path, key: Ed25519PrivateKey) -> None:
    """It used to re-render the contracts table, so the nav item looked broken."""
    client = _client(bundle_dir, key)
    receipts = client.get("/receipts").text
    contracts = client.get("/").text
    assert "MergeGate: Receipts" in receipts
    assert "Correct code still gets refunded" not in receipts
    assert receipts != contracts


def test_receipts_page_links_to_every_receipt(bundle_dir: Path, key: Ed25519PrivateKey) -> None:
    html = _client(bundle_dir, key).get("/receipts").text
    assert 'href="/receipts/receipt-pass"' in html
    assert 'href="/receipts/mainnet-receipt-fail"' in html
    assert "checks passed" in html


def test_dashboard_rows_are_clickable(bundle_dir: Path, key: Ed25519PrivateKey) -> None:
    """A small link in the last column could be scrolled out of view, which
    made the receipts look unreachable. The whole row links now."""
    html = _client(bundle_dir, key).get("/").text
    assert 'class="absolute inset-0"' in html
    assert 'aria-label="Open receipt' in html


def test_no_em_dashes_anywhere_in_the_ui(bundle_dir: Path, key: Ed25519PrivateKey) -> None:
    """Requested explicitly. Covers rendered data as well as template prose,
    since receipt reasons carry their own punctuation."""
    client = _client(bundle_dir, key)
    for path in (
        "/",
        "/receipts",
        "/verifier",
        "/receipts/receipt-pass",
        "/receipts/mainnet-receipt-fail",
    ):
        assert "—" not in client.get(path).text, f"em-dash rendered on {path}"


# -- evaluation page -----------------------------------------------------------


def _eval_client(bundle_dir: Path, key: Ed25519PrivateKey) -> TestClient:
    return _client(bundle_dir, key)


def test_evaluation_page_reflects_a_passing_run(bundle_dir: Path, key: Ed25519PrivateKey) -> None:
    html = _eval_client(bundle_dir, key).get("/evaluations/receipt-pass").text
    assert "Sealed evaluation run" in html
    assert "No path violations" in html
    assert "exit 0" in html
    assert "stdout digest" in html


def test_evaluation_page_shows_which_stage_stopped_a_rejected_run(
    bundle_dir: Path, key: Ed25519PrivateKey
) -> None:
    """Stage states come from the manifest. A page that always showed the same
    ticks would be describing a run it never read."""
    html = _eval_client(bundle_dir, key).get("/evaluations/mainnet-receipt-fail").text
    assert "not run" in html.lower()
    assert "No commands executed" in html
    assert "protected_path" in html
    assert "decided the verdict" in html.lower()


def test_evaluation_page_binds_the_verification_identity(
    bundle_dir: Path, key: Ed25519PrivateKey
) -> None:
    html = _eval_client(bundle_dir, key).get("/evaluations/receipt-pass").text
    for label in ("base sha", "submission sha", "tree hash", "grader hash", "verifier image"):
        assert label in html


def test_unknown_evaluation_is_404(bundle_dir: Path, key: Ed25519PrivateKey) -> None:
    assert _eval_client(bundle_dir, key).get("/evaluations/nope").status_code == 404


# -- contract page -------------------------------------------------------------


def _contract_client(bundle_dir: Path, key: Ed25519PrivateKey, store: Any = None) -> TestClient:
    app = FastAPI()
    app.include_router(
        build_web_router(ReceiptBundle(bundle_dir, public_key=key.public_key()), contracts=store)
    )
    return TestClient(app)


def test_contract_page_renders_stored_terms(bundle_dir: Path, key: Ed25519PrivateKey) -> None:
    from mergegate.store import MemoryContractStore

    store = MemoryContractStore()
    store.put(
        {
            "contract_hash": CONTRACT_HASH,
            "task_id": "4KInc/mergegate-demo-task",
            "repository": "4KInc/mergegate-demo-task",
            "funding_tx": "0x" + "a" * 64,
            "funded_amount_usdc": "0.30",
            "chain": "BASE",
            "mandate_hash": "sha256:" + "b" * 64,
            "mandate_statement": "pay exactly 0.25 USDC to provider",
            "terms": {
                "repository": "4KInc/mergegate-demo-task",
                "base_sha": BASE_SHA,
                "reward_usdc": "0.25",
                "protected_paths": [".github/**"],
                "allowed_source_paths": ["src/**"],
                "grader_paths": ["tests/**"],
                "required_commands": [["python", "-m", "pytest", "-q"]],
            },
        }
    )
    html = _contract_client(bundle_dir, key, store).get(f"/contracts/{CONTRACT_HASH}").text

    assert "FUNDED" in html
    assert ".github/**" in html and "src/**" in html
    assert "python -m pytest -q" in html
    assert "0x" + "a" * 64 in html
    assert "Terms not recorded" not in html


def test_contract_page_admits_when_terms_were_not_recorded(
    bundle_dir: Path, key: Ed25519PrivateKey
) -> None:
    """Contracts funded before terms were persisted must say so rather than
    render terms the page cannot produce."""
    html = _contract_client(bundle_dir, key, None).get(f"/contracts/{CONTRACT_HASH}").text
    assert "Terms not recorded" in html
    assert "still fully verifiable" in html


def test_unknown_contract_is_404(bundle_dir: Path, key: Ed25519PrivateKey) -> None:
    r = _contract_client(bundle_dir, key, None).get("/contracts/sha256:" + "0" * 64)
    assert r.status_code == 404


def test_receipt_links_to_its_evaluation_and_contract(
    bundle_dir: Path, key: Ed25519PrivateKey
) -> None:
    html = _client(bundle_dir, key).get("/receipts/receipt-pass").text
    assert 'href="/evaluations/receipt-pass"' in html
    assert f'href="/contracts/{CONTRACT_HASH}"' in html


def test_verifier_page_documents_the_runtime_guard(
    bundle_dir: Path, key: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page describes the grading pipeline, so it has to describe the step
    that was added after a submission implementing nothing passed."""
    monkeypatch.setenv("VERIFIER_IMAGE_DIGEST", IMAGE)
    html = _client(bundle_dir, key).get("/verifier").text
    assert "runtime grader guard" in html
    assert "Reading the graded tests at run time" in html
    assert "without implementing anything" in html


# -- x402 ----------------------------------------------------------------------


def test_x402_challenge_matches_the_live_wire_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shape taken from a real x402 v2 listing, so an off-the-shelf client can
    read it without special-casing MergeGate."""
    from mergegate.app import create_app
    from mergegate.webhook import WebhookReceiver

    monkeypatch.setenv("VERIFIER_FEE_WALLET_ADDRESS", "0xFEE")
    monkeypatch.setenv("VERIFIER_FEE_USDC", "0.05")
    app = create_app(receiver=WebhookReceiver(secret="s", repository="r", resolve=lambda p: None))
    r = TestClient(app).get("/x402/verify")

    assert r.status_code == 402
    body = r.json()
    assert body["x402Version"] == 2
    accept = body["accepts"][0]
    assert accept["scheme"] == "exact"
    assert accept["network"] == "eip155:8453"
    # USDC has six decimals, so 0.05 is 50000 units. A float here could price
    # the fee at an amount nobody set.
    assert accept["amount"] == "50000"
    assert accept["payTo"] == "0xFEE"


def test_x402_does_not_claim_a_payment_it_cannot_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Answering 200 to a payment that was not verified and settled would claim
    a fee that never moved, which is worse than charging nothing.

    The endpoint now verifies presented authorizations rather than refusing them
    all, so this pins the two ways it must still say 402: an undecodable
    payload, and one that verifies but cannot be settled because no relayer
    holds gas.
    """
    from mergegate.app import create_app
    from mergegate.webhook import WebhookReceiver
    from mergegate.x402_settle import RELAYER_KEY_VAR

    monkeypatch.setenv("VERIFIER_FEE_WALLET_ADDRESS", "0xFEE")
    monkeypatch.delenv(RELAYER_KEY_VAR, raising=False)
    app = create_app(receiver=WebhookReceiver(secret="s", repository="r", resolve=lambda p: None))
    client = TestClient(app)

    undecodable = client.get("/x402/verify", headers={"X-PAYMENT": "anything"})
    assert undecodable.status_code == 402
    assert "not a decodable" in undecodable.json()["error"]

    # A real, correctly signed authorization, which must still not return 200
    # while nothing can submit it on-chain.
    from mergegate.x402 import X402Price
    from tests.test_x402_settle import PAY_TO, USDC, _sign

    monkeypatch.setenv("VERIFIER_FEE_WALLET_ADDRESS", PAY_TO)
    monkeypatch.setenv("USDC_CONTRACT_ADDRESS", USDC)
    app = create_app(receiver=WebhookReceiver(secret="s", repository="r", resolve=lambda p: None))
    header = _sign(X402Price(pay_to=PAY_TO, asset=USDC, amount_usdc="0.05"))
    r = TestClient(app).get("/x402/verify", headers={"X-PAYMENT": header})

    assert r.status_code == 402
    body = r.json()
    assert body["verified"] is True
    assert "not settled" in body["error"]
    assert "no relayer configured" in body["detail"]
