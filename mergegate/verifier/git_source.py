"""Producing the verifier's inputs from a real repository.

Two jobs, both narrow on purpose:

* materialize the buyer's tree at the contract's **pinned base SHA**, and
* reduce the provider's commit to an explicit list of
  :class:`~mergegate.submission.FileChange` records.

Everything here runs before the sandbox and treats the repository as hostile
input. Git is invoked as argv vectors with ``shell=False``; no ref name,
branch, or path from the provider is ever interpolated into a command string.
Refs are resolved to SHAs and then only SHAs are used, because a ref can be
moved between the moment it is read and the moment it is checked out.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..submission import ChangeKind, FileChange, Submission

__all__ = ["GitError", "materialize_base_tree", "build_submission", "resolve_sha"]

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_GIT_TIMEOUT = 300


class GitError(RuntimeError):
    """A git operation failed, or returned something we refuse to trust."""


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(  # noqa: S603 - argv vector, shell=False
        ["git", "-C", str(repo), *args],
        capture_output=True,
        timeout=_GIT_TIMEOUT,
        shell=False,
        check=False,
    )
    if proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.decode('utf-8', errors='replace').strip()}"
        )
    return proc.stdout.decode("utf-8", errors="replace")


def _git_bytes(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(  # noqa: S603 - argv vector, shell=False
        ["git", "-C", str(repo), *args],
        capture_output=True,
        timeout=_GIT_TIMEOUT,
        shell=False,
        check=False,
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode})")
    return proc.stdout


def resolve_sha(repo: Path, rev: str) -> str:
    """Resolve a revision to a full 40-hex SHA, or raise.

    Callers should resolve once and pass the SHA onward. A branch name is not
    an artifact identity; it can point somewhere else a second later.
    """
    out = _git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()
    if not _SHA_RE.match(out):
        raise GitError(f"git resolved {rev!r} to something that is not a commit SHA: {out!r}")
    return out


def materialize_base_tree(*, repo: Path, base_sha: str, destination: Path) -> Path:
    """Write the tree at ``base_sha`` into ``destination``.

    Uses ``git archive``, which emits the tree contents and nothing else: no
    ``.git`` directory is created, so the reference solution is never present to
    be stripped later (P1.2 by construction rather than by cleanup).
    """
    if not _SHA_RE.match(base_sha):
        raise GitError(f"base_sha must be a full 40-hex SHA before materializing, got {base_sha!r}")
    # Confirm the SHA exists and is a commit in this repo before trusting it.
    resolved = resolve_sha(repo, base_sha)
    if resolved != base_sha:
        raise GitError(f"{base_sha} did not resolve to itself (got {resolved})")

    destination.mkdir(parents=True, exist_ok=True)
    archive = _git_bytes(repo, "archive", "--format=tar", base_sha)
    _extract_tar(archive, destination)
    return destination


def _extract_tar(data: bytes, destination: Path) -> None:
    """Extract a tar stream, refusing any member that escapes ``destination``."""
    import io
    import tarfile

    root = destination.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if not target.is_relative_to(root):
                raise GitError(f"archive member escapes the destination: {member.name!r}")
            if member.issym() or member.islnk():
                link_target = (target.parent / member.linkname).resolve()
                if not link_target.is_relative_to(root):
                    raise GitError(f"archive link escapes the destination: {member.name!r}")
        tar.extractall(destination, filter="data")


def build_submission(*, repo: Path, base_sha: str, submission_sha: str) -> Submission:
    """Reduce the diff between two commits to explicit file changes.

    Renames and copies are decomposed into a delete plus an add. The path guard
    has to see every path the submission writes, and a rename recorded as one
    ``R`` entry hides the destination from a naive reader.
    """
    if not _SHA_RE.match(submission_sha):
        raise GitError(f"submission_sha must be a full 40-hex SHA, got {submission_sha!r}")

    raw = _git(
        repo,
        "diff",
        "--name-status",
        "--no-renames",  # emit rename as delete + add, so both paths are visible
        "-z",
        base_sha,
        submission_sha,
    )

    changes: list[FileChange] = []
    fields = [f for f in raw.split("\0") if f != ""]
    i = 0
    while i < len(fields):
        status = fields[i]
        path = fields[i + 1] if i + 1 < len(fields) else ""
        i += 2
        if not path:
            continue
        code = status[0]
        if code == "D":
            changes.append(FileChange(path=path, kind=ChangeKind.DELETE))
            continue
        kind = ChangeKind.ADD if code == "A" else ChangeKind.MODIFY
        content = _git_bytes(repo, "show", f"{submission_sha}:{path}")
        changes.append(
            FileChange(
                path=path,
                kind=kind,
                content=content,
                executable=_is_executable(repo, submission_sha, path),
            )
        )

    return Submission(submission_sha=submission_sha, changes=tuple(changes))


def _is_executable(repo: Path, sha: str, path: str) -> bool:
    out = _git(repo, "ls-tree", sha, "--", path).strip()
    return bool(out) and out.split()[0] == "100755"
