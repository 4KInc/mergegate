"""P1.3: protected / graded path enforcement.

These tests encode the rule that makes the whole trust model hold: a submission
that touches a protected or graded path is rejected *regardless of test
results*. Passing tests never rescue a path violation.
"""

from __future__ import annotations

import pytest

from mergegate.contract import TaskContract
from mergegate.paths import (
    PathGuard,
    PathSyntaxError,
    ViolationKind,
    normalize_path,
)


@pytest.fixture
def guard(contract: TaskContract) -> PathGuard:
    return PathGuard.from_contract(contract)


def test_allowed_source_path_passes(guard: PathGuard) -> None:
    assert guard.classify("src/calc.py") is None
    assert guard.classify("src/deep/nested/module.py") is None


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/deploy.yml",
        "deploy/production.yaml",
        "Dockerfile",
    ],
)
def test_protected_path_is_rejected(guard: PathGuard, path: str) -> None:
    violation = guard.classify(path)
    assert violation is not None
    assert violation.kind is ViolationKind.PROTECTED_PATH


@pytest.mark.parametrize("path", ["tests/test_contract.py", "conftest.py"])
def test_grader_path_is_rejected(guard: PathGuard, path: str) -> None:
    violation = guard.classify(path)
    assert violation is not None
    assert violation.kind is ViolationKind.GRADER_PATH


def test_unlisted_path_is_rejected(guard: PathGuard) -> None:
    """Nothing is writable by default. The allow-list is the whole authorization."""
    violation = guard.classify("scripts/release.sh")
    assert violation is not None
    assert violation.kind is ViolationKind.OUTSIDE_ALLOWED


def test_deny_wins_over_allow() -> None:
    """A path on both lists is a violation: there is no most-specific-wins rule
    for an attacker to engineer against."""
    guard = PathGuard(
        allowed_source_paths=("src/**",),
        protected_paths=("src/generated/**",),
        grader_paths=("tests/**",),
    )
    assert guard.classify("src/ok.py") is None
    violation = guard.classify("src/generated/pb2.py")
    assert violation is not None
    assert violation.kind is ViolationKind.PROTECTED_PATH


@pytest.mark.parametrize(
    "path",
    [
        "../../etc/passwd",
        "src/../.github/workflows/deploy.yml",
        "/etc/passwd",
        "C:/Windows/system32",
        "src\\..\\.github\\deploy.yml",
        "src/\x00evil.py",
        "",
    ],
)
def test_unsafe_paths_are_rejected_not_sanitized(guard: PathGuard, path: str) -> None:
    """Traversal and absolute forms are refused outright. Rewriting them into
    something benign is how path guards get bypassed."""
    violation = guard.classify(path)
    assert violation is not None
    assert violation.kind is ViolationKind.UNSAFE_PATH


def test_traversal_cannot_reach_a_protected_path(guard: PathGuard) -> None:
    """The specific attack: dress a protected path up as an allowed one."""
    assert guard.classify("src/../.github/workflows/deploy.yml") is not None
    assert guard.classify("src/./../../deploy/prod.yaml") is not None


def test_normalize_collapses_redundant_segments() -> None:
    assert normalize_path("src/./calc.py") == "src/calc.py"
    assert normalize_path("src//calc.py") == "src/calc.py"
    with pytest.raises(PathSyntaxError):
        normalize_path("src/../x.py")


def test_bare_directory_pattern_protects_the_subtree() -> None:
    guard = PathGuard(
        allowed_source_paths=("src/**",),
        protected_paths=(".github",),
        grader_paths=("tests",),
    )
    assert guard.classify(".github/workflows/ci.yml") is not None
    assert guard.classify("tests/unit/test_x.py") is not None


def test_report_names_the_failed_term(guard: PathGuard) -> None:
    """P2.1: the refund receipt must name the exact failed contract term."""
    report = guard.evaluate(
        ["src/calc.py", ".github/workflows/deploy.yml", "tests/test_contract.py"]
    )
    assert not report.ok
    assert report.accepted == ("src/calc.py",)
    assert report.failed_terms == ("grader_path", "protected_path")
    assert ".github/workflows/deploy.yml" in report.summary()
    assert "contract-protected path" in report.summary()


def test_clean_submission_reports_ok(guard: PathGuard) -> None:
    report = guard.evaluate(["src/calc.py", "src/util.py"])
    assert report.ok
    assert report.failed_terms == ()
