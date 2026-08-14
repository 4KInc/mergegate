"""The provider must not be able to read the buyer's grader at run time.

The path guard stops a provider *editing* the tests. It says nothing about
reading them, and reading turns out to be enough: a submission that implements
nothing can scrape the expected values out of the test file at import time and
answer from a lookup table. Every assertion passes, the code does nothing, and
the verdict is meaningless.

This was demonstrated against the system before the guard existed, not
hypothesised. The first test here is that exact attack.

The guard has to be narrow. pytest reads the test files constantly and the
grader may read its own fixtures; a blanket ban on opening grader paths would
fail every honest run instead of the dishonest one. So the tests below check
both directions, and the honest-submission case matters as much as the attack.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mergegate.contract import SealedContract, TaskContract, build_contract
from mergegate.submission import ChangeKind, FileChange, Submission
from mergegate.verifier.evaluate import evaluate
from mergegate.verifier.manifest import Verdict

from .conftest import IMAGE

GRADER = """\
from src.calc import add


def test_negatives():
    assert add(-1, -1) == -2


def test_positives():
    assert add(2, 2) == 4
"""

HONEST = "def add(a, b):\n    return a + b\n"

# Implements nothing. Reads the buyer's test file and answers from it.
CHEAT = """
import re
from pathlib import Path

_TABLE = {}
for _p in Path(__file__).resolve().parent.parent.rglob("test_*.py"):
    for _a, _b, _want in re.findall(
        r"add\\((-?\\d+),\\s*(-?\\d+)\\)\\s*==\\s*(-?\\d+)", _p.read_text()
    ):
        _TABLE[(int(_a), int(_b))] = int(_want)


def add(a, b):
    return _TABLE.get((a, b), 0)
"""

# Reads the grader lazily, at call time rather than import time, in case the
# guard only covered module import.
CHEAT_LAZY = """
import re
from pathlib import Path


def add(a, b):
    for _p in Path(__file__).resolve().parent.parent.rglob("test_*.py"):
        for _a, _b, _want in re.findall(
            r"add\\((-?\\d+),\\s*(-?\\d+)\\)\\s*==\\s*(-?\\d+)", _p.read_text()
        ):
            if (int(_a), int(_b)) == (a, b):
                return int(_want)
    return 0
"""


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
def grader(tmp_path: Path) -> Path:
    bundle = tmp_path / "grader"
    (bundle / "tests").mkdir(parents=True)
    (bundle / "tests" / "test_calc.py").write_text(GRADER)
    return bundle


@pytest.fixture
def sealed(grader: Path) -> SealedContract:
    contract: TaskContract = build_contract(
        grader_bundle=grader,
        task_id="task-confidentiality",
        repository="4KInc/demo-repo",
        base_sha="a" * 40,
        verifier_image_digest=IMAGE,
        required_commands=((sys.executable, "-m", "pytest", "-q"),),
        allowed_source_paths=("src/**",),
        protected_paths=(".github/**",),
        grader_paths=("tests/**", "conftest.py"),
        reward_usdc="1.00",
        buyer_agent="0xBUYER",
        provider_agent="0xPROVIDER",
        deadline=datetime.now(UTC) + timedelta(hours=1),
    )
    return contract.seal(funding_tx="0xfund", mandate_hash="sha256:" + "e" * 64)


def _run(sealed: SealedContract, base: Path, grader: Path, tmp: Path, source: str) -> Verdict:
    manifest = evaluate(
        sealed=sealed,
        submission=Submission(
            "1" * 40, (FileChange("src/calc.py", ChangeKind.MODIFY, source.encode()),)
        ),
        base_tree=base,
        grader_bundle=grader,
        destination=tmp / "workspace",
        timeout_seconds=120,
    )
    return manifest.verdict


def test_honest_submission_still_passes(
    sealed: SealedContract, base_tree: Path, grader: Path, tmp_path: Path
) -> None:
    """The control, and the one that constrains the guard.

    A guard that blocks pytest from reading its own test files would fail every
    honest run. If this breaks, the guard is too broad and the attack tests
    below prove nothing.
    """
    assert _run(sealed, base_tree, grader, tmp_path, HONEST) is Verdict.PASS


def test_reading_the_grader_at_import_time_is_blocked(
    sealed: SealedContract, base_tree: Path, grader: Path, tmp_path: Path
) -> None:
    """The attack as originally demonstrated: passed before the guard existed."""
    assert _run(sealed, base_tree, grader, tmp_path, CHEAT) is Verdict.FAIL


def test_reading_the_grader_at_call_time_is_blocked(
    sealed: SealedContract, base_tree: Path, grader: Path, tmp_path: Path
) -> None:
    """Deferring the read to call time must not evade a guard that only
    covered module import."""
    assert _run(sealed, base_tree, grader, tmp_path, CHEAT_LAZY) is Verdict.FAIL


def test_guard_lives_outside_the_workspace(
    sealed: SealedContract, base_tree: Path, grader: Path, tmp_path: Path
) -> None:
    """Inside the workspace it would be reachable by the diff and deletable by
    the grader-path purge."""
    _run(sealed, base_tree, grader, tmp_path, HONEST)
    workspace = tmp_path / "workspace"
    guard = tmp_path / "_mergegate_guard" / "sitecustomize.py"
    assert guard.is_file()
    assert not list(workspace.rglob("sitecustomize.py"))


def test_guard_roots_resolve_to_real_directories(
    sealed: SealedContract, base_tree: Path, grader: Path, tmp_path: Path
) -> None:
    """The hook compares filesystem paths, so a glob left unresolved would
    protect nothing while appearing configured."""
    from mergegate.verifier.workspace import assemble_workspace

    workspace = assemble_workspace(
        base_tree=base_tree,
        submission=Submission(
            "2" * 40, (FileChange("src/calc.py", ChangeKind.MODIFY, HONEST.encode()),)
        ),
        contract=sealed.contract,
        grader_bundle=grader,
        destination=tmp_path / "ws2",
    )
    assert workspace.grader_roots and all(p.exists() for p in workspace.grader_roots)
    assert workspace.source_roots and all(p.exists() for p in workspace.source_roots)
    assert not any("*" in str(p) for p in workspace.grader_roots + workspace.source_roots)
