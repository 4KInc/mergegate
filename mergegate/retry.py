"""Role 4: turning a FAIL into a bounded, policy-checked retry.

A refund closes one attempt, not the task. The provider agent's next question is
economic: *is another attempt worth it, and what exactly would I change?* A raw
manifest cannot answer that, and neither can a paragraph of prose. This produces
a structured plan and then checks it against the contract before the provider
acts on it.

**The plan is a proposal; the checker is the gate.** Gemini names files it would
change. :func:`check_plan` decides whether those files may be touched, using the
same ``PathGuard`` that decides verdicts, so the answer cannot drift from what
the evaluator will actually enforce. A plan that proposes editing a protected
path is refused here rather than discovered after another failed submission.

**Retries are budgeted, and the budget is not advisory.** The whole point of a
closed loop is that it terminates. :class:`RetryBudget` bounds attempts and
respects the contract deadline, so a provider agent cannot spin against a task
it will never satisfy, and a buyer's escrow is not consumed by an unbounded
sequence of verifier fees.

Nothing here changes a verdict or moves money.

**A retry is a new contract, not a second go at the old one**, and the wording
here used to say otherwise. Once a task settles, the state machine treats it as
terminal and refuses every later event including a fresh submission, because the
buyer's mandate authorized exactly one payment decision. So the loop is: FAIL,
refund, then a *new* contract funded on the same terms, carrying a
``retry_of`` link to its predecessor. Nothing about the failed attempt is
reopened; the link is provenance, not a reference the evaluator consults.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .contract import TaskContract
from .gemini import GeminiResult, available, clip, generate_json
from .paths import PathGuard
from .verifier.manifest import Verdict, VerificationManifest

__all__ = [
    "RetryPlan",
    "PlanCheck",
    "RetryBudget",
    "plan_retry",
    "check_plan",
    "files_to_revert",
    "RETRY_SCHEMA",
]

RETRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "root_cause": {"type": "string"},
        "violated_terms": {"type": "array", "items": {"type": "string"}},
        "safe_files_to_modify": {"type": "array", "items": {"type": "string"}},
        "prohibited_changes": {"type": "array", "items": {"type": "string"}},
        "proposed_fix": {"type": "string"},
        "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
        "recommendation": {"type": "string", "enum": ["RETRY", "CLARIFY", "ABANDON"]},
    },
    "required": ["root_cause", "safe_files_to_modify", "proposed_fix", "recommendation"],
}

_PROMPT = """\
A submission to an automated escrow contract failed. You are advising the \
provider agent on whether and how to retry. The refund has already happened; \
nothing you write changes it.

Contract terms, enforced mechanically:
- writable paths: {writable}
- protected paths: {protected}
- pinned commands: {commands}
- reward: {reward} USDC

Why it failed:
{reason}

Failed contract terms: {failed_terms}

Command results:
{commands_run}
{diff_block}
Name the specific files a compliant retry would change, and say what would be \
wrong to change. Only list files under the writable paths above: a plan that \
proposes touching a protected path is rejected before the provider can act on \
it, so proposing one wastes the attempt.

