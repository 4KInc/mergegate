"""P0.3 / P1.1 / P1.2 / P1.3 — the provider cannot influence the grader.

These are end-to-end: a real base repo on disk, a real buyer grader bundle, real
provider submissions, and a real ``pytest`` process run inside the assembled
workspace. The attacks are the ones documented in the SWE-bench and coding-agent
literature — editing the tests, dropping a ``conftest.py`` that forces a pass,
reading the answer out of ``.git`` history, disabling CI on the way past — and
each one is asserted to fail.

Asserting on a mocked runner would prove nothing here. The claim is that the
grade is unaffected, so the grade has to actually be computed.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mergegate.contract import SealedContract, TaskContract, build_contract
from mergegate.submission import ChangeKind, FileChange, Submission
from mergegate.verifier import Verdict
from mergegate.verifier.evaluate import evaluate
from mergegate.verifier.manifest import VerificationManifest
from mergegate.verifier.workspace import assemble_workspace

from .conftest import BASE_SHA, IMAGE

# The buyer's grader asserts add() handles negatives. The base repo gets it
# wrong, so an honest provider must actually fix src/calc.py to pass.
GRADER_TEST = """\
from src.calc import add


def test_adds_positives():
    assert add(2, 2) == 4


def test_adds_negatives():
    assert add(-1, -1) == -2
"""

BROKEN_CALC = """\
def add(a, b):
    if a < 0 or b < 0:
        return 0
    return a + b
"""

FIXED_CALC = """\
def add(a, b):
    return a + b
"""


@pytest.fixture
def base_tree(tmp_path: Path) -> Path:
    """The buyer's repository at the pinned base SHA, with real git history."""
    root = tmp_path / "base"
    (root / "src").mkdir(parents=True)
    (root / "src" / "calc.py").write_text(BROKEN_CALC)
    (root / "src" / "__init__.py").write_text("")
    (root / "tests").mkdir()
    (root / "tests" / "test_calc.py").write_text("# placeholder, replaced by the buyer's bundle\n")
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "deploy.yml").write_text("on: push\njobs: {}\n")

    # A .git directory containing the reference solution, exactly the leak
    # P1.2 exists to close.
    git = root / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "GOLD_PATCH").write_text(FIXED_CALC)
    return root


@pytest.fixture
def buyer_grader(tmp_path: Path) -> Path:
    bundle = tmp_path / "grader"
    (bundle / "tests").mkdir(parents=True)
    (bundle / "tests" / "test_calc.py").write_text(GRADER_TEST)
    return bundle


@pytest.fixture
def sealed(base_tree: Path, buyer_grader: Path) -> SealedContract:
    contract: TaskContract = build_contract(
        grader_bundle=buyer_grader,
        task_id="task-neutrality",
        repository="4KInc/demo-repo",
        base_sha=BASE_SHA,
        verifier_image_digest=IMAGE,
        required_commands=((sys.executable, "-m", "pytest", "-q"),),
        allowed_source_paths=("src/**",),
        protected_paths=(".github/**",),
        grader_paths=("tests/**",),
        reward_usdc="250.00",
        buyer_agent="0xBUYER",
        provider_agent="0xPROVIDER",
        deadline=_deadline(),
    )
    return contract.seal(funding_tx="0xfund", mandate_hash="sha256:" + "e" * 64)


def _deadline() -> datetime:
    return datetime.now(UTC) + timedelta(hours=6)


def _run(
    sealed: SealedContract,
    submission: Submission,
    base_tree: Path,
    buyer_grader: Path,
    tmp_path: Path,
) -> VerificationManifest:
    return evaluate(
        sealed=sealed,
        submission=submission,
        base_tree=base_tree,
        grader_bundle=buyer_grader,
        destination=tmp_path / "workspace",
        timeout_seconds=120,
    )


# -- the honest baseline ------------------------------------------------------


def test_correct_fix_passes(
    sealed: SealedContract, base_tree: Path, buyer_grader: Path, tmp_path: Path
) -> None:
    """Control case. If this does not pass, every failure below is meaningless."""
    submission = Submission(
        submission_sha="1" * 40,
        changes=(FileChange("src/calc.py", ChangeKind.MODIFY, FIXED_CALC.encode()),),
    )
    manifest = _run(sealed, submission, base_tree, buyer_grader, tmp_path)
    assert manifest.verdict is Verdict.PASS
    assert manifest.failed_terms == ()
    assert manifest.tamper_signals == ()


def test_unfixed_code_fails(
    sealed: SealedContract, base_tree: Path, buyer_grader: Path, tmp_path: Path
) -> None:
    """The other control: the buyer's grader really does fail broken code."""
    submission = Submission(
        submission_sha="2" * 40,
        changes=(FileChange("src/calc.py", ChangeKind.MODIFY, BROKEN_CALC.encode()),),
    )
    manifest = _run(sealed, submission, base_tree, buyer_grader, tmp_path)
    assert manifest.verdict is Verdict.FAIL


# -- P0.3: the provider cannot alter the effective grader ---------------------


