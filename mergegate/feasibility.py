"""Role 3: helping a provider agent decide whether to accept a contract at all.

The other Gemini surfaces act after work exists — screening a diff, explaining a
FAIL. This one runs before any work is done, when the provider's question is
still *should I take this job?*

**The epistemics are the hard part, not the prompt.** Under
:data:`~mergegate.contract.TermsVisibility.HASH_ONLY` — the default, and what
this deployment actually does — the acceptance criteria are a hash. The model
cannot see the tests. It can read the task, the repository and the paths, and
from those it can produce a genuinely useful implementation sketch. What it
cannot do is know whether the hidden tests are satisfiable, and a confident
``ACCEPT`` on a contract nobody can read is precisely the kind of claim this
project spends its effort not making.

So the model's certainty is not taken at face value. :func:`assess_contract`
applies a deterministic downgrade when the criteria are hidden: the ceiling on
confidence is lowered and an unremovable caveat is attached. The model proposes;
this module decides what may be claimed. That is the same division as everywhere
else here, applied to knowledge rather than to money.

**Advisory, and structurally so.** Nothing here accepts a contract, signs
anything, or moves funds. The provider agent decides. A ``DECLINE`` costs
nothing and an ``ACCEPT`` obligates nobody — which is the point, because the
assessment is a guess about hidden information and is allowed to be wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .contract import TaskContract, TermsVisibility
from .gemini import GeminiResult, available, clip, generate_json
from .paths import PathGuard

__all__ = [
    "FeasibilityAssessment",
    "AssessmentCheck",
    "assess_contract",
    "check_assessment",
    "FEASIBILITY_SCHEMA",
]

FEASIBILITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "implementation_plan": {"type": "array", "items": {"type": "string"}},
        "files_likely_to_change": {"type": "array", "items": {"type": "string"}},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "feasibility": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
        "attempt_risk": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
        "recommendation": {
            "type": "string",
            "enum": ["ACCEPT", "REQUEST_CLARIFICATION", "DECLINE"],
        },
    },
    "required": ["summary", "implementation_plan", "feasibility", "recommendation"],
}

#: What the model is told about what it cannot see. Stated in the prompt as well
#: as enforced afterwards: a model that knows the tests are hidden writes a more
#: useful assessment than one that is silently guessing.
_VISIBILITY_NOTE = {
    TermsVisibility.HASH_ONLY: (
        "You CANNOT see the acceptance tests. The contract commits only to their "
        "hash. You may reason about what they probably check from the task and the "
        "repository, but you must not claim to know whether they pass. Treat any "
        "statement about the hidden tests as an assumption and list it under "
        "open_questions."
    ),
    TermsVisibility.PUBLISHED_GRADER: (
        "The buyer asserts the acceptance tests are readable in the repository at "
        "the grader paths. That assertion is not verified by anyone. If the tests "
        "are not actually present where claimed, say so as a warning."
    ),
}

_PROMPT = """\
A provider agent is deciding whether to accept a software task under an \
automated escrow contract. Payment is decided by a deterministic evaluator \
running the buyer's pinned tests: no human and no model reviews the work.

{visibility_note}

Contract terms, enforced mechanically:
- repository: {repository}
- base commit: {base_sha}
- writable paths: {writable}
- protected paths: {protected}
- pinned commands: {commands}
- reward: {reward} USDC
- verifier fee per attempt: {fee} USDC, charged whatever the verdict
- acceptance criteria visibility: {visibility}

Task:
{task}
{tree_block}
Editing a protected path fails the contract outright, before any test runs, \
even if the code is otherwise correct. Only name files under the writable \
paths above.

Recommend DECLINE if the work cannot be done within the writable paths, or if \
the reward does not justify the risk. Recommend REQUEST_CLARIFICATION if the \
task is ambiguous enough that an attempt would be a guess. Recommend ACCEPT \
only if there is a plausible compliant implementation.
"""


@dataclass(frozen=True, slots=True)
class FeasibilityAssessment:
    """A pre-acceptance read on a contract. Advisory in every case."""

    summary: str = ""
    implementation_plan: tuple[str, ...] = ()
    files_likely_to_change: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    feasibility: str = ""
    attempt_risk: str = ""
    recommendation: str = ""
    estimated_attempt_cost_usdc: str = "0"
    criteria_visible: bool = False
    """Whether the assessment could actually read the acceptance criteria.

    Carried on the result rather than left implicit, because it is the single
    fact that determines how much the rest of the object is worth. An assessment
    of hidden criteria and one of published criteria are different kinds of
    claim and should not be rendered identically.
    """

    model: str = ""
    available: bool = False
    error: str = ""
    advisory: bool = True

    @classmethod
    def unavailable(cls, reason: str, model: str = "") -> FeasibilityAssessment:
        return cls(available=False, error=reason, model=model)


@dataclass(frozen=True, slots=True)
class AssessmentCheck:
    """Whether the assessment's proposed files are actually writable."""

    ok: bool
    reasons: tuple[str, ...] = ()
    disallowed_files: tuple[str, ...] = ()