Recommend RETRY only if a compliant fix is genuinely available. Recommend \
ABANDON if the contract cannot be satisfied by editing the writable paths, and \
CLARIFY if the terms are ambiguous enough that another attempt is a guess.
"""


@dataclass(frozen=True, slots=True)
class RetryPlan:
    """A proposed next attempt. Advisory until :func:`check_plan` clears it."""

    root_cause: str = ""
    violated_terms: tuple[str, ...] = ()
    safe_files_to_modify: tuple[str, ...] = ()
    prohibited_changes: tuple[str, ...] = ()
    proposed_fix: str = ""
    confidence: str = ""
    recommendation: str = ""
    estimated_retry_cost_usdc: str = "0"
    model: str = ""
    available: bool = False
    error: str = ""
    advisory: bool = True

    @classmethod
    def unavailable(cls, reason: str, model: str = "") -> RetryPlan:
        return cls(available=False, error=reason, model=model)


@dataclass(frozen=True, slots=True)
class PlanCheck:
    """Whether a provider agent may act on a plan."""

    ok: bool
    reasons: tuple[str, ...] = ()
    disallowed_files: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetryBudget:
    """Bounds on how many times a provider may come back.

    A closed loop that does not terminate is just a loop. Each attempt costs the
    buyer a verifier fee whatever the verdict, so an unbounded retry sequence
    drains escrow on evaluations rather than work.
    """

    max_attempts: int = 3
    attempts_used: int = 0
    fee_per_attempt_usdc: str = "0.05"

    def exhausted(self) -> bool:
        return self.attempts_used >= self.max_attempts

    def remaining(self) -> int:
        return max(0, self.max_attempts - self.attempts_used)

    def spent_usdc(self) -> str:
        return str(Decimal(self.fee_per_attempt_usdc) * self.attempts_used)

    def allows(self, contract: TaskContract, *, now: datetime | None = None) -> tuple[bool, str]:
        """Whether another attempt is permitted at all.

        The deadline is checked here as well as at settlement. A provider that
        spends compute on an attempt which cannot settle has been failed by its
        own tooling, even though the settlement layer would refuse it correctly.
        """
        moment = now or datetime.now(UTC)
        if self.exhausted():
            return False, f"retry budget exhausted: {self.attempts_used}/{self.max_attempts}"
        if moment >= contract.deadline:
            return False, "the contract deadline has passed; no further attempt can settle"
        return True, ""


def _command_summary(manifest: VerificationManifest) -> str:
    if not manifest.commands:
        return "No commands ran: the verdict was decided by a contract term before execution."
    return "\n".join(f"- {' '.join(c.argv)} -> exit {c.exit_code}" for c in manifest.commands)


def _report(result: GeminiResult, fee: str) -> RetryPlan:
    if not result.ok:
        return RetryPlan.unavailable(result.unavailable_reason, result.model)

    data = result.data

    def strings(key: str) -> tuple[str, ...]:
        return tuple(str(v).strip() for v in data.get(key, []) if str(v).strip())

    confidence = str(data.get("confidence", "")).upper()
    if confidence not in {"HIGH", "MEDIUM", "LOW"}:
        confidence = ""
    recommendation = str(data.get("recommendation", "")).upper()
    if recommendation not in {"RETRY", "CLARIFY", "ABANDON"}:
        recommendation = ""

    return RetryPlan(
        root_cause=str(data.get("root_cause", "")).strip(),
        violated_terms=strings("violated_terms"),
        safe_files_to_modify=strings("safe_files_to_modify"),
        prohibited_changes=strings("prohibited_changes"),
        proposed_fix=str(data.get("proposed_fix", "")).strip(),
        confidence=confidence,
        recommendation=recommendation,
        estimated_retry_cost_usdc=fee,
        model=result.model,
        available=True,
    )


def plan_retry(
    manifest: VerificationManifest,
    contract: TaskContract,
    *,
    diff: str = "",
    fee_usdc: str = "0.05",
) -> RetryPlan:
    """Propose a next attempt for a failed submission.

    Refuses on a PASS: there is nothing to retry, and a plan attached to a
    successful settlement invites a reader to treat a completed payment as
    provisional.
    """
    if manifest.verdict is not Verdict.FAIL:
        return RetryPlan.unavailable("manifest did not fail; there is nothing to retry")
    if not available():
        return RetryPlan.unavailable("no GEMINI_API_KEY configured")

    clipped_diff, _ = clip(diff) if diff else ("", False)
    prompt = _PROMPT.format(
        writable=", ".join(contract.allowed_source_paths) or "none",
        protected=", ".join(contract.protected_paths) or "none",
        commands="; ".join(" ".join(c) for c in contract.required_commands) or "none",
        reward=contract.reward_usdc,
        reason=manifest.rejection_reason or "not recorded",
        failed_terms=", ".join(manifest.failed_terms) or "none",
        commands_run=_command_summary(manifest),
        diff_block=f"\nThe failed diff:\n<diff>\n{clipped_diff}\n</diff>\n" if diff else "",
    )
    return _report(generate_json(prompt, RETRY_SCHEMA), fee_usdc)


def check_plan(plan: RetryPlan, contract: TaskContract) -> PlanCheck:
    """Decide whether a provider agent may act on a plan.

    Uses the contract's own :class:`~mergegate.paths.PathGuard`, the same one
    that decides verdicts. A separate reimplementation here could disagree with
    the evaluator, and a checker that says yes where the evaluator says no is
    worse than no checker: it would spend an attempt to learn what it was
    supposed to prevent.
    """
    reasons: list[str] = []
    if not plan.available:
        return PlanCheck(False, (f"no plan was produced: {plan.error}",))
    if plan.recommendation == "ABANDON":
        return PlanCheck(False, ("the plan recommends abandoning this contract",))
    if not plan.safe_files_to_modify:
        return PlanCheck(False, ("the plan names no files to change",))

    guard = PathGuard.from_contract(contract)
    disallowed = tuple(
        f"{path} ({violation.kind.value})"
        for path in plan.safe_files_to_modify
        if (violation := guard.classify(path)) is not None
    )
    if disallowed:
        reasons.append(
            "the plan proposes changing files the contract does not permit: "
            + ", ".join(disallowed)
        )

    return PlanCheck(not reasons, tuple(reasons), disallowed)


def files_to_revert(changed_files: tuple[str, ...], contract: TaskContract) -> tuple[str, ...]:
    """Which of a failed submission's files must be undone to comply.

    The remediation the closed loop actually applies, and it is deliberately
    **not** a model output. Gemini explains *why* a submission failed; what a
    provider agent then does about it must be reproducible, because it decides
    what gets resubmitted and therefore what gets paid for.

    The rule is the narrow one that generalises: revert everything the
    contract's own guard rejects, keep everything it permits. That undoes the
    term violation without touching the work, which is exactly the FAIL case
    this system is built around — a correct fix bundled with a prohibited edit.

    It does not write code, and it cannot repair a submission that failed
    because the tests genuinely did not pass. A caller reaching for it in that
    case gets an empty tuple, which correctly means "nothing here is fixable by
    reverting".
    """
    guard = PathGuard.from_contract(contract)
    return tuple(sorted(path for path in changed_files if guard.classify(path) is not None))
