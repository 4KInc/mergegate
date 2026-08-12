"""Producing verifier inputs from a real git repository.

These build actual repositories with actual commits. Mocking git here would
test our idea of git's output rather than git's output.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from mergegate.submission import ChangeKind
from mergegate.verifier.git_source import (
    GitError,
    build_submission,
    materialize_base_tree,
    resolve_sha,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _run(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, "init", "-q", "-b", "main")
    _run(root, "config", "user.email", "test@example.com")
    _run(root, "config", "user.name", "Test")
    (root / "src").mkdir()
    (root / "src" / "calc.py").write_text("def add(a, b):\n    return 0\n")
    (root / "README.md").write_text("# demo\n")
    _run(root, "add", "-A")
    _run(root, "commit", "-q", "-m", "base")
    return root


def _commit(repo: Path, message: str) -> str:
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", message)
    return _run(repo, "rev-parse", "HEAD").strip()


def test_resolve_sha_returns_full_sha(repo: Path) -> None:
    sha = resolve_sha(repo, "main")
    assert len(sha) == 40
    assert resolve_sha(repo, sha) == sha


def test_resolve_sha_rejects_unknown_ref(repo: Path) -> None:
    with pytest.raises(GitError):
        resolve_sha(repo, "no-such-branch")


def test_materialize_base_tree_has_no_git_directory(repo: Path, tmp_path: Path) -> None:
    """P1.2 by construction: git archive emits tree contents only."""
    base = resolve_sha(repo, "HEAD")
    out = materialize_base_tree(repo=repo, base_sha=base, destination=tmp_path / "base")

    assert (out / "src" / "calc.py").read_text() == "def add(a, b):\n    return 0\n"
    assert not (out / ".git").exists()


def test_materialize_rejects_a_branch_name(repo: Path, tmp_path: Path) -> None:
    """A ref is not an artifact identity — it can move underneath you."""
    with pytest.raises(GitError, match="full 40-hex SHA"):
        materialize_base_tree(repo=repo, base_sha="main", destination=tmp_path / "base")


def test_build_submission_captures_modifications(repo: Path) -> None:
    base = resolve_sha(repo, "HEAD")
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    head = _commit(repo, "fix")

    submission = build_submission(repo=repo, base_sha=base, submission_sha=head)

    assert submission.submission_sha == head
    assert len(submission.changes) == 1
    change = submission.changes[0]
    assert change.path == "src/calc.py"
    assert change.kind is ChangeKind.MODIFY
    assert change.content == b"def add(a, b):\n    return a + b\n"


def test_build_submission_captures_adds_and_deletes(repo: Path) -> None:
    base = resolve_sha(repo, "HEAD")
    (repo / "src" / "util.py").write_text("X = 1\n")
    (repo / "README.md").unlink()
    head = _commit(repo, "add and delete")

    submission = build_submission(repo=repo, base_sha=base, submission_sha=head)
    by_path = {c.path: c for c in submission.changes}

    assert by_path["src/util.py"].kind is ChangeKind.ADD
    assert by_path["src/util.py"].content == b"X = 1\n"
    assert by_path["README.md"].kind is ChangeKind.DELETE
    assert by_path["README.md"].content is None


def test_renames_are_decomposed_so_both_paths_are_visible(repo: Path) -> None:
    """A rename recorded as one entry hides the destination from the path guard."""
    base = resolve_sha(repo, "HEAD")
    _run(repo, "mv", "src/calc.py", "src/calculator.py")
    head = _commit(repo, "rename")

    submission = build_submission(repo=repo, base_sha=base, submission_sha=head)
    paths = {c.path: c.kind for c in submission.changes}

    assert paths["src/calc.py"] is ChangeKind.DELETE
    assert paths["src/calculator.py"] is ChangeKind.ADD
    # Both ends of the rename reach the guard.
    assert set(submission.touched_paths) == {"src/calc.py", "src/calculator.py"}


def test_paths_with_spaces_and_quotes_survive(repo: Path) -> None:
    """-z output means no quoting to misparse."""
    base = resolve_sha(repo, "HEAD")
    (repo / "src" / "a file with spaces.py").write_text("Y = 2\n")
    head = _commit(repo, "odd name")

    submission = build_submission(repo=repo, base_sha=base, submission_sha=head)
    assert "src/a file with spaces.py" in submission.touched_paths


def test_executable_bit_is_captured(repo: Path) -> None:
    base = resolve_sha(repo, "HEAD")
    script = repo / "src" / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    head = _commit(repo, "script")

    submission = build_submission(repo=repo, base_sha=base, submission_sha=head)
    change = next(c for c in submission.changes if c.path == "src/run.sh")
    assert change.executable


def test_build_submission_rejects_a_non_sha(repo: Path) -> None:
    base = resolve_sha(repo, "HEAD")
    with pytest.raises(GitError, match="full 40-hex SHA"):
        build_submission(repo=repo, base_sha=base, submission_sha="HEAD")