def _downgrade_for_hidden_criteria(
    assessment: FeasibilityAssessment,
) -> FeasibilityAssessment:
    """Cap what may be claimed when the acceptance criteria cannot be read.

    Deterministic, and applied after the model has spoken, because asking a
    model to be appropriately uncertain and trusting that it was is not a
    control. The model is free to return ``feasibility: HIGH`` on a contract
    whose tests are a hash; what it is not free to do is have that reach the
    provider agent unqualified.

    ``HIGH`` becomes ``MEDIUM``. ``ACCEPT`` survives — declining every hidden
    contract would make the whole system unusable, since ``HASH_ONLY`` is the
    normal case — but it arrives with the reason it might be wrong attached.
    """
    if assessment.criteria_visible:
        return assessment

    caveat = (
        "The acceptance tests are committed by hash and were not readable. "
        "Feasibility here is inferred from the task and the repository, not from "
        "the criteria that will actually decide payment."
    )
    return replace(
        assessment,
        feasibility="MEDIUM" if assessment.feasibility == "HIGH" else assessment.feasibility,
        warnings=(caveat, *assessment.warnings),
    )


def _report(
    result: GeminiResult, *, fee_usdc: str, criteria_visible: bool
) -> FeasibilityAssessment:
    if not result.ok:
        return FeasibilityAssessment.unavailable(result.unavailable_reason, result.model)

    data = result.data

    def strings(key: str) -> tuple[str, ...]:
        return tuple(str(v).strip() for v in data.get(key, []) if str(v).strip())

    def enum(key: str, allowed: set[str]) -> str:
        value = str(data.get(key, "")).upper()
        return value if value in allowed else ""

    assessment = FeasibilityAssessment(
        summary=str(data.get("summary", "")).strip(),
        implementation_plan=strings("implementation_plan"),
        files_likely_to_change=strings("files_likely_to_change"),
        warnings=strings("warnings"),
        open_questions=strings("open_questions"),
        feasibility=enum("feasibility", {"HIGH", "MEDIUM", "LOW"}),
        attempt_risk=enum("attempt_risk", {"HIGH", "MEDIUM", "LOW"}),
        recommendation=enum("recommendation", {"ACCEPT", "REQUEST_CLARIFICATION", "DECLINE"}),
        estimated_attempt_cost_usdc=fee_usdc,
        criteria_visible=criteria_visible,
        model=result.model,
        available=True,
    )
    return _downgrade_for_hidden_criteria(assessment)


def assess_contract(
    contract: TaskContract,
    *,
    task: str = "",
    repo_tree: str = "",
    fee_usdc: str = "0.05",
) -> FeasibilityAssessment:
    """Assess a contract before the provider agent commits to it.

    ``task`` is the human-readable description; ``repo_tree`` is whatever
    context the provider chose to share, already clipped. Neither is required —
    an assessment from the terms alone is thinner but still says whether the
    writable paths could plausibly contain the work.
    """
    if not available():
        return FeasibilityAssessment.unavailable("no GEMINI_API_KEY configured")

    visibility = contract.terms_visibility
    clipped_tree, _ = clip(repo_tree) if repo_tree else ("", False)

    prompt = _PROMPT.format(
        visibility_note=_VISIBILITY_NOTE.get(visibility, ""),
        repository=contract.repository,
        base_sha=contract.base_sha,
        writable=", ".join(contract.allowed_source_paths) or "none",
        protected=", ".join(contract.protected_paths) or "none",
        commands="; ".join(" ".join(c) for c in contract.required_commands) or "none",
        reward=contract.reward_usdc,
        fee=fee_usdc,
        visibility=str(visibility),
        task=task or "(no description supplied beyond the pinned terms)",
        tree_block=f"\nRepository context:\n<tree>\n{clipped_tree}\n</tree>\n" if repo_tree else "",
    )
    return _report(
        generate_json(prompt, FEASIBILITY_SCHEMA),
        fee_usdc=fee_usdc,
        criteria_visible=visibility is TermsVisibility.PUBLISHED_GRADER,
    )


def check_assessment(assessment: FeasibilityAssessment, contract: TaskContract) -> AssessmentCheck:
    """Check the proposed files against the contract's own path guard.

    The same :class:`~mergegate.paths.PathGuard` the evaluator uses, for the
    same reason it is reused in :mod:`mergegate.retry`: a second implementation
    could disagree with the one that decides verdicts, and a checker that
    approves what the evaluator will reject is worse than no checker.

    Catching it *here* is worth more than catching it after a failed retry.
    This runs before the provider has done any work at all, so a plan that
    would have violated the contract costs nothing rather than costing an
    attempt and a verifier fee.
    """
    if not assessment.available:
        return AssessmentCheck(False, (f"no assessment was produced: {assessment.error}",))

    guard = PathGuard.from_contract(contract)
    disallowed = tuple(
        f"{path} ({violation.kind.value})"
        for path in assessment.files_likely_to_change
        if (violation := guard.classify(path)) is not None
    )
    if disallowed:
        return AssessmentCheck(
            False,
            (
                "the plan expects to change files the contract does not permit: "
                + ", ".join(disallowed),
            ),
            disallowed,
        )
    return AssessmentCheck(True)
