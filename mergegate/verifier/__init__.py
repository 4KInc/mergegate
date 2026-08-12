"""P0.3 — the neutral, provable sandbox verifier.

Split deliberately in two:

* :mod:`~mergegate.verifier.workspace` and :mod:`~mergegate.verifier.runner`
  hold the logic whose correctness the trust model depends on — grader
  injection, path enforcement, hook quarantine, ``.git`` stripping, command
  execution, result digests. Pure local work, fully exercised in CI.
* :mod:`~mergegate.verifier.sandbox` holds the GCP substrate that executes that
  logic under gVisor with default-deny egress.

Keeping them apart means the neutrality claims are proven by tests rather than
by pointing at a container spec.
"""

from __future__ import annotations

from .manifest import CommandResult, Verdict, VerificationManifest
from .runner import run_pinned_commands
from .workspace import AssembledWorkspace, WorkspaceRejectedError, assemble_workspace

__all__ = [
    "AssembledWorkspace",
    "CommandResult",
    "VerificationManifest",
    "Verdict",
    "WorkspaceRejectedError",
    "assemble_workspace",
    "run_pinned_commands",
]
