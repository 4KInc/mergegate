"""Both demo flows, end to end, with everything except the chain.

This is P2.1's PASS→release and protected-path FAIL→refund, driven through the
real components: a real repo, a real grader bundle, a real pytest run, the real
state machine, the real mandate executor, and a receipt that is then verified
offline by a party holding nothing but the receipt and a public key.

The only thing stubbed is the on-chain transfer, because there are no
credentials yet. Where a transaction hash would go, these tests put a marker and
assert the receipt carries it — so when the Circle wiring lands, the change is
substituting a real hash, not restructuring the flow.

P0.6's done-when is what this file exists to show: the settlement is the
deterministic result of the signed mandate plus the verdict, traceable from the
contract the buyer signed to the money that moved.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mergegate.contract import SealedContract, TaskContract, build_contract
from mergegate.mandate import PaymentMandate, SettlementAction
from mergegate.receipt import build_receipt, sign_receipt, verify_receipt
from mergegate.settlement import TaskState, TaskStateMachine
from mergegate.submission import ChangeKind, FileChange, Submission
from mergegate.verifier import Verdict
from mergegate.verifier.evaluate import evaluate

from .conftest import IMAGE

GRADER_TEST = """\
from src.calc import add


def test_adds_negatives():
    assert add(-1, -1) == -2
