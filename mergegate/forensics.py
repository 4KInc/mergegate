"""Role 2: Gemini explains a FAIL after the refund is already decided.

A raw failure is close to useless to the agent that has to act on it. Exit code
1 and a stdout dump say that something broke, not what to change. For a provider
agent deciding whether to retry, that gap is the difference between a cheap
second attempt and abandoning the task.

**Ordering is the safety property.** This runs after the verdict exists and
after the refund directive is derived. It cannot influence either, and it cannot
trigger a re-run: a re-test that a model asked for would let the provider retry
on someone else's judgement rather than on the contract's terms, and MergeGate
grants exactly one evaluation per eligible submission.

**It reads the buyer's tests, which the provider may not.** Grader
confidentiality is enforced inside the sandbox precisely so a submission cannot
answer from the tests. The report generated here is written after grading has
finished, so it cannot feed a run, but it can still quote the buyer's test code
back to the provider. ``redact_grader`` therefore governs what leaves: with it
on, the report describes the failure without reproducing the grader. It defaults
to on, because the safe direction is to say less.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .gemini import GeminiResult, available, clip, generate_json
from .verifier.manifest import Verdict, VerificationManifest

__all__ = ["FailureForensics", "explain_failure", "FORENSICS_SCHEMA"]

FORENSICS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "failed_tests": {"type": "array", "items": {"type": "string"}},
        "root_cause": {"type": "string"},
        "suggestion": {"type": "string"},
        "retry_likelihood": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
        "test_quality_note": {"type": "string"},
    },
    "required": ["failed_tests", "root_cause", "suggestion", "retry_likelihood"],
}

_PROMPT = """\
You are explaining an automated verification failure to the AI agent whose \
submission failed. It will use your explanation to decide whether to retry.

The verdict is already final and a refund has already been issued. Nothing you \
write changes it. Do not recommend re-running the tests.

Failure reason recorded by the verifier:
{reason}

Failed contract terms: {failed_terms}

Command results:
{commands}

Test output:
<output>
{output}
</output>
{diff_block}
Explain what actually went wrong and what specific change would address it. \
Be concrete: name the behaviour that is missing, not the test that caught it. \
{redaction}

If the failure is a contract-term violation rather than a test failure, say so \
plainly: the submission was rejected before the tests ran, and passing tests \
would not have rescued it.
"""

_REDACTED = (
    "Do not quote or reconstruct the buyer's test code. Describe the behaviour "
    "it expects in your own words."
)
_UNREDACTED = "You may quote the buyer's tests."


@dataclass(frozen=True, slots=True)
class FailureForensics:
    """An explanation attached to a FAIL. Never an input to settlement."""

    failed_tests: tuple[str, ...]
    root_cause: str
    suggestion: str
    retry_likelihood: str
    test_quality_note: str
    model: str
    available: bool
    truncated: bool = False
    error: str = ""
    advisory: bool = True

    @classmethod
    def unavailable(cls, reason: str, model: str = "") -> FailureForensics:
        return cls(
            failed_tests=(),
            root_cause="",
            suggestion="",
            retry_likelihood="",
            test_quality_note="",
            model=model,
            available=False,
            error=reason,
        )

    @property
    def looked(self) -> bool:
        return self.available and bool(self.root_cause)


def _command_summary(manifest: VerificationManifest) -> str:
    if not manifest.commands:
        return "No commands were executed: the verdict was decided before any command ran."
    return "\n".join(
        f"- {' '.join(c.argv)} -> exit {c.exit_code} ({c.duration_ms}ms)" for c in manifest.commands
    )


def _report_from(result: GeminiResult) -> FailureForensics:
    if not result.ok:
        return FailureForensics.unavailable(result.unavailable_reason, result.model)

    data = result.data
    likelihood = str(data.get("retry_likelihood", "")).upper()
    if likelihood not in {"HIGH", "MEDIUM", "LOW"}:
        likelihood = ""

    return FailureForensics(
        failed_tests=tuple(str(t) for t in data.get("failed_tests", []) if str(t).strip()),
        root_cause=str(data.get("root_cause", "")).strip(),
        suggestion=str(data.get("suggestion", "")).strip(),
        retry_likelihood=likelihood,
        test_quality_note=str(data.get("test_quality_note", "")).strip(),
        model=result.model,
        available=True,
        truncated=result.truncated,
    )


def explain_failure(
    manifest: VerificationManifest,
    *,
    output: str = "",
    diff: str = "",
    redact_grader: bool = True,
) -> FailureForensics:
    """Explain a failed verification. Never raises, never re-runs anything.

    Refuses on a PASS manifest rather than producing something: there is no
    failure to explain, and generating prose about a successful run invites a
    reader to treat it as a caveat on a payment that was made cleanly.
    """
    if manifest.verdict is not Verdict.FAIL:
        return FailureForensics.unavailable("manifest did not fail; nothing to explain")
    if not available():
        return FailureForensics.unavailable("no GEMINI_API_KEY configured")

    clipped_output, output_truncated = clip(output)
    clipped_diff, diff_truncated = clip(diff) if diff else ("", False)

    prompt = _PROMPT.format(
        reason=manifest.rejection_reason or "not recorded",
        failed_terms=", ".join(manifest.failed_terms) or "none",
        commands=_command_summary(manifest),
        output=clipped_output or "no output captured",
        diff_block=f"\nThe provider's diff:\n<diff>\n{clipped_diff}\n</diff>\n" if diff else "",
        redaction=_REDACTED if redact_grader else _UNREDACTED,
    )
    return _report_from(
        generate_json(prompt, FORENSICS_SCHEMA, truncated=output_truncated or diff_truncated)
    )
