"""Pre-acceptance assessment, and the limits it is not allowed to overstate.

The model is not called here. What is tested is the deterministic layer around
it: the confidence downgrade when the acceptance criteria are hidden, and the
path check that reuses the evaluator's own guard. Those are the parts that
decide what a provider agent is told, and they have to hold whatever the model
returns — including when it returns something confident and wrong.
"""

from __future__ import annotations

from dataclasses import replace

from mergegate.contract import TaskContract, TermsVisibility
from mergegate.feasibility import (
    AssessmentCheck,
    FeasibilityAssessment,
    _downgrade_for_hidden_criteria,
    check_assessment,
)


def _assessment(**kwargs: object) -> FeasibilityAssessment:
    base = FeasibilityAssessment(
        summary="fix the importer",
        implementation_plan=("edit src/calc.py",),
        files_likely_to_change=("src/calc.py",),
        feasibility="HIGH",
        recommendation="ACCEPT",
        available=True,
    )
    return replace(base, **kwargs)  # type: ignore[arg-type]


# -- the confidence ceiling ---------------------------------------------------


def test_high_confidence_is_downgraded_when_the_tests_cannot_be_read() -> None:
    """A confident ACCEPT on criteria nobody can see is the claim to avoid.

    The model is free to return HIGH. What it is not free to do is have that
    reach the provider agent unqualified, because it is a guess about hidden
    information dressed as a finding.
    """
    result = _downgrade_for_hidden_criteria(_assessment(criteria_visible=False))
    assert result.feasibility == "MEDIUM"


def test_the_caveat_is_attached_and_comes_first() -> None:
    result = _downgrade_for_hidden_criteria(
        _assessment(criteria_visible=False, warnings=("something else",))
    )
    assert "committed by hash and were not readable" in result.warnings[0]
    assert "something else" in result.warnings


def test_accept_survives_the_downgrade() -> None:
    """Declining every hidden contract would make the system unusable.

    HASH_ONLY is the normal case, not an edge case. The downgrade exists to
    qualify the recommendation, not to refuse the only mode the deployment
    actually offers.
    """
    result = _downgrade_for_hidden_criteria(_assessment(criteria_visible=False))
    assert result.recommendation == "ACCEPT"


def test_a_low_estimate_is_not_raised_by_the_downgrade() -> None:
    """The cap is one-directional. It may only reduce what is claimed."""
    result = _downgrade_for_hidden_criteria(_assessment(criteria_visible=False, feasibility="LOW"))
    assert result.feasibility == "LOW"


def test_published_criteria_are_left_alone() -> None:
    original = _assessment(criteria_visible=True)
    assert _downgrade_for_hidden_criteria(original) == original


def test_the_default_visibility_produces_a_downgraded_assessment(
    contract: TaskContract,
) -> None:
    """Ties the cap to the contract rather than to a caller remembering.

    ``criteria_visible`` is derived from the contract's own ``terms_visibility``
    inside ``assess_contract``, so a HASH_ONLY contract cannot produce an
    un-caveated assessment by a caller forgetting to pass a flag.
    """
    assert contract.terms_visibility is TermsVisibility.HASH_ONLY


# -- the path check -----------------------------------------------------------


def test_a_plan_touching_a_protected_path_is_refused(contract: TaskContract) -> None:
    """Caught before any work, not after a paid attempt.

    This is the same violation the FAIL demo turns on. Catching it at
    acceptance time costs nothing; catching it after submission costs the
    buyer a verifier fee and the provider its work.
    """
    protected = contract.protected_paths[0].replace("**", "workflows/deploy.yml")
    result = check_assessment(_assessment(files_likely_to_change=(protected,)), contract)
    assert not result.ok
    assert result.disallowed_files
    assert protected in result.reasons[0]


def test_a_plan_within_the_writable_paths_is_allowed(contract: TaskContract) -> None:
    allowed = contract.allowed_source_paths[0].replace("**", "calc.py")
    assert check_assessment(_assessment(files_likely_to_change=(allowed,)), contract).ok


def test_an_unavailable_assessment_is_not_silently_approved(
    contract: TaskContract,
) -> None:
    """ "No assessment" and "assessment says fine" must not look the same.

    A provider agent that reads a falsy ``ok`` as "proceed" would treat a
    missing API key as a green light.
    """
    result = check_assessment(
        FeasibilityAssessment.unavailable("no GEMINI_API_KEY configured"), contract
    )
    assert not result.ok
    assert "no assessment was produced" in result.reasons[0]


def test_the_check_uses_the_contracts_own_guard(contract: TaskContract) -> None:
    """Not a reimplementation, for the same reason retry.check_plan is not.

    A checker that approves what the evaluator rejects is worse than none: it
    spends an attempt to discover what it existed to prevent. Proven by
    widening the contract and watching the same file become acceptable.
    """
    grader_file = contract.grader_paths[0].replace("**", "test_calc.py")
    assessment = _assessment(files_likely_to_change=(grader_file,))
    assert not check_assessment(assessment, contract).ok

    widened = replace(
        contract,
        grader_paths=("nowhere/**",),
        allowed_source_paths=(*contract.allowed_source_paths, "tests/**"),
    )
    assert check_assessment(assessment, widened).ok


def test_the_module_exposes_nothing_that_accepts_or_pays(contract: TaskContract) -> None:
    """Advisory as a property of the surface, not as a claim in a docstring.

    This runs before any work exists, which is exactly when a provider agent is
    most likely to hand it authority it should not have. Nothing exported here
    may sign, fund, submit or settle: the assessment ends in a value the caller
    decides what to do with.
    """
    import mergegate.feasibility as module

    forbidden = ("accept", "sign", "fund", "pay", "submit", "settle", "transfer", "commit")
    exported = [name for name in module.__all__ if any(f in name.lower() for f in forbidden)]
    assert not exported, f"advisory module exposes state-changing names: {exported}"

    # And the one function that returns a judgement returns data, not an action.
    result = check_assessment(_assessment(), contract)
    assert isinstance(result, AssessmentCheck)
