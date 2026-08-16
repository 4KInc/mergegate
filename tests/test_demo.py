"""Both demo flows through the real components, with a fake rail.

Everything here is the production path except the chain: a real git repository,
the buyer's real grader bundle, a real pytest process in the assembled
workspace, the real state machine, the real mandate executor, and a receipt
verified offline. Only :class:`~mergegate.payments.FakeRail` stands in, so the
flows can be exercised without spending USDC.

The FAIL flow is the one that matters. Its code is *correct*; it would pass the
buyer's tests, but it also edits a protected path, so it is rejected before the
tests run and escrow returns to the buyer.
"""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mergegate.demo import (
    PASS_PATCH,
    PROTECTED_PATCH,
    DemoConfig,
    DemoRunner,
    _sealed_job,
)
from mergegate.payments import FakeRail
from mergegate.receipt import verify_receipt
from mergegate.settlement import TaskState
from mergegate.store import MemoryTaskStore
from mergegate.verifier.git_source import resolve_sha
from mergegate.verifier.manifest import Verdict

from .conftest import IMAGE

BUYER, PROVIDER, ESCROW, FEE = "0xBUYER", "0xPROVIDER", "0xESCROW", "0xVERIFIER"


@pytest.fixture
def config() -> DemoConfig:
    return DemoConfig(
        repo="4KInc/mergegate-demo-task",
        buyer=BUYER,
        provider=PROVIDER,
        escrow=ESCROW,
        verifier_fee_wallet=FEE,
        reward_usdc="0.25",
        verifier_fee_usdc="0.05",
        chain="BASE-SEPOLIA",
        usdc_address="0xUSDC",
        verifier_image=IMAGE,
        circle_cli="",
        # These tests grade in-process, so they pin an interpreter that exists
        # here. Production pins "python", which is what the sealed image has.
        verifier_python=sys.executable,
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def local_repo(tmp_path: Path) -> Path:
    """A stand-in for the demo repository, with the same shape and the same bug."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)

    work = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    (work / "src").mkdir(parents=True)
    (work / "src" / "__init__.py").write_text("")
    (work / "src" / "calc.py").write_text(
        "def add(a, b):\n    if a < 0 or b < 0:\n        return 0\n    return a + b\n"
    )
    (work / "tests").mkdir()
    (work / "tests" / ".gitkeep").write_text("")
    (work / ".github" / "workflows").mkdir(parents=True)
    (work / ".github" / "workflows" / "deploy.yml").write_text("on: push\njobs: {}\n")
    _git(work, "add", "-A")
    _git(work, "-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-q", "-m", "base")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "main")
    return work


@pytest.fixture
def runner(config: DemoConfig, local_repo: Path, tmp_path: Path) -> DemoRunner:
    rail = FakeRail(balances={BUYER: Decimal("10")})
    key = Ed25519PrivateKey.generate()
    r = DemoRunner(
        config,
        rail=rail,
        store=MemoryTaskStore(),
        workdir=tmp_path / "work",
        signing=("demo-key", key),
    )
    r.workdir.mkdir(parents=True, exist_ok=True)
    r.repo_dir = local_repo  # skip the network clone; everything else is real
    return r


def _drive(runner: DemoRunner, *, touch_protected: bool) -> dict[str, Any]:
    base_sha = resolve_sha(runner.repo_dir, "HEAD")
    contract = runner.build_contract(base_sha)
    sealed, mandate, _funding = runner.fund(contract)
    submission_sha = runner.submit(touch_protected=touch_protected)
    manifest = runner.evaluate(sealed, submission_sha)
    result = runner.settle(manifest, mandate)
    result["manifest"] = manifest
    return result


# -- PASS -> release ----------------------------------------------------------


def test_pass_flow_releases_to_the_provider(runner: DemoRunner) -> None:
    result = _drive(runner, touch_protected=False)
    binding = result["receipt"]["body"]["binding"]

    assert result["manifest"].verdict is Verdict.PASS
    assert result["state"] == TaskState.SETTLED.value
    assert binding["settlement_action"] == "release"
    assert binding["settlement_recipient"] == PROVIDER
    assert binding["settlement_amount_usdc"] == "0.25"
    assert runner.rail.balances[PROVIDER] == Decimal("0.25")  # type: ignore[attr-defined]


def test_escrow_is_funded_with_the_reward_and_the_verifier_fee(runner: DemoRunner) -> None:
    """P0.1: the money is committed against fixed terms up front, not after.

    Escrow holds reward + fee. Funding only the reward leaves nothing for the
    verifier fee once the provider is paid, and because the executor treats a
    failed fee as non-fatal, that shortfall does not break settlement. It just
    silently emits an empty verifier_fee_tx and drops P2.2 without complaint.
    """
    base_sha = resolve_sha(runner.repo_dir, "HEAD")
    contract = runner.build_contract(base_sha)
    runner.fund(contract)
    assert runner.rail.balances[ESCROW] == Decimal("0.30")  # type: ignore[attr-defined]


def test_escrow_is_emptied_by_a_release_plus_fee(runner: DemoRunner) -> None:
    """Nothing should be stranded in escrow after a terminal settlement."""
    result = _drive(runner, touch_protected=False)
    assert result["state"] == TaskState.SETTLED.value
    assert runner.rail.balances[ESCROW] == Decimal("0")  # type: ignore[attr-defined]


def test_funding_is_idempotent_on_the_mandate(runner: DemoRunner) -> None:
    """Re-running the demo with identical terms must not fund twice."""
    base_sha = resolve_sha(runner.repo_dir, "HEAD")
    contract = runner.build_contract(base_sha)
    runner.fund(contract)
    runner.fund(contract)
    assert runner.rail.settled_count == 1  # type: ignore[attr-defined]


# -- FAIL -> refund (the control-layer proof) ---------------------------------


def test_protected_path_flow_refunds_the_buyer(runner: DemoRunner) -> None:
    """Functionally correct code that disables the deploy gate is still rejected."""
    result = _drive(runner, touch_protected=True)
    binding = result["receipt"]["body"]["binding"]
    manifest = result["manifest"]

    assert manifest.verdict is Verdict.FAIL
    assert result["state"] == TaskState.REFUNDED.value
    assert binding["settlement_action"] == "refund"
    assert binding["settlement_recipient"] == BUYER
    assert ".github/workflows/deploy.yml" in binding["reason"]
    # The pinned commands never ran: the violation decided it.
    assert manifest.commands == ()


def test_the_failing_submission_would_otherwise_have_passed(runner: DemoRunner) -> None:
    """Without this, the FAIL flow proves nothing; it could just be broken code.

    The same source fix, submitted without touching the protected path, passes.
    """
    result = _drive(runner, touch_protected=False)
    assert result["manifest"].verdict is Verdict.PASS
    assert (runner.repo_dir / "src" / "calc.py").read_text() == PASS_PATCH
    assert PROTECTED_PATCH  # the FAIL flow differs only by this file


# -- the receipt --------------------------------------------------------------


@pytest.mark.parametrize("touch_protected", [False, True])
def test_receipt_verifies_offline_for_both_flows(runner: DemoRunner, touch_protected: bool) -> None:
    result = _drive(runner, touch_protected=touch_protected)
    key = runner.signing[1]  # type: ignore[index]
    outcome = verify_receipt(result["receipt"], public_key=key.public_key())
    assert outcome.valid, outcome.summary()


def test_receipt_binds_the_real_artifacts(runner: DemoRunner) -> None:
    """Each bound hash is re-derived from the originals, not compared to itself."""
    from mergegate.hashing import hash_directory

    result = _drive(runner, touch_protected=False)
    binding = result["receipt"]["body"]["binding"]

    assert binding["grader_hash"] == hash_directory(runner.grader_dir)
    assert binding["verifier_image_digest"] == runner.config.verifier_image
    assert binding["submission_sha"] == resolve_sha(runner.repo_dir, "HEAD")
    assert binding["settlement_tx"]


def test_verifier_fee_is_bound_and_distinct(runner: DemoRunner) -> None:
    """P2.2: escrow pays the verifier, and it is a different beneficiary."""
    result = _drive(runner, touch_protected=False)
    binding = result["receipt"]["body"]["binding"]
    assert binding["verifier_fee_tx"]
    assert binding["verifier_fee_tx"] != binding["settlement_tx"]
    assert runner.rail.balances[FEE] == Decimal("0.05")  # type: ignore[attr-defined]


def test_a_replayed_settlement_pays_once(runner: DemoRunner) -> None:
    """The whole flow, then the settlement events again. One payment."""
    base_sha = resolve_sha(runner.repo_dir, "HEAD")
    contract = runner.build_contract(base_sha)
    sealed, mandate, _ = runner.fund(contract)
    submission_sha = runner.submit(touch_protected=False)
    manifest = runner.evaluate(sealed, submission_sha)

    runner.settle(manifest, mandate, delivery_prefix="first")
    before = runner.rail.balances[PROVIDER]  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError):
        # Terminal state refuses a second settlement outright.
        runner.settle(manifest, mandate, delivery_prefix="second")
    assert runner.rail.balances[PROVIDER] == before  # type: ignore[attr-defined]


class TestSealedJobConfiguration:
    """Whether a run is sealed must never be decided by accident.

    These exist because the first version of :func:`_sealed_job` counted its
    settings with ``locals()``, which also counted the ``values`` argument. All
    four settings present looked like five, ``missing`` came out empty, and the
    function raised "half-configured; missing " with nothing after it — on the
    fully configured path. The half-configured path it was written to catch was
    the one path it could not reach.
    """

    FULL = {
        "VERIFIER_JOB_NAME": "mergegate-verifier",
        "VERIFIER_JOB_REGION": "us-central1",
        "GOOGLE_CLOUD_PROJECT": "some-project",
        "EVIDENCE_BUCKET": "some-bucket",
    }

    def test_all_four_settings_build_a_job(self) -> None:
        job = _sealed_job(dict(self.FULL))
        assert job is not None
        assert (job.name, job.region, job.project, job.bucket) == (
            "mergegate-verifier",
            "us-central1",
            "some-project",
            "some-bucket",
        )

    def test_no_settings_at_all_means_grade_in_process(self) -> None:
        """Absence is a choice; it is the laptop-reproducible mode."""
        assert _sealed_job({}) is None

    @pytest.mark.parametrize("omitted", sorted(FULL))
    def test_any_single_missing_setting_refuses_rather_than_unseals(self, omitted: str) -> None:
        values = {k: v for k, v in self.FULL.items() if k != omitted}
        with pytest.raises(RuntimeError) as raised:
            _sealed_job(values)
        # The name of what is missing must appear. An operator who reads
        # "half-configured" with an empty list learns nothing and is likely to
        # conclude the sealed path is broken and drop back to in-process.
        assert omitted in str(raised.value)

    def test_the_refusal_never_reports_an_empty_list_of_missing_settings(self) -> None:
        with pytest.raises(RuntimeError) as raised:
            _sealed_job({"VERIFIER_JOB_NAME": "mergegate-verifier"})
        assert "missing ." not in str(raised.value)


# -- the closed retry loop ----------------------------------------------------


def test_remediation_reverts_only_what_the_contract_forbids(runner: DemoRunner) -> None:
    """The repair is computed from the guard, not proposed by a model.

    Gemini explains why the submission failed; what the provider agent then
    does about it decides what gets resubmitted and therefore what gets paid
    for, so it has to be reproducible.
    """
    base_sha = resolve_sha(runner.repo_dir, "HEAD")
    contract = runner.build_contract(base_sha)
    runner.submit(touch_protected=True)

    new_sha, reverted = runner.remediate(contract, base_sha)

    assert new_sha, "remediation should produce a new submission"
    assert reverted == (".github/workflows/deploy.yml",)

    # The work survives. This is the whole point: the code was correct and only
    # the term violation is undone.
    calc = (runner.repo_dir / "src" / "calc.py").read_text()
    assert calc == PASS_PATCH

    # Compared against what the base commit actually held, not against a
    # hardcoded string. The invariant is "restored to base"; pinning the
    # content would pass only for one repository and would have to be edited
    # whenever the fixture changed, which is how a test stops testing anything.
    at_base = _git(runner.repo_dir, "show", f"{base_sha}:.github/workflows/deploy.yml")
    workflow = (runner.repo_dir / ".github" / "workflows" / "deploy.yml").read_text()
    assert workflow == at_base, "the protected file must be back to its base state"


def test_remediation_declines_when_there_is_nothing_to_revert(runner: DemoRunner) -> None:
    """A submission that failed its tests is not repairable by reverting.

    Returning an empty result rather than an optimistic one matters: a caller
    that treated "nothing to revert" as "fixed" would fund a second contract
    and resubmit an identical failing tree.
    """
    base_sha = resolve_sha(runner.repo_dir, "HEAD")
    contract = runner.build_contract(base_sha)
    runner.submit(touch_protected=False)

    new_sha, reverted = runner.remediate(contract, base_sha)

    assert new_sha == ""
    assert reverted == ()


def test_a_retry_is_a_new_contract_linked_to_its_predecessor(runner: DemoRunner) -> None:
    """Terminal means terminal, so a retry cannot reuse the settled contract.

    The buyer's mandate authorized exactly one payment decision. The link is
    provenance in ``metadata``, which is hashed but never read by the
    evaluator: an auditor can follow a retry back to the failure that prompted
    it, and no verdict can depend on it.
    """
    base_sha = resolve_sha(runner.repo_dir, "HEAD")
    contract = runner.build_contract(base_sha)

    retry = runner.build_contract(base_sha, retry_of=contract.contract_hash)

    assert ("retry_of", contract.contract_hash) in retry.metadata
    assert retry.contract_hash != contract.contract_hash
    # Same terms otherwise: a retry must not quietly move the goalposts.
    assert retry.allowed_source_paths == contract.allowed_source_paths
    assert retry.protected_paths == contract.protected_paths
    assert retry.grader_hash == contract.grader_hash


def test_the_full_loop_fails_then_pays_after_remediation(runner: DemoRunner) -> None:
    """The claim the closed loop makes, end to end, with money moving both ways.

    Attempt one is correct code plus a protected-path edit: refused, buyer
    refunded. The agent reverts what the guard rejects and resubmits under a
    new contract. Attempt two passes and the provider is paid.

    The buyer pays **two** verifier fees across this. That is the honest cost of
    a retry and the reason RetryBudget exists: a loop that costs the buyer
    nothing per attempt would have no reason to terminate.
    """
    base_sha = resolve_sha(runner.repo_dir, "HEAD")

    first = runner.build_contract(base_sha)
    sealed, mandate, _ = runner.fund(first)
    runner.submit(touch_protected=True)
    failed = runner.evaluate(sealed, resolve_sha(runner.repo_dir, "HEAD"))
    attempt_one = runner.settle(failed, mandate)

    assert failed.verdict is Verdict.FAIL
    assert attempt_one["state"] == TaskState.REFUNDED.value
    # .get, because the rail only creates an entry when money actually moves:
    # "no balance" and "a zero balance" both mean the provider was not paid.
    assert runner.rail.balances.get(PROVIDER, Decimal("0")) == Decimal("0")  # type: ignore[attr-defined]

    new_sha, reverted = runner.remediate(first, base_sha)
    assert reverted, "the protected-path edit should be revertible"

    second = runner.build_contract(base_sha, retry_of=first.contract_hash)
    sealed2, mandate2, _ = runner.fund(second)
    passed = runner.evaluate(sealed2, new_sha)
    attempt_two = runner.settle(passed, mandate2, delivery_prefix="retry")

    assert passed.verdict is Verdict.PASS
    assert attempt_two["state"] == TaskState.SETTLED.value
    assert runner.rail.balances[PROVIDER] == Decimal("0.25")  # type: ignore[attr-defined]

    # Two separate receipts, and the second names the first.
    assert (
        attempt_two["receipt"]["body"]["binding"]["contract_hash"]
        != attempt_one["receipt"]["body"]["binding"]["contract_hash"]
    )
    assert ("retry_of", first.contract_hash) in second.metadata
