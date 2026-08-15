"""Shared Gemini client. Advisory only, by construction.

MergeGate settles on an exit code. Gemini is wired in around that decision and
never into it: nothing in this module or its callers can change a verdict,
release escrow, or alter a receipt. That is not a convention to be respected by
future code, it is the property the tests in
``tests/test_gemini_boundary.py`` exist to enforce.

**Three rules this module is built to keep.**

*It fails open, always.* No key, a timeout, a malformed answer, a quota error:
every one of them returns a result marked unavailable and the caller carries on.
An advisory layer that can stall or break a settlement is worse than no advisory
layer, because it converts a nice-to-have into an outage in the path that moves
money.

*Its input is attacker-controlled.* The diff being screened was written by the
provider agent, which is the party with an incentive to be judged favourably. A
diff can contain text aimed at the model reading it. That is assumed rather than
defended against with clever prompting: the honest mitigation is that a
manipulated screening changes nothing, because settlement never consults it.

*Its output never enters the signed receipt.* The receipt binds thirteen fields
that are cross-checked against the manifest, and its worth comes from every one
being mechanically derived. A model's opinion cross-checks against nothing.
Advisory reports are stored and displayed alongside a receipt, never inside it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "GeminiResult",
    "DEFAULT_MODEL",
    "MAX_INPUT_CHARS",
    "available",
    "generate_json",
    "clip",
]

DEFAULT_MODEL = "gemini-2.5-flash"
API_KEY_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

# Diffs and test logs have no upper bound; a runaway one would cost real money
# and add latency to a path a provider is waiting on. Clipping is disclosed in
# the report rather than done silently, so a reader knows the model saw part.
MAX_INPUT_CHARS = 24_000

# Bounded so a hung API call cannot hold up an evaluation. The advisory result
# is worth less than the run finishing on time.
DEFAULT_TIMEOUT_MS = 20_000


@dataclass(frozen=True, slots=True)
class GeminiResult:
    """What came back, or why nothing did.

    ``ok`` false is a normal outcome rather than an error to raise on. Callers
    build an "unavailable" report from it and continue.
    """

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    model: str = DEFAULT_MODEL
    truncated: bool = False

    @property
    def unavailable_reason(self) -> str:
        return "" if self.ok else (self.error or "unknown")


def available() -> bool:
    """Whether this deployment has a key configured.

    Pages call this to say "screening is not enabled here" rather than showing
    an empty panel that reads like a clean result.
    """
    return any(os.environ.get(name) for name in API_KEY_VARS)


def _api_key() -> str:
    for name in API_KEY_VARS:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def clip(text: str, limit: int = MAX_INPUT_CHARS) -> tuple[str, bool]:
    """Bound model input. Returns the text and whether it was shortened."""
    if len(text) <= limit:
        return text, False
    head = limit * 2 // 3
    tail = limit - head
    return text[:head] + "\n\n[... clipped ...]\n\n" + text[-tail:], True


def generate_json(
    prompt: str,
    schema: dict[str, Any],
    *,
    model: str = DEFAULT_MODEL,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    truncated: bool = False,
) -> GeminiResult:
    """One structured call. Never raises.

    Every failure path returns ``ok=False`` instead of propagating, because
    every caller sits next to something that moves money and none of them
    should have to reason about which exceptions the SDK raises this week.
    """
    key = _api_key()
    if not key:
        return GeminiResult(False, error="no GEMINI_API_KEY configured", model=model)

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        # The SDK is an optional extra. A deployment without it runs the
        # deterministic core exactly as before.
        return GeminiResult(
            False, error="google-genai not installed (pip install 'mergegate[gemini]')", model=model
        )

    try:
        client = genai.Client(api_key=key)
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
                http_options=types.HttpOptions(timeout=timeout_ms),
            ),
        )
        text = (response.text or "").strip()
    except Exception as exc:  # noqa: BLE001 - any SDK failure is the same answer here
        return GeminiResult(False, error=f"{type(exc).__name__}: {exc}", model=model)

    # Structured output should not be fenced, but a model that wraps it anyway
    # would otherwise cost a whole report to a formatting quirk.
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return GeminiResult(False, error=f"model did not return JSON: {exc}", model=model)

    if not isinstance(data, dict):
        return GeminiResult(False, error="model returned JSON that was not an object", model=model)

    return GeminiResult(True, data=data, model=model, truncated=truncated)
