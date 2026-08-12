"""The verification manifest — what was run, on what, and what came out.

The manifest is the evidence object the receipt binds to (P0.7). It records the
full verification identity (base SHA + grader hash + submission SHA + tree hash
+ verifier image digest), the exact commands, their exit codes, digests of their
output, and the resulting verdict.

Two design rules matter here:

* **The verdict is a function of the manifest, not a field someone sets.**
  :meth:`VerificationManifest.verdict` is computed from path violations and exit
  codes. There is no code path that stamps PASS onto a run that did not earn it.
* **Command output is bound by digest, not by content.** Test output can be
  large and can contain whatever the provider's code printed; the receipt
  commits to a digest so tampering is detectable without the receipt carrying
  megabytes of attacker-controlled text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ..engine import canonicalize
from ..hashing import OUTPUT_DOMAIN, RESULT_DOMAIN, digest, hash_object

__all__ = ["CommandResult", "VerificationManifest", "Verdict"]

# Bound so one pathological command cannot produce a multi-gigabyte artifact.
# The digest still covers the full stream; only the retained excerpt is capped.
MAX_RETAINED_OUTPUT = 64 * 1024


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"

    @property
    def releases_escrow(self) -> bool:
        return self is Verdict.PASS


@dataclass(frozen=True, slots=True)
class CommandResult:
    """One pinned command's execution record."""

    argv: tuple[str, ...]
    exit_code: int
    stdout_digest: str
    stderr_digest: str
    duration_ms: int
    stdout_excerpt: str = ""
    stderr_excerpt: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "stdout_digest": self.stdout_digest,
            "stderr_digest": self.stderr_digest,
            "timed_out": self.timed_out,
        }


@dataclass(frozen=True, slots=True)
class VerificationManifest:
    """The complete, reproducible record of one evaluation run."""

    task_id: str
    contract_hash: str
    grader_hash: str
    base_sha: str
    submission_sha: str
    tree_hash: str
    verifier_image_digest: str
    commands: tuple[CommandResult, ...] = field(default_factory=tuple)
    failed_terms: tuple[str, ...] = field(default_factory=tuple)
    """Contract terms the submission violated. Non-empty forces FAIL regardless
    of exit codes — a path violation is not rescued by passing tests."""

    rejection_reason: str = ""
    """Human-readable, names the exact failed term. Quoted by the refund receipt."""

    tamper_signals: tuple[str, ...] = field(default_factory=tuple)
    """P1.5 — quarantined provider hooks, purged grader files, env-sniffing.
    Recorded rather than silently fixed up."""

    git_stripped: bool = True
    egress_policy: str = "default-deny"

    @property
    def verdict(self) -> Verdict:
        """Computed, never assigned.

        FAIL if the submission violated a contract term, or if any pinned
        command exited non-zero or timed out. PASS only when nothing failed and
        at least one command actually ran — an empty command list is not a pass.
        """
        if self.failed_terms:
            return Verdict.FAIL
        if not self.commands:
            return Verdict.FAIL
        if any(not cmd.ok for cmd in self.commands):
            return Verdict.FAIL
        return Verdict.PASS

    @property
    def command_output_digest(self) -> str:
        """One digest over every command's output streams, in pinned order."""
        payload = canonicalize(
            [
                {
                    "argv": list(cmd.argv),
                    "stdout_digest": cmd.stdout_digest,
                    "stderr_digest": cmd.stderr_digest,
                }
                for cmd in self.commands
            ]
        )
        return digest(OUTPUT_DOMAIN, payload)

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "contract_hash": self.contract_hash,
            "grader_hash": self.grader_hash,
            "base_sha": self.base_sha,
            "submission_sha": self.submission_sha,
            "tree_hash": self.tree_hash,
            "verifier_image_digest": self.verifier_image_digest,
            "commands": [cmd.to_canonical_dict() for cmd in self.commands],
            "command_output_digest": self.command_output_digest,
            "failed_terms": sorted(self.failed_terms),
            "rejection_reason": self.rejection_reason,
            "tamper_signals": sorted(self.tamper_signals),
            "git_stripped": self.git_stripped,
            "egress_policy": self.egress_policy,
            "verdict": self.verdict.value,
        }

    @property
    def result_digest(self) -> str:
        """The single hash the receipt binds this whole run to."""
        return hash_object(RESULT_DOMAIN, self.to_canonical_dict())


def digest_stream(data: bytes) -> str:
    """Digest a captured output stream."""
    return digest(OUTPUT_DOMAIN, data)
