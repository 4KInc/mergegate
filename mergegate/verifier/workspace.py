"""P0.3: deterministic workspace assembly under the verification identity.

    buyer base tree + buyer grader bundle + provider diff

The advantage MergeGate claims is not "it runs in a sandbox": plenty of things
run in sandboxes. It is that **the provider cannot influence the grader**, and
that this is demonstrable rather than asserted. Assembly is therefore ordered so
that the provider's contribution is always overwritten by the buyer's:

1. Materialize the pinned base tree.
2. Guard every path the submission touches. Any protected- or grader-path
   violation is a hard reject; the commands never run (P1.3).
3. Apply the provider's changes to allowed source paths only.
4. Quarantine test-hook files the provider introduced or modified anywhere in
   the tree, not just under the declared grader paths (P1.1).
5. Purge the grader paths outright, then inject the buyer's grader bundle, so
   the graded tests are always the buyer's bytes (P0.3 step 3).
6. Strip ``.git`` so no reference solution can be read out of history (P1.2).
7. Hash the resulting tree.

Steps 2 and 5 are deliberately redundant. Step 2 already rejects a submission
that edits a grader path, so step 5 should never have anything to purge: but
it runs anyway, because a defense that depends on a single check being correct
is a defense that fails when that check is wrong.

This module is pure filesystem work with no container or cloud dependency,
which is what makes every one of those claims testable in CI.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..contract import TaskContract
from ..hashing import TREE_DOMAIN, hash_directory
from ..paths import (
    GuardReport,
    PathGuard,
    PathSyntaxError,
    compile_patterns,
    match_any,
    normalize_path,
)
from ..submission import ChangeKind, Submission

__all__ = [
    "AssembledWorkspace",
    "WorkspaceRejectedError",
    "assemble_workspace",
    "TEST_HOOK_NAMES",
    "TEST_HOOK_SUFFIXES",
]

# Files that can change a test outcome without appearing in the test tree.
# A provider-supplied conftest.py can override assertions, monkeypatch the code
# under test, or register plugins; sitecustomize/usercustomize and .pth files
# execute at interpreter startup, before any test code is imported.
TEST_HOOK_NAMES: frozenset[str] = frozenset(
    {
        "conftest.py",
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
        "pyproject.toml",
        "sitecustomize.py",
        "usercustomize.py",
    }
)
TEST_HOOK_SUFFIXES: tuple[str, ...] = (".pth",)

_GIT_DIR = ".git"


class WorkspaceRejectedError(Exception):
    """The submission failed a contract term before any command was run.

    Carries the guard report so the refund receipt can name the exact failed
    term rather than reporting a generic failure.
    """

    def __init__(self, report: GuardReport) -> None:
        self.report = report
        super().__init__(report.summary())


@dataclass(frozen=True, slots=True)
class AssembledWorkspace:
    """A workspace ready for the pinned commands, plus what it took to get there."""

    root: Path
    tree_hash: str
    applied_paths: tuple[str, ...]
    injected_grader_files: tuple[str, ...]
    quarantined_hooks: tuple[str, ...] = field(default_factory=tuple)
    """Provider-introduced test hooks removed before grading. Non-empty is a
    tamper signal and is recorded in the receipt (P1.5), not a silent fixup."""

    purged_grader_paths: tuple[str, ...] = field(default_factory=tuple)
    """Grader-path files present after diff apply. Should always be empty given
    the path guard; if it is not, the guard has a hole worth knowing about."""

    git_stripped: bool = True
    grader_roots: tuple[Path, ...] = field(default_factory=tuple)
    """Concrete directories the buyer's grader occupies, for the runtime guard."""

    source_roots: tuple[Path, ...] = field(default_factory=tuple)
    """Concrete directories provider code occupies. Frames from here are
    forbidden to read the grader."""

    @property
    def tamper_signals(self) -> tuple[str, ...]:
        signals: list[str] = []
        for hook in self.quarantined_hooks:
            signals.append(f"provider-supplied test hook quarantined: {hook}")
        for path in self.purged_grader_paths:
            signals.append(f"provider file purged from grader path: {path}")
        return tuple(signals)


