"""The verification manifest: what was run, on what, and what came out.

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
from typing import Any

from ..engine import canonicalize
from ..hashing import OUTPUT_DOMAIN, RESULT_DOMAIN, digest, hash_object
from .sandbox import EGRESS_UNRESTRICTED

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
    of exit codes: a path violation is not rescued by passing tests."""

    rejection_reason: str = ""
    """Human-readable, names the exact failed term. Quoted by the refund receipt."""

    tamper_signals: tuple[str, ...] = field(default_factory=tuple)
    """P1.5: quarantined provider hooks, purged grader files, env-sniffing.
    Recorded rather than silently fixed up."""

    git_stripped: bool = True
    egress_policy: str = EGRESS_UNRESTRICTED
    """Recorded into the signed receipt, so it must state what was true of the
    environment that actually graded this submission.

    The default is the unrestricted one, and that is the whole point. This field
    used to default to the sealed sandbox's posture while grading ran in the
    calling process, which meant every receipt asserted an isolation it did not
    have. A caller that genuinely ran inside the sealed job passes that posture
    in; everything else understates rather than overstates."""

    @property
    def verdict(self) -> Verdict:
        """Computed, never assigned.

        FAIL if the submission violated a contract term, or if any pinned
        command exited non-zero or timed out. PASS only when nothing failed and
        at least one command actually ran: an empty command list is not a pass.
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

    @classmethod
    def from_canonical_dict(cls, data: dict[str, Any]) -> VerificationManifest:
        """Rebuild a manifest a sealed job produced.

        The inverse of :meth:`to_canonical_dict`, needed because grading now
        happens in another process and the result crosses back as JSON. The
        round trip has to preserve ``result_digest`` exactly: that digest is
        what the receipt binds, so a manifest that changed while being read
        would bind a run that never happened.

        ``verdict`` is deliberately dropped rather than restored. It is a
        computed property of the failed terms and command results, and
        accepting it from the wire would let a job assert a verdict its own
        recorded evidence does not support.
        """
        commands = tuple(
            CommandResult(
                argv=tuple(c["argv"]),
                exit_code=int(c["exit_code"]),
                stdout_digest=str(c["stdout_digest"]),
                stderr_digest=str(c["stderr_digest"]),
                duration_ms=int(c.get("duration_ms", 0)),
                timed_out=bool(c.get("timed_out", False)),
            )
            for c in data.get("commands", [])
        )
        return cls(
            task_id=str(data["task_id"]),
            contract_hash=str(data["contract_hash"]),
            grader_hash=str(data["grader_hash"]),
            base_sha=str(data["base_sha"]),
            submission_sha=str(data["submission_sha"]),
            tree_hash=str(data["tree_hash"]),
            verifier_image_digest=str(data["verifier_image_digest"]),
            commands=commands,
            failed_terms=tuple(data.get("failed_terms", [])),
            rejection_reason=str(data.get("rejection_reason", "")),
            tamper_signals=tuple(data.get("tamper_signals", [])),
            git_stripped=bool(data.get("git_stripped", True)),
            egress_policy=str(data.get("egress_policy", EGRESS_UNRESTRICTED)),
        )

    @property
    def result_digest(self) -> str:
        """The single hash the receipt binds this whole run to."""
        return hash_object(RESULT_DOMAIN, self.to_canonical_dict())


def digest_stream(data: bytes) -> str:
    """Digest a captured output stream."""
    return digest(OUTPUT_DOMAIN, data)