def test_rewriting_the_graded_tests_is_rejected(
    sealed: SealedContract, base_tree: Path, buyer_grader: Path, tmp_path: Path
) -> None:
    """P0.3 done-when: submit a malicious tests/ change → rejected outright."""
    submission = Submission(
        submission_sha="3" * 40,
        changes=(
            FileChange(
                "tests/test_calc.py",
                ChangeKind.MODIFY,
                b"def test_adds_negatives():\n    assert True\n",
            ),
        ),
    )
    manifest = _run(sealed, submission, base_tree, buyer_grader, tmp_path)

    assert manifest.verdict is Verdict.FAIL
    assert "grader_path" in manifest.failed_terms
    assert "tests/test_calc.py" in manifest.rejection_reason
    # The commands never ran: a grader-path violation is decided before execution.
    assert manifest.commands == ()


def test_buyer_grader_overwrites_whatever_is_at_the_test_path(
    sealed: SealedContract, base_tree: Path, buyer_grader: Path, tmp_path: Path
) -> None:
    """Even reaching assembly directly, the buyer's bytes win.

    This bypasses the path guard on purpose, to prove the injection step is
    independently sufficient rather than relying on the earlier check.
    """
    submission = Submission(
        submission_sha="4" * 40,
        changes=(FileChange("src/calc.py", ChangeKind.MODIFY, FIXED_CALC.encode()),),
    )
    # Plant a hostile test in the base tree itself, as if it had survived.
    (base_tree / "tests" / "test_calc.py").write_text("def test_free_pass():\n    assert True\n")

    workspace = assemble_workspace(
        base_tree=base_tree,
        submission=submission,
        contract=sealed.contract,
        grader_bundle=buyer_grader,
        destination=tmp_path / "ws",
    )
    graded = (workspace.root / "tests" / "test_calc.py").read_text()
    assert graded == GRADER_TEST
    assert "test_free_pass" not in graded


# -- P1.1: conftest.py / persisted-file gaming --------------------------------


def test_provider_conftest_cannot_force_a_pass(
    sealed: SealedContract, base_tree: Path, buyer_grader: Path, tmp_path: Path
) -> None:
    """P1.1 — the documented attack: a conftest hook that rewrites outcomes.

    ``src/conftest.py`` is inside an allowed source path, so the path guard
    permits writing it. pytest would still collect and execute it. Allowed to
    write is not allowed to grade, so it is quarantined before the run.
    """
    hostile_conftest = (
        "import pytest\n"
        "\n"
        "@pytest.hookimpl(hookwrapper=True)\n"
        "def pytest_runtest_makereport(item, call):\n"
        "    outcome = yield\n"
        "    report = outcome.get_result()\n"
        "    report.outcome = 'passed'\n"
    )
    submission = Submission(
        submission_sha="5" * 40,
        changes=(
            # Code still broken — only the hook is doing the work.
            FileChange("src/calc.py", ChangeKind.MODIFY, BROKEN_CALC.encode()),
            FileChange("src/conftest.py", ChangeKind.ADD, hostile_conftest.encode()),
        ),
    )
    manifest = _run(sealed, submission, base_tree, buyer_grader, tmp_path)

    assert manifest.verdict is Verdict.FAIL
    assert any("src/conftest.py" in signal for signal in manifest.tamper_signals)


def test_provider_sitecustomize_is_quarantined(
    sealed: SealedContract, base_tree: Path, buyer_grader: Path, tmp_path: Path
) -> None:
    """sitecustomize runs at interpreter startup, before any test is imported."""
    submission = Submission(
        submission_sha="6" * 40,
        changes=(
            FileChange("src/calc.py", ChangeKind.MODIFY, BROKEN_CALC.encode()),
            FileChange("src/sitecustomize.py", ChangeKind.ADD, b"import sys\n"),
        ),
    )
    manifest = _run(sealed, submission, base_tree, buyer_grader, tmp_path)
    assert manifest.verdict is Verdict.FAIL
    assert any("sitecustomize" in signal for signal in manifest.tamper_signals)


def test_buyers_own_untouched_hooks_survive(
    sealed: SealedContract, base_tree: Path, buyer_grader: Path, tmp_path: Path
) -> None:
    """The quarantine must not eat the buyer's own repo configuration.

    A repo legitimately ships a root conftest.py. If the provider did not touch
    it, it is the buyer's and it stays — otherwise the defense would break
    ordinary projects.
    """
    (base_tree / "conftest.py").write_text("# buyer's own\ncollect_ignore = []\n")
    submission = Submission(
        submission_sha="7" * 40,
        changes=(FileChange("src/calc.py", ChangeKind.MODIFY, FIXED_CALC.encode()),),
    )
    workspace = assemble_workspace(
        base_tree=base_tree,
        submission=submission,
        contract=sealed.contract,
        grader_bundle=buyer_grader,
        destination=tmp_path / "ws",
    )
    assert (workspace.root / "conftest.py").exists()
    assert workspace.quarantined_hooks == ()