def assemble_workspace(
    *,
    base_tree: Path,
    submission: Submission,
    contract: TaskContract,
    grader_bundle: Path,
    destination: Path,
) -> AssembledWorkspace:
    """Build the graded workspace, or raise :class:`WorkspaceRejectedError`.

    ``base_tree`` is the buyer's repository at the contract's pinned base SHA.
    ``grader_bundle`` is the buyer's bundle, whose hash must already match the
    contract's ``grader_hash``: the caller verifies that before calling, so a
    swapped bundle cannot reach assembly.
    """
    guard = PathGuard.from_contract(contract)

    # -- 2. Guard before touching anything. A rejected submission never runs.
    report = guard.evaluate(submission.touched_paths)
    if not report.ok:
        raise WorkspaceRejectedError(report)

    # -- 1. Materialize the base tree.
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(base_tree, destination, symlinks=True)

    baseline_hooks = _find_hooks(destination)

    # -- 3. Apply provider changes.
    applied = _apply_changes(destination, submission)

    # -- 4. Quarantine provider-introduced or -modified hooks.
    quarantined = _quarantine_provider_hooks(
        destination, baseline_hooks=baseline_hooks, applied_paths=applied
    )

    # -- 5. Purge grader paths, then inject the buyer's bundle.
    purged = _purge_grader_paths(destination, contract, applied)
    injected = _inject_grader_bundle(destination, grader_bundle, contract)

    # -- 6. Strip .git.
    git_stripped = _strip_git(destination)

    # -- 7. Hash the result.
    tree_hash = hash_directory(destination, domain=TREE_DOMAIN)

    grader_roots = _existing_roots(destination, contract.grader_paths)
    source_roots = _existing_roots(destination, contract.allowed_source_paths)

    return AssembledWorkspace(
        root=destination,
        tree_hash=tree_hash,
        applied_paths=applied,
        injected_grader_files=injected,
        quarantined_hooks=quarantined,
        purged_grader_paths=purged,
        git_stripped=git_stripped,
        grader_roots=grader_roots,
        source_roots=source_roots,
    )


def _existing_roots(root: Path, patterns: tuple[str, ...]) -> tuple[Path, ...]:
    """Resolve glob patterns to the directories that actually exist.

    The runtime guard compares real filesystem paths, so a pattern like
    ``tests/**`` has to become the ``tests`` directory rather than staying a
    glob. Patterns matching nothing are dropped: a guard root that does not
    exist would silently protect nothing.
    """
    out: list[Path] = []
    for pattern in patterns:
        head = pattern.split("*", 1)[0].rstrip("/")
        if not head:
            continue
        candidate = root / head
        if candidate.exists():
            out.append(candidate)
    return tuple(dict.fromkeys(out))


# -- steps --------------------------------------------------------------------


def _apply_changes(root: Path, submission: Submission) -> tuple[str, ...]:
    applied: list[str] = []
    for change in submission.changes:
        rel = normalize_path(change.path)
        target = _resolve_within(root, rel)
        if change.kind is ChangeKind.DELETE:
            if target.is_file() or target.is_symlink():
                target.unlink()
            applied.append(rel)
            continue
        assert change.content is not None  # guaranteed by FileChange.__post_init__
        target.parent.mkdir(parents=True, exist_ok=True)
        # Replace rather than write through: if the base shipped a symlink at
        # this path, writing through it would escape the workspace.
        if target.is_symlink() or target.exists():
            target.unlink()
        target.write_bytes(change.content)
        if change.executable:
            target.chmod(0o755)
        applied.append(rel)
    return tuple(applied)


def _find_hooks(root: Path) -> dict[str, bytes]:
    """Map every test-hook file in the tree to its current contents."""
    found: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if _GIT_DIR in path.relative_to(root).parts:
            continue
        if _is_hook(path.name):
            found[path.relative_to(root).as_posix()] = path.read_bytes()
    return found


def _is_hook(name: str) -> bool:
    return name in TEST_HOOK_NAMES or name.endswith(TEST_HOOK_SUFFIXES)