"""

BROKEN_CALC = "def add(a, b):\n    if a < 0 or b < 0:\n        return 0\n    return a + b\n"
FIXED_CALC = "def add(a, b):\n    return a + b\n"


@pytest.fixture
def base_tree(tmp_path: Path) -> Path:
    root = tmp_path / "base"
    (root / "src").mkdir(parents=True)
    (root / "src" / "__init__.py").write_text("")
    (root / "src" / "calc.py").write_text(BROKEN_CALC)
    (root / "tests").mkdir()
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "deploy.yml").write_text("on: push\njobs: {}\n")
    return root


@pytest.fixture
def grader(tmp_path: Path) -> Path:
    bundle = tmp_path / "grader"
    (bundle / "tests").mkdir(parents=True)
    (bundle / "tests" / "test_calc.py").write_text(GRADER_TEST)
    return bundle


@pytest.fixture
def deadline() -> datetime:
    return datetime.now(UTC) + timedelta(hours=6)


@pytest.fixture
def sealed(grader: Path, deadline: datetime) -> SealedContract:
    contract: TaskContract = build_contract(
        grader_bundle=grader,
        task_id="task-demo",
        repository="4KInc/demo-repo",
        base_sha="a" * 40,
        verifier_image_digest=IMAGE,
        required_commands=((sys.executable, "-m", "pytest", "-q"),),
        allowed_source_paths=("src/**",),
        protected_paths=(".github/**",),
        grader_paths=("tests/**",),
        reward_usdc="250.00",
        buyer_agent="0xBUYER",
        provider_agent="0xPROVIDER",
        deadline=deadline,
    )
    # The buyer agent signs the mandate and funds escrow in one step; the
    # contract is sealed against that funding transaction.
    return contract.seal(funding_tx="0xFUND_TX", mandate_hash="sha256:" + "e" * 64)


@pytest.fixture
def mandate(sealed: SealedContract, deadline: datetime) -> PaymentMandate:
    return PaymentMandate(
        task_id=sealed.contract.task_id,
        contract_hash=sealed.contract_hash,
        buyer_agent="0xBUYER",
        provider_agent="0xPROVIDER",
        amount_usdc="250.00",
        asset="USDC",
        chain="base",
        deadline=deadline,
        nonce="demo-nonce",
    )


def _drive(
    *,
    sealed: SealedContract,
    mandate: PaymentMandate,
    submission: Submission,
    base_tree: Path,
    grader: Path,
    workspace: Path,
    settlement_tx: str,
) -> tuple[TaskStateMachine, dict[str, object]]:
    """Run one submission from webhook to signed receipt."""
    machine = TaskStateMachine(
        task_id=sealed.contract.task_id,
        contract_hash=sealed.contract_hash,
        mandate=mandate,
    )
    machine.on_submission(submission_sha=submission.submission_sha, delivery_id="d1")
    machine.on_verification_started(submission_sha=submission.submission_sha, delivery_id="d2")

    manifest = evaluate(
        sealed=sealed,
        submission=submission,
        base_tree=base_tree,
        grader_bundle=grader,
        destination=workspace,
        timeout_seconds=120,
    )
    machine.on_verification_completed(manifest=manifest, delivery_id="d3")

    outcome = machine.on_settlement(manifest=manifest, now=datetime.now(UTC), delivery_id="d4")
    assert outcome.directive is not None
    machine.record_settlement_tx(settlement_tx)

    body = build_receipt(
        manifest=manifest,
        mandate=mandate,
        directive=outcome.directive,
        issued_at=datetime.now(UTC),
        settlement_tx=settlement_tx,
        verifier_fee_tx="0xFEE_TX_PENDING",
    )
    return machine, body


def test_pass_flow_releases_to_the_provider(
    sealed: SealedContract,
    mandate: PaymentMandate,
    base_tree: Path,
    grader: Path,
    tmp_path: Path,
) -> None:
    """PASS → release. The provider actually fixed the code."""
    submission = Submission(
        submission_sha="1" * 40,
        changes=(FileChange("src/calc.py", ChangeKind.MODIFY, FIXED_CALC.encode()),),
    )
    machine, body = _drive(
        sealed=sealed,
        mandate=mandate,
        submission=submission,
        base_tree=base_tree,
        grader=grader,
        workspace=tmp_path / "ws",
        settlement_tx="0xRELEASE_TX",
    )

    assert machine.state is TaskState.SETTLED
    binding = body["binding"]
    assert isinstance(binding, dict)
    assert binding["decision"] == Verdict.PASS.value
    assert binding["settlement_action"] == SettlementAction.RELEASE.value
    assert binding["settlement_recipient"] == "0xPROVIDER"
    assert binding["settlement_amount_usdc"] == "250.00"
    assert binding["settlement_tx"] == "0xRELEASE_TX"

    # Every link in the chain is present in one object.
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
    ):
        assert binding[field], f"{field} missing from the receipt binding"


def test_protected_path_flow_refunds_the_buyer(
    sealed: SealedContract,
    mandate: PaymentMandate,
    base_tree: Path,
    grader: Path,
    tmp_path: Path,
) -> None:
    """FAIL → refund. The demo's control-layer proof.

    The code is functionally correct — it would pass the buyer's tests — but the
    submission also disabled the deploy workflow. That is a contract violation,
    so the pinned commands never run and escrow returns to the buyer.
    """
    submission = Submission(
        submission_sha="2" * 40,
        changes=(
            FileChange("src/calc.py", ChangeKind.MODIFY, FIXED_CALC.encode()),
            FileChange(".github/workflows/deploy.yml", ChangeKind.MODIFY, b"on: []\n"),
        ),
    )
    machine, body = _drive(
        sealed=sealed,
        mandate=mandate,
        submission=submission,
        base_tree=base_tree,
        grader=grader,
        workspace=tmp_path / "ws",
        settlement_tx="0xREFUND_TX",
    )

    assert machine.state is TaskState.REFUNDED
    binding = body["binding"]
    assert isinstance(binding, dict)
    assert binding["decision"] == Verdict.FAIL.value
    assert binding["settlement_action"] == SettlementAction.REFUND.value
    assert binding["settlement_recipient"] == "0xBUYER"

    # The receipt names the exact failed term, not just "failed".
    reason = str(binding["reason"])
    assert ".github/workflows/deploy.yml" in reason
    assert "contract-protected path" in reason

    manifest = body["manifest"]
    assert isinstance(manifest, dict)
    assert manifest["failed_terms"] == ["protected_path"]
    assert manifest["commands"] == [], "commands must not run after a path violation"


def test_the_receipt_verifies_for_someone_who_holds_only_the_receipt(
    sealed: SealedContract,
    mandate: PaymentMandate,
    base_tree: Path,
    grader: Path,
    tmp_path: Path,
) -> None:
    """The whole point: a third party can re-check the chain independently.

    They have no repo, no grader bundle, no database, and no access to
    MergeGate — just the receipt and a public key.
    """
    key = Ed25519PrivateKey.generate()
    submission = Submission(
        submission_sha="3" * 40,
        changes=(FileChange("src/calc.py", ChangeKind.MODIFY, FIXED_CALC.encode()),),
    )
    _machine, body = _drive(
        sealed=sealed,
        mandate=mandate,
        submission=submission,
        base_tree=base_tree,
        grader=grader,
        workspace=tmp_path / "ws",
        settlement_tx="0xRELEASE_TX",
    )
    envelope = sign_receipt(body, private_key=key, kid="mergegate-demo")

    result = verify_receipt(envelope, public_key=key.public_key())
    assert result.valid, result.summary()


def test_the_receipt_binds_the_contract_the_buyer_actually_signed(
    sealed: SealedContract,
    mandate: PaymentMandate,
    base_tree: Path,
    grader: Path,
    tmp_path: Path,
) -> None:
    """P0.6 traceability: receipt → mandate → contract → grader bundle.

    Each hash in the receipt is re-derived here from the original artifacts,
    rather than compared to itself.
    """
    from mergegate.hashing import hash_directory

    submission = Submission(
        submission_sha="4" * 40,
        changes=(FileChange("src/calc.py", ChangeKind.MODIFY, FIXED_CALC.encode()),),
    )
    _machine, body = _drive(
        sealed=sealed,
        mandate=mandate,
        submission=submission,
        base_tree=base_tree,
        grader=grader,
        workspace=tmp_path / "ws",
        settlement_tx="0xRELEASE_TX",
    )
    binding = body["binding"]
    assert isinstance(binding, dict)

    assert binding["contract_hash"] == sealed.contract.contract_hash
    assert binding["grader_hash"] == hash_directory(grader)
    assert binding["base_sha"] == sealed.contract.base_sha
    assert binding["verifier_image_digest"] == sealed.contract.verifier_image_digest
    assert binding["mandate_hash"] == mandate.mandate_hash
    assert binding["submission_sha"] == submission.submission_sha


def test_a_second_settlement_attempt_pays_nothing(
    sealed: SealedContract,
    mandate: PaymentMandate,
    base_tree: Path,
    grader: Path,
    tmp_path: Path,
) -> None:
    """The full flow, then a replayed settlement event. One payment only."""
    submission = Submission(
        submission_sha="5" * 40,
        changes=(FileChange("src/calc.py", ChangeKind.MODIFY, FIXED_CALC.encode()),),
    )
    machine, _body = _drive(
        sealed=sealed,
        mandate=mandate,
        submission=submission,
        base_tree=base_tree,
        grader=grader,
        workspace=tmp_path / "ws",
        settlement_tx="0xRELEASE_TX",
    )
    manifest = evaluate(
        sealed=sealed,
        submission=submission,
        base_tree=base_tree,
        grader_bundle=grader,
        destination=tmp_path / "ws2",
        timeout_seconds=120,
    )
    replay = machine.on_settlement(manifest=manifest, now=datetime.now(UTC), delivery_id="d-replay")
    assert not replay.applied
    assert machine.settlement_tx == "0xRELEASE_TX"