def test_modifying_the_buyers_hook_is_quarantined(
    sealed: SealedContract, base_tree: Path, buyer_grader: Path, tmp_path: Path
) -> None:
    """...but editing that same file is a provider change, and goes."""
    (base_tree / "src" / "conftest.py").write_text("# buyer's own\n")
    submission = Submission(
        submission_sha="8" * 40,
        changes=(FileChange("src/conftest.py", ChangeKind.MODIFY, b"# provider's now\n"),),
    )
    workspace = assemble_workspace(
        base_tree=base_tree,
        submission=submission,
        contract=sealed.contract,
        grader_bundle=buyer_grader,
        destination=tmp_path / "ws",
    )
    assert "src/conftest.py" in workspace.quarantined_hooks
    assert not (workspace.root / "src" / "conftest.py").exists()


# -- P1.2: .git gold-commit / history leakage ---------------------------------


def test_git_history_is_stripped(
    sealed: SealedContract, base_tree: Path, buyer_grader: Path, tmp_path: Path
) -> None:
    """P1.2 — the reference solution must not be readable from the sandbox."""
    submission = Submission(
        submission_sha="9" * 40,
        changes=(FileChange("src/calc.py", ChangeKind.MODIFY, FIXED_CALC.encode()),),
    )
    workspace = assemble_workspace(
        base_tree=base_tree,
        submission=submission,
        contract=sealed.contract,
        grader_bundle=buyer_grader,
        destination=tmp_path / "ws",
    )
    assert workspace.git_stripped
    assert not (workspace.root / ".git").exists()
    assert not list(workspace.root.rglob("GOLD_PATCH"))


def test_a_run_cannot_read_the_gold_patch(
    sealed: SealedContract, base_tree: Path, buyer_grader: Path, tmp_path: Path
) -> None:
    """The same claim, proven by executing code that tries to read it."""
    thief = (
        "from pathlib import Path\n"
        "\n"
        "def add(a, b):\n"
        "    gold = Path(__file__).resolve().parent.parent / '.git' / 'GOLD_PATCH'\n"
        "    if gold.exists():\n"
        "        ns = {}\n"
        "        exec(gold.read_text(), ns)\n"
        "        return ns['add'](a, b)\n"
        "    return 0\n"
    )
    submission = Submission(
        submission_sha="a" * 40,
        changes=(FileChange("src/calc.py", ChangeKind.MODIFY, thief.encode()),),
    )
    manifest = _run(sealed, submission, base_tree, buyer_grader, tmp_path)
    # With history stripped, the fallback returns 0 and the grader fails it.
    assert manifest.verdict is Verdict.FAIL


# -- P1.3: protected paths, integrated ----------------------------------------


def test_passing_code_that_touches_a_protected_path_still_fails(
    sealed: SealedContract, base_tree: Path, buyer_grader: Path, tmp_path: Path
) -> None:
    """The refund-flow case: functionally correct, but it disabled the CI gate.

    This is the demo's FAIL branch. Tests would have passed; the submission is
    rejected anyway, and the receipt can name the term.
    """
    submission = Submission(
        submission_sha="b" * 40,
        changes=(
            FileChange("src/calc.py", ChangeKind.MODIFY, FIXED_CALC.encode()),
            FileChange(".github/workflows/deploy.yml", ChangeKind.MODIFY, b"on: []\njobs: {}\n"),
        ),
    )
    manifest = _run(sealed, submission, base_tree, buyer_grader, tmp_path)

    assert manifest.verdict is Verdict.FAIL
    assert "protected_path" in manifest.failed_terms
    assert ".github/workflows/deploy.yml" in manifest.rejection_reason
    assert manifest.commands == ()


# -- integrity of the evaluator's own inputs ----------------------------------


def test_swapped_grader_bundle_is_refused(
    sealed: SealedContract, base_tree: Path, buyer_grader: Path, tmp_path: Path
) -> None:
    """A bundle that is not the pinned one must not produce an ordinary FAIL.

    FAIL refunds and closes the task, which would quietly bury the fact that the
    verifier was fed the wrong grader.
    """
    from mergegate.contract import ContractError

    (buyer_grader / "tests" / "test_calc.py").write_text("def test_nothing():\n    assert True\n")
    submission = Submission(
        submission_sha="c" * 40,
        changes=(FileChange("src/calc.py", ChangeKind.MODIFY, FIXED_CALC.encode()),),
    )
    with pytest.raises(ContractError, match="does not match the hash pinned"):
        _run(sealed, submission, base_tree, buyer_grader, tmp_path)


def test_tree_hash_binds_the_graded_workspace(
    sealed: SealedContract, base_tree: Path, buyer_grader: Path, tmp_path: Path
) -> None:
    """P0.4 groundwork: two different submissions cannot share a tree hash."""
    first = _run(
        sealed,
        Submission("d" * 40, (FileChange("src/calc.py", ChangeKind.MODIFY, FIXED_CALC.encode()),)),
        base_tree,
        buyer_grader,
        tmp_path,
    )
    second = _run(
        sealed,
        Submission("e" * 40, (FileChange("src/calc.py", ChangeKind.MODIFY, BROKEN_CALC.encode()),)),
        base_tree,
        buyer_grader,
        tmp_path,
    )
    assert first.tree_hash != second.tree_hash
    assert first.result_digest != second.result_digest
