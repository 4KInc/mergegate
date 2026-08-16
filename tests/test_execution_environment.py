"""A receipt must not claim isolation the run did not have.

``VerificationManifest.egress_policy`` is written into a signed receipt, and it
used to default to the sealed sandbox's posture while nothing dispatched to that
sandbox: ``sandbox.build_job_request`` built a job request no caller submitted,
and grading happened through ``subprocess`` in the calling process. So every
receipt asserted a network posture that was never a property of the environment
that produced it.

Dispatch exists now, and these tests matter more rather than less. Both modes
are supported — sealed for real runs, in-process for this suite and for a laptop
with no GCP project — so the claim and the environment can still diverge. What
stops them is that a manifest has to be *told* it was sealed.

The whole suite passed while that was true, which is the more useful lesson:
381 tests covered what the verifier *computes* and none covered whether its
signed description of *where it ran* was accurate.

These tests close that. They are deliberately about the claim rather than the
mechanism, because the mechanism was never wrong.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mergegate.contract import SealedContract, TaskContract, build_contract
from mergegate.submission import ChangeKind, FileChange, Submission
from mergegate.verifier.evaluate import evaluate
from mergegate.verifier.manifest import VerificationManifest
from mergegate.verifier.sandbox import EGRESS_DENY_TCP, EGRESS_UNRESTRICTED

from .conftest import IMAGE

SOURCE = "def add(a, b):\n    return a + b\n"
GRADER = "from src.calc import add\n\n\ndef test_adds():\n    assert add(2, 2) == 4\n"


@pytest.fixture
def sealed(tmp_path: Path) -> SealedContract:
    bundle = tmp_path / "grader"
    (bundle / "tests").mkdir(parents=True)
    (bundle / "tests" / "test_calc.py").write_text(GRADER)
    contract: TaskContract = build_contract(
        grader_bundle=bundle,
        task_id="task-env",
        repository="4KInc/demo-repo",
        base_sha="a" * 40,
        verifier_image_digest=IMAGE,
        required_commands=(("python", "-c", "print(1)"),),
        allowed_source_paths=("src/**",),
        protected_paths=(".github/**",),
        grader_paths=("tests/**",),
        reward_usdc="1.00",
        buyer_agent="0xB",
        provider_agent="0xP",
        deadline=datetime.now(UTC) + timedelta(hours=1),
    )
    return contract.seal(funding_tx="0xfund", mandate_hash="sha256:" + "e" * 64)


@pytest.fixture
def base_tree(tmp_path: Path) -> Path:
    root = tmp_path / "base"
    (root / "src").mkdir(parents=True)
    (root / "src" / "__init__.py").write_text("")
    (root / "src" / "calc.py").write_text("def add(a, b):\n    return 0\n")
    (root / "tests").mkdir()
    return root


def test_an_in_process_run_does_not_claim_the_sandbox(
    sealed: SealedContract, base_tree: Path, tmp_path: Path
) -> None:
    """The regression test for the original bug.

    ``evaluate`` grades in the calling process. A manifest it produced claimed
    ``deny-tcp-egress`` regardless, and that string went into a signed receipt.
    """
    grader = tmp_path / "grader"
    manifest = evaluate(
        sealed=sealed,
        submission=Submission(
            "1" * 40, (FileChange("src/calc.py", ChangeKind.MODIFY, SOURCE.encode()),)
        ),
        base_tree=base_tree,
        grader_bundle=grader,
        destination=tmp_path / "workspace",
        timeout_seconds=60,
    )

    assert manifest.egress_policy != EGRESS_DENY_TCP
    assert manifest.egress_policy == EGRESS_UNRESTRICTED
    assert "not in the sealed sandbox" in manifest.egress_policy


def test_a_rejected_submission_also_states_the_real_environment(
    sealed: SealedContract, base_tree: Path, tmp_path: Path
) -> None:
    """The contract-violation path returns its own manifest and had to be
    fixed separately. A FAIL receipt overstating isolation is exactly as wrong
    as a PASS one."""
    grader = tmp_path / "grader"
    (base_tree / ".github" / "workflows").mkdir(parents=True)
    (base_tree / ".github" / "workflows" / "deploy.yml").write_text("on: push\n")

    manifest = evaluate(
        sealed=sealed,
        submission=Submission(
            "2" * 40,
            (FileChange(".github/workflows/deploy.yml", ChangeKind.MODIFY, b"on: []\n"),),
        ),
        base_tree=base_tree,
        grader_bundle=grader,
        destination=tmp_path / "workspace2",
        timeout_seconds=60,
    )

    assert manifest.failed_terms
    assert manifest.egress_policy == EGRESS_UNRESTRICTED


def test_the_sealed_posture_must_be_passed_in_explicitly() -> None:
    """A caller can still record the sealed posture, but only by stating it.

    That is the shape of the fix: isolation is something the environment
    provides and the caller attests, never something the manifest assumes.
    """
    manifest = VerificationManifest(
        task_id="t",
        contract_hash="sha256:" + "c" * 64,
        grader_hash="sha256:" + "d" * 64,
        base_sha="a" * 40,
        submission_sha="1" * 40,
        tree_hash="sha256:" + "e" * 64,
        verifier_image_digest=IMAGE,
        egress_policy=EGRESS_DENY_TCP,
    )
    assert manifest.to_canonical_dict()["egress_policy"] == EGRESS_DENY_TCP


def test_nothing_dispatches_to_cloud_run_yet() -> None:
    """Pins the gap rather than hiding it.

    ``build_job_request`` describes a sealed Cloud Run job that no code
    submits. While that stays true, the docs must not describe grading as
    happening there. When someone wires it, this test fails and is the prompt
    to update the claim in the same commit.
    """
    root = Path(__file__).parent.parent / "mergegate"
    dispatchers = {"run_v2", "RunJobRequest", "JobsClient", "execute_job"}
    found: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in dispatchers:
                found.append(f"{path.name}:{node.attr}")
            elif isinstance(node, ast.Name) and node.id in dispatchers:
                found.append(f"{path.name}:{node.id}")

    assert not found, (
        "something now dispatches to Cloud Run: "
        f"{found}. Grading may be sealed for real; update the egress claim and the docs."
    )
