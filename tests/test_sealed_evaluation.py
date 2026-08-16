"""The sealed evaluation path, and the checks that make it worth having.

Moving grading into a Cloud Run job buys nothing on its own. If the
orchestrator believed whatever manifest appeared on the output volume, the job
would be theatre: anything able to write there could mint a PASS. So most of
these tests are about refusal, not success.

The one that matters most is the last: no code outside the job constructs a
manifest. An orchestrator that can build its own verdict is an orchestrator
that can pay itself.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mergegate.contract import SealedContract, TaskContract, build_contract
from mergegate.submission import ChangeKind, FileChange, Submission
from mergegate.verifier.dispatch import (
    EvaluationRequest,
    VerdictUnavailableError,
    build_request,
    pack_inputs,
    verify_result,
)
from mergegate.verifier.job import run_job
from mergegate.verifier.manifest import Verdict
from mergegate.verifier.sandbox import EGRESS_DENY_TCP

from .conftest import IMAGE

GRADER = "from src.calc import add\n\n\ndef test_adds():\n    assert add(2, 2) == 4\n"
FIX = "def add(a, b):\n    return a + b\n"


@pytest.fixture
def grader_dir(tmp_path: Path) -> Path:
    bundle = tmp_path / "grader"
    (bundle / "tests").mkdir(parents=True)
    (bundle / "tests" / "test_calc.py").write_text(GRADER)
    return bundle


@pytest.fixture
def base_tree(tmp_path: Path) -> Path:
    root = tmp_path / "base"
    (root / "src").mkdir(parents=True)
    (root / "src" / "__init__.py").write_text("")
    (root / "src" / "calc.py").write_text("def add(a, b):\n    return 0\n")
    (root / "tests").mkdir()
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "deploy.yml").write_text("on: push\n")
    return root


@pytest.fixture
def sealed(grader_dir: Path) -> SealedContract:
    import sys

    contract: TaskContract = build_contract(
        grader_bundle=grader_dir,
        task_id="sealed-task",
        repository="4KInc/demo-repo",
        base_sha="a" * 40,
        verifier_image_digest=IMAGE,
        required_commands=((sys.executable, "-m", "pytest", "-q"),),
        allowed_source_paths=("src/**",),
        protected_paths=(".github/**",),
        grader_paths=("tests/**",),
        reward_usdc="0.25",
        buyer_agent="0xBUYER",
        provider_agent="0xPROVIDER",
        deadline=datetime.now(UTC) + timedelta(hours=1),
    )
    return contract.seal(funding_tx="0xfund", mandate_hash="sha256:" + "e" * 64)


def _submission(source: str = FIX, *, touch_protected: bool = False) -> Submission:
    changes = [FileChange("src/calc.py", ChangeKind.MODIFY, source.encode())]
    if touch_protected:
        changes.append(FileChange(".github/workflows/deploy.yml", ChangeKind.MODIFY, b"on: []\n"))
    return Submission("1" * 40, tuple(changes))


def _run(
    sealed: SealedContract, base: Path, grader: Path, mount: Path, sub: Submission
) -> tuple[EvaluationRequest, int]:
    """Pack a request, run the job body, and verify the result the way the
    orchestrator does. Exercises both halves against each other."""
    request = build_request(sealed=sealed, submission=sub, grader_bundle=grader)
    pack_inputs(request, base_tree=base, grader_bundle=grader, destination=mount)
    code = run_job(mount)
    return request, code


# -- the happy path, end to end ------------------------------------------------


def test_a_sealed_run_produces_a_verified_pass(
    sealed: SealedContract, base_tree: Path, grader_dir: Path, tmp_path: Path
) -> None:
    mount = tmp_path / "mount"
    request, code = _run(sealed, base_tree, grader_dir, mount, _submission())
    assert code == 0

    result = verify_result(
        request, outputs=mount / "out", execution_id="exec-1", image_digest=IMAGE
    )
    assert result.manifest.verdict is Verdict.PASS
    assert result.execution_id == "exec-1"


def test_a_sealed_run_records_the_sandbox_posture_because_it_is_true(
    sealed: SealedContract, base_tree: Path, grader_dir: Path, tmp_path: Path
) -> None:
    """The whole reason this module exists. In-process runs record
    `unrestricted`; a run that genuinely happened inside the sealed job records
    the posture that was probed."""
    mount = tmp_path / "mount"
    request, _ = _run(sealed, base_tree, grader_dir, mount, _submission())
    result = verify_result(request, outputs=mount / "out", execution_id="e", image_digest=IMAGE)

    assert result.manifest.egress_policy == EGRESS_DENY_TCP


def test_a_protected_path_violation_still_fails_inside_the_job(
    sealed: SealedContract, base_tree: Path, grader_dir: Path, tmp_path: Path
) -> None:
    mount = tmp_path / "mount"
    request, code = _run(sealed, base_tree, grader_dir, mount, _submission(touch_protected=True))
    assert code == 0

    result = verify_result(request, outputs=mount / "out", execution_id="e", image_digest=IMAGE)
    assert result.manifest.verdict is Verdict.FAIL
    assert "protected_path" in result.manifest.failed_terms
    assert not result.manifest.commands, "the pinned commands must not have run"


# -- refusals ------------------------------------------------------------------


def test_a_result_for_another_evaluation_is_refused(
    sealed: SealedContract, base_tree: Path, grader_dir: Path, tmp_path: Path
) -> None:
    """The check that stops a manifest from one run settling another."""
    mount = tmp_path / "mount"
    request, _ = _run(sealed, base_tree, grader_dir, mount, _submission())

    status = json.loads((mount / "out" / "status.json").read_text())
    status["correlation_id"] = "some-other-evaluation"
    (mount / "out" / "status.json").write_text(json.dumps(status))

    with pytest.raises(VerdictUnavailableError, match="different evaluation"):
        verify_result(request, outputs=mount / "out", execution_id="e", image_digest=IMAGE)


@pytest.mark.parametrize(
    "field_name,bogus",
    [
        ("contract_hash", "sha256:" + "0" * 64),
        ("submission_sha", "9" * 40),
        ("grader_hash", "sha256:" + "1" * 64),
        ("base_sha", "b" * 40),
        ("verifier_image_digest", "us-docker.pkg.dev/x/y@sha256:" + "2" * 64),
    ],
)
def test_a_manifest_describing_different_inputs_is_refused(
    field_name: str,
    bogus: str,
    sealed: SealedContract,
    base_tree: Path,
    grader_dir: Path,
    tmp_path: Path,
) -> None:
    """Each field is a way the result could describe an evaluation nobody paid
    for. A PASS on the wrong submission spends real money."""
    mount = tmp_path / "mount"
    request, _ = _run(sealed, base_tree, grader_dir, mount, _submission())

    manifest = json.loads((mount / "out" / "manifest.json").read_text())
    manifest[field_name] = bogus
    (mount / "out" / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(VerdictUnavailableError, match=field_name):
        verify_result(request, outputs=mount / "out", execution_id="e", image_digest=IMAGE)


def test_an_image_other_than_the_contracts_is_refused(
    sealed: SealedContract, base_tree: Path, grader_dir: Path, tmp_path: Path
) -> None:
    """The contract pins the image by digest. A run in a different image graded
    in an environment the buyer never agreed to."""
    mount = tmp_path / "mount"
    request, _ = _run(sealed, base_tree, grader_dir, mount, _submission())

    with pytest.raises(VerdictUnavailableError, match="not the digest the contract pinned"):
        verify_result(
            request,
            outputs=mount / "out",
            execution_id="e",
            image_digest="us-docker.pkg.dev/evil/v@sha256:" + "3" * 64,
        )


def test_a_missing_result_is_unavailable_not_a_fail(
    sealed: SealedContract, base_tree: Path, grader_dir: Path, tmp_path: Path
) -> None:
    """A FAIL refunds and closes the task. An absent result means the
    evaluation did not happen, and must not spend anything in either
    direction."""
    request = build_request(sealed=sealed, submission=_submission(), grader_bundle=grader_dir)
    empty = tmp_path / "nothing"
    empty.mkdir()

    with pytest.raises(VerdictUnavailableError, match="no status"):
        verify_result(request, outputs=empty, execution_id="e", image_digest=IMAGE)


def test_a_swapped_grader_on_the_volume_is_caught_by_the_job(
    sealed: SealedContract, base_tree: Path, grader_dir: Path, tmp_path: Path
) -> None:
    """The job recomputes the grader hash rather than trusting the request.
    Otherwise anything able to write the volume could grade a submission
    against tests nobody committed to."""
    mount = tmp_path / "mount"
    request = build_request(sealed=sealed, submission=_submission(), grader_bundle=grader_dir)
    pack_inputs(request, base_tree=base_tree, grader_bundle=grader_dir, destination=mount)

    # Swap the grader for one that passes unconditionally, after the request
    # was written.
    swapped = tmp_path / "swapped"
    (swapped / "tests").mkdir(parents=True)
    (swapped / "tests" / "test_calc.py").write_text("def test_always():\n    assert True\n")
    from mergegate.verifier.dispatch import _tar_bytes

    (mount / "in" / "grader.tar").write_bytes(_tar_bytes(swapped))

    assert run_job(mount) == 2
    status = json.loads((mount / "out" / "status.json").read_text())
    assert status["ok"] is False
    assert "hashes to" in status["reason"]


# -- the structural guarantee --------------------------------------------------


def test_only_the_job_can_construct_a_manifest() -> None:
    """The property that makes the sealed job meaningful.

    If the orchestrator could build a ``VerificationManifest``, moving grading
    into a job would prove nothing: the process asking for a verdict could
    write one. ``evaluate`` is the sole constructor, and the only caller that
    reaches it in the settlement path is the job body.
    """
    root = Path(__file__).parent.parent / "mergegate"
    builders: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "VerificationManifest"
            ):
                builders.append(path.relative_to(root.parent).as_posix())

    assert set(builders) <= {"mergegate/verifier/evaluate.py"}, (
        f"a manifest is constructed outside the evaluator: {sorted(set(builders))}"
    )


def test_dispatch_cannot_produce_a_verdict() -> None:
    """`VerdictUnavailableError` carries no decision, and the module has no way
    to invent one: it never imports the evaluator."""
    source = (Path(__file__).parent.parent / "mergegate" / "verifier" / "dispatch.py").read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[-1])

    assert "evaluate" not in imported, "the orchestrator can reach the evaluator directly"