def _quarantine_provider_hooks(
    root: Path, *, baseline_hooks: dict[str, bytes], applied_paths: tuple[str, ...]
) -> tuple[str, ...]:
    """Remove test hooks the provider introduced or changed.

    Hooks that were already in the buyer's base tree and were not touched by the
    submission are the *buyer's* and stay: a repo legitimately has a root
    ``conftest.py`` or pytest config. What gets removed is any hook the provider
    added, or one it modified, anywhere in the tree.

    This closes the gap the path guard alone leaves open: ``src/conftest.py``
    sits inside an allowed source path, so the guard permits it, yet pytest will
    still collect and execute it. Allowed-to-write is not allowed-to-grade.
    """
    applied = set(applied_paths)
    quarantined: list[str] = []
    for rel, current in sorted(_find_hooks(root).items()):
        was_present = rel in baseline_hooks
        unchanged = was_present and baseline_hooks[rel] == current
        if unchanged and rel not in applied:
            continue
        (root / rel).unlink()
        quarantined.append(rel)
    return tuple(quarantined)


def _purge_grader_paths(
    root: Path, contract: TaskContract, applied_paths: tuple[str, ...]
) -> tuple[str, ...]:
    """Delete everything under the contract's grader paths.

    Belt and braces behind the path guard: whatever is here, the buyer's bundle
    is about to be written over it, and nothing survives to be collected
    alongside the real tests.

    Everything at a grader path is deleted, but only *provider-written* files
    are returned: those are the tamper signal. The buyer's own base tree
    routinely has files at grader paths (that is where the bundle goes), and
    reporting those would cry wolf on every honest run.
    """
    applied = set(applied_paths)
    grader_patterns = compile_patterns(contract.grader_paths)
    purged: list[str] = []
    for path in sorted(root.rglob("*"), key=lambda p: p.as_posix(), reverse=True):
        rel = path.relative_to(root).as_posix()
        if _GIT_DIR in path.relative_to(root).parts:
            continue
        if match_any(rel, grader_patterns) is None:
            continue
        if path.is_file() or path.is_symlink():
            path.unlink()
            if rel in applied:
                purged.append(rel)
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    return tuple(purged)


def _inject_grader_bundle(
    root: Path, grader_bundle: Path, contract: TaskContract
) -> tuple[str, ...]:
    """Copy the buyer's grader bundle in, overwriting whatever is there.

    Runs *after* the provider's diff, which is the whole point: the bytes that
    get graded are the buyer's, regardless of what the provider submitted.
    """
    injected: list[str] = []
    for src in sorted(grader_bundle.rglob("*"), key=lambda p: p.as_posix()):
        if not src.is_file() or src.is_symlink():
            continue
        rel = src.relative_to(grader_bundle).as_posix()
        target = _resolve_within(root, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.exists():
            target.unlink()
        shutil.copy2(src, target)
        injected.append(rel)

    if not injected:
        raise ValueError(
            f"grader bundle {grader_bundle} contributed no files: "
            f"nothing would be graded for contract {contract.task_id}"
        )
    return tuple(injected)


def _strip_git(root: Path) -> bool:
    """P1.2: remove ``.git`` so history cannot leak the reference solution.

    Coding agents are documented as recovering gold patches and expected outputs
    from git history when it is reachable. Nothing in a pinned-command run needs
    the object store, so it does not survive into the graded workspace.
    """
    git_dir = root / _GIT_DIR
    if git_dir.is_dir():
        shutil.rmtree(git_dir)
        return True
    if git_dir.is_file():  # worktree/submodule gitlink
        git_dir.unlink()
        return True
    return False


def _resolve_within(root: Path, rel: str) -> Path:
    """Join ``rel`` onto ``root``, refusing anything that escapes the workspace.

    ``normalize_path`` already rejects traversal, so this is the second line of
    defense; it also catches escapes via symlinked parent directories, which a
    purely lexical check cannot see.
    """
    try:
        safe_rel = normalize_path(rel)
    except PathSyntaxError as exc:
        raise ValueError(f"refusing to resolve unsafe path {rel!r}: {exc}") from exc

    target = root / safe_rel
    resolved_root = root.resolve()
    # Resolve the deepest existing ancestor; the leaf itself may not exist yet.
    probe = target
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.resolve().is_relative_to(resolved_root):
        raise ValueError(f"path {rel!r} resolves outside the workspace via a symlinked parent")
    return target
