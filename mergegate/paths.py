"""P1.3 — protected / graded path enforcement.

This is a security boundary, not a convenience filter. A provider diff that
touches a protected path (CI config, deploy manifests, infrastructure) or any
grader path is a **hard reject regardless of test results** — a submission that
disables the deploy gate and then passes the unit tests has not satisfied the
contract, it has routed around it.

The matcher is deliberately conservative:

* Paths are normalized and rejected outright if they attempt traversal, use
  absolute or Windows-style forms, or contain NUL. Ambiguity is a reject, never
  a best-effort interpretation.
* Classification is deny-biased: a path that matches both an allow pattern and a
  protected pattern is a violation. There is no "most specific wins" rule to
  reason about, because attackers reason about those rules too.
* A path matching nothing at all is also a violation. The allow-list is
  exhaustive by construction (:class:`~mergegate.contract.TaskContract` refuses
  an empty one), so "unclassified" means "not authorized".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "PathGuard",
    "GuardReport",
    "PathViolation",
    "ViolationKind",
    "compile_patterns",
    "match_any",
    "normalize_path",
    "PathSyntaxError",
]


class PathSyntaxError(ValueError):
    """Raised when a submitted path is not a safe, repo-relative POSIX path."""


class ViolationKind(StrEnum):
    PROTECTED_PATH = "protected_path"
    GRADER_PATH = "grader_path"
    OUTSIDE_ALLOWED = "outside_allowed_source_paths"
    UNSAFE_PATH = "unsafe_path"

    def describe(self) -> str:
        return {
            ViolationKind.PROTECTED_PATH: "modifies a contract-protected path",
            ViolationKind.GRADER_PATH: "modifies a buyer-pinned grader path",
            ViolationKind.OUTSIDE_ALLOWED: "falls outside the allowed source paths",
            ViolationKind.UNSAFE_PATH: "is not a safe repo-relative path",
        }[self]


@dataclass(frozen=True, slots=True)
class PathViolation:
    path: str
    kind: ViolationKind
    pattern: str | None = None

    @property
    def reason(self) -> str:
        """Human-readable, and precise enough to name the failed contract term.

        The refund receipt quotes this string verbatim (P2.1), so it must
        identify *which* term failed, not merely that something failed.
        """
        suffix = f" (pattern: {self.pattern})" if self.pattern else ""
        return f"{self.path} {self.kind.describe()}{suffix}"


@dataclass(frozen=True, slots=True)
class GuardReport:
    violations: tuple[PathViolation, ...]
    accepted: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def failed_terms(self) -> tuple[str, ...]:
        """Distinct contract terms this submission violated, for the receipt."""
        return tuple(sorted({v.kind.value for v in self.violations}))

    def summary(self) -> str:
        if self.ok:
            return f"{len(self.accepted)} path(s) within allowed source paths"
        return "; ".join(v.reason for v in self.violations)


def normalize_path(raw: str) -> str:
    """Normalize a repo-relative POSIX path, or raise :class:`PathSyntaxError`.

    Rejects rather than sanitizes. Silently rewriting ``a/../../etc/passwd``
    into something benign is how path guards get bypassed; refusing to classify
    it at all is the safe posture.
    """
    if not raw or not raw.strip():
        raise PathSyntaxError("empty path")
    if "\x00" in raw:
        raise PathSyntaxError(f"path contains NUL: {raw!r}")
    if "\\" in raw:
        raise PathSyntaxError(f"backslashes are not permitted in repo paths: {raw!r}")
    if raw.startswith("/"):
        raise PathSyntaxError(f"absolute paths are not permitted: {raw!r}")
    if re.match(r"^[A-Za-z]:", raw):
        raise PathSyntaxError(f"drive-letter paths are not permitted: {raw!r}")

    parts: list[str] = []
    for segment in raw.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise PathSyntaxError(f"path traversal is not permitted: {raw!r}")
        parts.append(segment)

    if not parts:
        raise PathSyntaxError(f"path resolves to nothing: {raw!r}")
    return "/".join(parts)


def _compile(pattern: str) -> re.Pattern[str]:
    """Translate a gitignore-style glob into an anchored regex.

    Supported: ``**`` (any depth, including none), ``*`` (within one segment),
    ``?`` (one character), and a trailing ``/`` meaning "this directory and
    everything under it". A bare directory name with no wildcard also matches
    everything beneath it, so ``.github`` protects the whole tree.
    """
    if pattern.endswith("/"):
        pattern = pattern.rstrip("/") + "/**"

    out: list[str] = ["^"]
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("/**", i):
            # trailing "/**" -> the directory itself or anything under it
            out.append("(?:/.*)?")
            i += 3
        elif pattern.startswith("**", i):
            # bare or trailing "**" -> any path, at any depth. Must be handled
            # before the single-"*" case, which is segment-local and would
            # compile "**" into something that matches nothing with a slash.
            out.append(".*")
            i += 2
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    # A pattern with no wildcard names either an exact file or a directory
    # prefix; both should match, so allow an optional subtree suffix.
    if not any(tok in pattern for tok in ("*", "?")):
        out.append("(?:/.*)?")
    out.append("$")
    return re.compile("".join(out))


def compile_patterns(patterns: tuple[str, ...] | list[str]) -> list[tuple[str, re.Pattern[str]]]:
    """Compile globs once, keeping each source pattern for error reporting."""
    return [(p, _compile(p)) for p in patterns]


def match_any(path: str, compiled: list[tuple[str, re.Pattern[str]]]) -> str | None:
    """Return the first pattern matching ``path``, or ``None``.

    For callers that need a plain "is this path in this set?" question without
    the guard's allow/deny precedence — asking a deny-biased classifier and then
    reinterpreting its answer is how a check ends up meaning something other
    than what its caller assumed.
    """
    for pattern, rx in compiled:
        if rx.match(path):
            return pattern
    return None


class PathGuard:
    """Classifies provider-touched paths against a sealed contract's terms."""

    def __init__(
        self,
        *,
        allowed_source_paths: tuple[str, ...] | list[str],
        protected_paths: tuple[str, ...] | list[str],
        grader_paths: tuple[str, ...] | list[str],
    ) -> None:
        self._allowed = [(p, _compile(p)) for p in allowed_source_paths]
        self._protected = [(p, _compile(p)) for p in protected_paths]
        self._grader = [(p, _compile(p)) for p in grader_paths]

    @classmethod
    def from_contract(cls, contract: object) -> PathGuard:
        return cls(
            allowed_source_paths=contract.allowed_source_paths,  # type: ignore[attr-defined]
            protected_paths=contract.protected_paths,  # type: ignore[attr-defined]
            grader_paths=contract.grader_paths,  # type: ignore[attr-defined]
        )

    def classify(self, raw_path: str) -> PathViolation | None:
        """Return a violation, or ``None`` if the path is legitimately writable."""
        try:
            path = normalize_path(raw_path)
        except PathSyntaxError:
            return PathViolation(path=raw_path, kind=ViolationKind.UNSAFE_PATH)

        # Deny checks run first and unconditionally. Being on the allow-list
        # does not rescue a path that is also protected or graded.
        for pattern, rx in self._protected:
            if rx.match(path):
                return PathViolation(path, ViolationKind.PROTECTED_PATH, pattern)
        for pattern, rx in self._grader:
            if rx.match(path):
                return PathViolation(path, ViolationKind.GRADER_PATH, pattern)
        for _pattern, rx in self._allowed:
            if rx.match(path):
                return None
        return PathViolation(path, ViolationKind.OUTSIDE_ALLOWED)

    def evaluate(self, paths: list[str] | tuple[str, ...]) -> GuardReport:
        violations: list[PathViolation] = []
        accepted: list[str] = []
        for raw in paths:
            verdict = self.classify(raw)
            if verdict is None:
                accepted.append(normalize_path(raw))
            else:
                violations.append(verdict)
        return GuardReport(violations=tuple(violations), accepted=tuple(accepted))
