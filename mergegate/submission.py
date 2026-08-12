"""The provider's submission, expressed as explicit file changes.

MergeGate deliberately does **not** shell out to ``patch`` or ``git apply`` with
attacker-controlled input. A submission is reduced to a list of
:class:`FileChange` records — path plus new content, or path plus a deletion
marker — before anything touches the workspace. Every path then flows through
the same :class:`~mergegate.paths.PathGuard` as everything else.

In production these records come from ``git diff --name-status`` between the
pinned base SHA and the provider's submission SHA, with blob contents read out
of the object store (see :mod:`mergegate.verifier.git_source`). Keeping the
in-memory form separate from the git plumbing is what makes the neutrality
guarantees testable without a repository.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .paths import normalize_path

__all__ = ["ChangeKind", "FileChange", "Submission"]


class ChangeKind(StrEnum):
    ADD = "add"
    MODIFY = "modify"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class FileChange:
    """One file the provider wants to add, modify, or delete.

    ``path`` is normalized at construction, so a change whose path is unsafe
    fails loudly here rather than being classified later against a form that no
    longer resembles what the provider actually wrote.
    """

    path: str
    kind: ChangeKind
    content: bytes | None = None
    executable: bool = False

    def __post_init__(self) -> None:
        if self.kind is ChangeKind.DELETE:
            if self.content is not None:
                raise ValueError(f"deletion of {self.path!r} must not carry content")
        elif self.content is None:
            raise ValueError(f"{self.kind.value} of {self.path!r} requires content")

    @property
    def safe_path(self) -> str:
        """Normalized path. Raises :class:`~mergegate.paths.PathSyntaxError`."""
        return normalize_path(self.path)


@dataclass(frozen=True, slots=True)
class Submission:
    """A provider submission pinned to one exact commit.

    ``submission_sha`` is the artifact identity. It is bound into the
    verification result and the receipt, so a later force-push produces a
    different SHA and cannot inherit this submission's verdict (P0.4).
    """

    submission_sha: str
    changes: tuple[FileChange, ...] = field(default_factory=tuple)

    @property
    def touched_paths(self) -> tuple[str, ...]:
        """Every path this submission writes to, in submission order.

        Returned raw — *not* normalized — because the path guard has to see and
        reject the provider's original form. Normalizing here would launder
        traversal attempts into innocuous-looking paths before the check.
        """
        return tuple(change.path for change in self.changes)
