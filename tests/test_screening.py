"""Screening and forensics: what they produce, given what a model returns.

The boundary tests prove these cannot affect settlement. These check the part
that is allowed to matter, which is whether the report shown to a buyer is a
faithful rendering of the answer rather than a hopeful one.

The recurring theme is refusing to round up. A missing band is derived from the
score rather than defaulted to LOW; a missing key reports that nothing was
looked at rather than reporting nothing found.
"""

from __future__ import annotations

from typing import Any

import pytest

from mergegate import forensics, screening
from mergegate.contract import TaskContract
from mergegate.gemini import GeminiResult
from mergegate.verifier.manifest import CommandResult, VerificationManifest

CONTRACT_HASH = "sha256:" + "c" * 64


def _stub(monkeypatch: pytest.MonkeyPatch, module: Any, payload: dict[str, Any]) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(module, "generate_json", lambda *a, **k: GeminiResult(True, data=payload))


def test_a_clean_diff_reports_low(contract: TaskContract, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(
        monkeypatch,
        screening,
        {
            "code_risk_score": 15,
            "code_risk_band": "LOW",
            "flags": [],
            "assessment": "Clean diff, no new dependencies.",
            "recommendation": "PROCEED",
        },
    )
    report = screening.screen_diff("diff --git a/src/x.py b/src/x.py", contract)

    assert report.looked is True
    assert (report.score, report.band, report.recommendation) == (15, "LOW", "PROCEED")
    assert report.flags == ()


def test_flags_survive_intact(contract: TaskContract, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(
        monkeypatch,
        screening,
        {
            "code_risk_score": 78,
            "code_risk_band": "HIGH",
            "flags": ["new_dependency:cryptominer-utils@0.1.0", "obfuscated_code:base64_exec"],
            "assessment": "Consistent with a supply chain attack.",
            "recommendation": "FLAG",
        },
    )
    report = screening.screen_diff("diff", contract)

    assert report.band == "HIGH"
    assert len(report.flags) == 2
    assert "cryptominer" in report.flags[0]


@pytest.mark.parametrize(
    "score,expected",
    [(5, "LOW"), (34, "LOW"), (35, "MEDIUM"), (69, "MEDIUM"), (70, "HIGH"), (99, "HIGH")],
)
def test_a_missing_band_is_derived_from_the_score(
    score: int, expected: str, contract: TaskContract, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A band that disagrees with its own score would misrepresent the
    assessment on the page, so it is computed rather than trusted."""
    _stub(monkeypatch, screening, {"code_risk_score": score, "flags": [], "assessment": "x"})
    assert screening.screen_diff("diff", contract).band == expected


def test_a_nonsense_score_does_not_become_zero(
    contract: TaskContract, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Coercing an unparseable score to 0 would display the safest possible
    number for the least trustworthy answer."""
    _stub(monkeypatch, screening, {"code_risk_score": "BLOCK", "flags": ["something"]})
    report = screening.screen_diff("diff", contract)

    assert report.score == -1
    assert report.looked is False


def test_out_of_range_scores_are_clamped(
    contract: TaskContract, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(monkeypatch, screening, {"code_risk_score": 5000, "flags": []})
    assert screening.screen_diff("diff", contract).score == 100


def test_flags_force_a_flag_recommendation(
    contract: TaskContract, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model that raises findings and then recommends nothing be done about
    them is contradicting itself; the findings win."""
    _stub(
        monkeypatch,
        screening,
        {"code_risk_score": 50, "flags": ["protected_path:.github/ci.yml"], "recommendation": ""},
    )
    assert screening.screen_diff("diff", contract).recommendation == "FLAG"


def test_the_contract_terms_reach_the_prompt(
    contract: TaskContract, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the protected paths in context the model cannot tell an allowed
    edit from a violation, and the report becomes generic."""
    captured: dict[str, str] = {}
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def capture(prompt: str, schema: Any, **kwargs: Any) -> GeminiResult:
        captured["prompt"] = prompt
        return GeminiResult(True, data={"code_risk_score": 1, "flags": []})

    monkeypatch.setattr(screening, "generate_json", capture)
    screening.screen_diff("the diff body", contract)

    assert contract.repository in captured["prompt"]
    assert ".github/**" in captured["prompt"]
    assert "the diff body" in captured["prompt"]


# -- forensics -----------------------------------------------------------------


def _failed_manifest(*, commands: bool = True) -> VerificationManifest:
    return VerificationManifest(
        task_id="task-001",
        contract_hash=CONTRACT_HASH,
        grader_hash="sha256:" + "d" * 64,
        base_sha="a" * 40,
        submission_sha="1" * 40,
        tree_hash="sha256:" + "e" * 64,
        verifier_image_digest="us-docker.pkg.dev/mergegate/verifier@sha256:" + "b" * 64,
        commands=(
            (
                CommandResult(
                    argv=("pytest", "-q"),
                    exit_code=1,
                    stdout_digest="sha256:" + "a" * 64,
                    stderr_digest="sha256:" + "b" * 64,
                    duration_ms=900,
                ),
            )
            if commands
            else ()
        ),
        failed_terms=("protected_path",),
        rejection_reason=".github/workflows/deploy.yml modifies a protected path",
    )


def test_forensics_returns_an_actionable_explanation(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(
        monkeypatch,
        forensics,
        {
            "failed_tests": ["test_refund_exceeds_balance"],
            "root_cause": "Overflow case is unhandled.",
            "suggestion": "Add a balance check before the subtraction.",
            "retry_likelihood": "HIGH",
            "test_quality_note": "Well-constructed test.",
        },
    )
    report = forensics.explain_failure(_failed_manifest(), output="AssertionError")

    assert report.looked is True
    assert report.failed_tests == ("test_refund_exceeds_balance",)
    assert report.retry_likelihood == "HIGH"


def test_a_run_with_no_commands_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """The protected-path case: the verdict was decided before anything ran,
    and an explanation implying tests failed would describe a different run."""
    captured: dict[str, str] = {}
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def capture(prompt: str, schema: Any, **kwargs: Any) -> GeminiResult:
        captured["prompt"] = prompt
        return GeminiResult(True, data={"failed_tests": [], "root_cause": "r", "suggestion": "s"})

    monkeypatch.setattr(forensics, "generate_json", capture)
    forensics.explain_failure(_failed_manifest(commands=False))

    assert "No commands were executed" in captured["prompt"]
    assert "protected_path" in captured["prompt"]


def test_an_invalid_retry_likelihood_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Better to show nothing than to show a confidence the model did not
    express in the vocabulary that was asked for."""
    _stub(
        monkeypatch,
        forensics,
        {"failed_tests": [], "root_cause": "r", "suggestion": "s", "retry_likelihood": "CERTAIN"},
    )
    assert forensics.explain_failure(_failed_manifest()).retry_likelihood == ""


def test_redaction_changes_what_the_model_is_told(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def capture(prompt: str, schema: Any, **kwargs: Any) -> GeminiResult:
        captured.append(prompt)
        return GeminiResult(True, data={"failed_tests": [], "root_cause": "r", "suggestion": "s"})

    monkeypatch.setattr(forensics, "generate_json", capture)
    forensics.explain_failure(_failed_manifest(), redact_grader=True)
    forensics.explain_failure(_failed_manifest(), redact_grader=False)

    assert "Do not quote" in captured[0]
    assert "may quote" in captured[1]


# -- the page ------------------------------------------------------------------


def test_evaluation_page_renders_the_advisory_panel(tmp_path: Any) -> None:
    """The panel has to state its own irrelevance to settlement, because that
    is the only thing that makes an LLM acceptable this close to a page about
    money that moved."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mergegate.store import MemoryAdvisoryStore
    from mergegate.web import ReceiptBundle, build_web_router

    store = MemoryAdvisoryStore()
    store.put(
        "r1",
        {
            "model": "gemini-2.5-flash",
            "screening": {
                "available": True,
                "score": 78,
                "band": "HIGH",
                "flags": ["obfuscated_code:base64_exec"],
                "assessment": "Consistent with a supply chain attack.",
            },
            "forensics": {
                "available": True,
                "root_cause": "Overflow unhandled.",
                "suggestion": "Add a balance check.",
                "retry_likelihood": "HIGH",
            },
        },
    )

    class OneReceipt:
        def all(self) -> list[Any]:
            return []

        def get(self, receipt_id: str) -> dict[str, Any] | None:
            return _ENVELOPE if receipt_id == "r1" else None

    app = FastAPI()
    app.include_router(build_web_router(ReceiptBundle(source=OneReceipt()), advisory=store))
    html = TestClient(app).get("/evaluations/r1").text

    assert "78" in html and "HIGH" in html
    assert "obfuscated_code:base64_exec" in html
    assert "Add a balance check." in html
    assert "no effect on settlement" in html
    assert "neither is signed" in html


_ENVELOPE: dict[str, Any] = {
    "body": {
        "binding": {
            "task_id": "task-001",
            "decision": "FAIL",
            "settlement_action": "refund",
            "reason": "protected path",
            "contract_hash": CONTRACT_HASH,
            "submission_sha": "1" * 40,
        },
        "manifest": {"failed_terms": ["protected_path"], "commands": []},
        "mandate": {"chain": "BASE"},
    },
    "sig": {"kid": "k"},
}


def test_advisory_absent_is_not_rendered_as_clean() -> None:
    """A missing report and a clean report must not look the same."""
    from mergegate.web import _advisory_for

    assert _advisory_for(None, "any") == {}


def test_advisory_store_failure_does_not_break_the_page() -> None:
    """Advisory data is never worth an error page on a settlement that already
    happened."""
    from mergegate.web import _advisory_for

    class Broken:
        def get(self, receipt_id: str) -> dict[str, object]:
            raise RuntimeError("firestore unavailable")

    assert _advisory_for(Broken(), "any") == {}


def test_advisory_round_trips_through_the_store() -> None:
    from mergegate.store import MemoryAdvisoryStore

    store = MemoryAdvisoryStore()
    store.put("receipt-1", {"screening": {"available": True, "score": 12}})

    stored = store.get("receipt-1")
    assert stored is not None
    assert stored["screening"]["score"] == 12
    assert store.get("missing") is None
