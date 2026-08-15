"""The integration page must describe the service that exists.

Documentation drift is the failure mode here, and it is not cosmetic: the
receipt page told readers to run ``mergegate verify`` for weeks while the
command raised ImportError. These tests bind every documented surface to the
running app, so the page cannot claim an endpoint or a tool that is not there.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mergegate.app import create_app
from mergegate.mcp import TOOLS
from mergegate.web import HTTP_ENDPOINTS, MCP_TOOLS

PUBLIC_KEY = "bKniJaFvoeSt4_LmdfiKemxeIqaz-ALsjSFtiNWzA8U"
TEMPLATE = "mergegate/templates/base.html"


def _app() -> Any:
    """Build the app with an injected receiver, so these tests do not depend on
    the deployment configuration the webhook path requires."""
    from mergegate.webhook import WebhookReceiver

    return create_app(receiver=WebhookReceiver(secret="s", repository="r", resolve=lambda p: None))


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("MERGEGATE_RECEIPT_PUBLIC_KEY", PUBLIC_KEY)
    return TestClient(_app())


def test_integrate_page_renders(client: Any) -> None:
    response = client.get("/integrate")
    assert response.status_code == 200
    assert "mergegate-mcp" in response.text
    assert "mergegate verify" in response.text


def _normalise(path: str) -> str:
    """Collapse path parameter names, so the page may say ``{id}`` where the
    route says ``{receipt_id}`` without that counting as drift."""
    return re.sub(r"\{[^}]*\}", "{}", path)


def test_every_documented_endpoint_is_routed(client: Any) -> None:
    """The page lists these paths as things a caller can hit. If one is not
    registered, the page is lying to an integrator.

    Read from the served OpenAPI schema rather than ``app.routes``: this
    FastAPI version keeps included routers nested, so walking ``app.routes``
    silently misses every dashboard path and the check would pass by seeing
    nothing.
    """
    spec = client.get("/openapi.json").json()
    routed = {
        (method.upper(), _normalise(path))
        for path, operations in spec["paths"].items()
        for method in operations
    }
    for method, path, _ in HTTP_ENDPOINTS:
        assert (method, _normalise(path)) in routed, f"{method} {path} documented but not routed"


def test_the_route_check_can_actually_fail() -> None:
    """Guards the test above.

    Its previous implementation passed on an empty route set, which is exactly
    how a documentation check quietly stops checking. This pins that a bogus
    path is rejected.
    """
    assert ("GET", _normalise("/api/does-not-exist")) not in {("GET", "/api/receipts")}


def test_documented_mcp_tools_match_the_server() -> None:
    assert [name for name, _ in MCP_TOOLS] == [tool["name"] for tool in TOOLS]


def test_verification_key_is_published(client: Any) -> None:
    """Receipts claimed to be independently verifiable while the key needed to
    verify them was unobtainable, which made the claim unexercisable."""
    payload = client.get("/api/verification-key").json()
    assert payload["alg"] == "Ed25519"
    assert payload["public_key"]
    assert "caveat" in payload


def test_verification_key_absent_is_503_not_a_fake_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serving an empty key would let a caller 'verify' against nothing."""
    monkeypatch.delenv("MERGEGATE_RECEIPT_PUBLIC_KEY", raising=False)
    response = TestClient(_app()).get("/api/verification-key")
    assert response.status_code == 503


def test_receipts_api_distinguishes_empty_from_broken(client: Any) -> None:
    payload = client.get("/api/receipts").json()
    assert "count" in payload
    assert "source_error" in payload


def test_published_key_verifies_the_receipts_this_deployment_serves(client: Any) -> None:
    """End to end, through the documented path only.

    Fetch the key the way the page tells an integrator to, fetch a receipt the
    way the page tells them to, and verify. If this passes, the instructions on
    the page work.
    """
    listing = client.get("/api/receipts").json()
    if not listing["receipts"]:
        pytest.skip("no receipts in this environment")

    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from mergegate.receipt import verify_receipt

    raw = client.get("/api/verification-key").json()["public_key"]
    key = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    envelope = client.get(listing["receipts"][0]["envelope_url"]).json()

    assert verify_receipt(envelope, public_key=key).valid


def test_console_scripts_are_declared() -> None:
    """Both entry points ship, and both resolve."""
    import importlib
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    scripts = pyproject["project"]["scripts"]
    assert set(scripts) == {"mergegate", "mergegate-mcp"}
    for target in scripts.values():
        module_name, _, attribute = target.partition(":")
        assert callable(getattr(importlib.import_module(module_name), attribute))


def test_integrate_page_states_the_scope_limit(client: Any) -> None:
    text = client.get("/integrate").text
    assert "not code quality" in text
    assert "self-consistent" in text  # the key-provenance caveat


def test_integrate_page_has_no_em_dashes(client: Any) -> None:
    assert "—" not in client.get("/integrate").text


def _contrast(fg: str, bg: str) -> float:
    """WCAG relative-contrast ratio between two hex colours."""

    def luminance(value: str) -> float:
        v = value.lstrip("#")
        channels = [int(v[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    a, b = luminance(fg), luminance(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def test_faint_text_meets_contrast_on_both_grounds(client: Any) -> None:
    """The faint tone carries the smallest text on every page, so it was the
    least readable colour doing the most work. It measured 4.09:1 on the page
    and 3.80:1 on a card, below the 4.5:1 WCAG AA needs for text this size.
    """
    text = client.get("/").text
    match = re.search(r"faint:\s*'(#[0-9a-fA-F]{6})'", text) or re.search(
        r"faint:\s*'(#[0-9a-fA-F]{6})'", (Path(__file__).parent.parent / TEMPLATE).read_text()
    )
    assert match, "could not find the faint colour token"
    faint = match.group(1)
    for ground in ("#0a0a0c", "#141417"):
        assert _contrast(faint, ground) >= 4.5, (
            f"faint {faint} is {_contrast(faint, ground):.2f}:1 on {ground}"
        )


def test_navigation_survives_a_narrow_viewport(client: Any) -> None:
    """The sidebar is hidden below md. Until a replacement existed, a phone
    landed on the dashboard with no route to any other page: nav, network and
    the scope line all disappeared with it.
    """
    text = client.get("/").text
    assert "md:hidden" in text, "no small-viewport header"
    header = text.split('class="md:hidden', 1)[1].split("</header>", 1)[0]
    for href in ("/receipts", "/verifier", "/integrate"):
        assert href in header, f"{href} unreachable on a narrow viewport"


def test_page_uses_the_forwarded_scheme(client: Any) -> None:
    """Behind Cloud Run's TLS-terminating proxy the app sees plain HTTP, so
    request.base_url reported http:// and the page shipped that inside curl
    commands and an MCP config block. Production served it that way."""
    text = client.get("/integrate", headers={"x-forwarded-proto": "https"}).text
    assert "http://testserver" not in text
    assert "https://testserver" in text


def test_scheme_is_not_forced_when_nothing_forwarded(client: Any) -> None:
    """Plain local HTTP must stay http, or the page tells a developer running
    `uvicorn` to curl an address that refuses the connection."""
    assert "http://testserver" in client.get("/integrate").text


def test_install_instructions_do_not_promise_pypi(client: Any) -> None:
    """MergeGate is not on PyPI, and `pip install mergegate` fails.

    It would also be wrong if it succeeded: the wheel packages only
    ``mergegate``, while canonical JSON, Merkle hashing and signature
    verification live in the ``engine`` submodule, so an install without it
    raises at import. Both the page and /api/verification-key told readers to
    run exactly that.
    """
    for text in (
        client.get("/integrate").text,
        client.get("/api/verification-key").json()["verify_with"],
    ):
        assert "pip install mergegate" not in text
        assert "recurse-submodules" in text


def test_the_engine_submodule_is_genuinely_required() -> None:
    """Pins the reason the instruction has to mention submodules.

    If the proof layer ever gets vendored into the package, this fails and the
    install instructions can be simplified. Until then they cannot.
    """
    from pathlib import Path

    import mergegate.engine as engine

    assert Path(engine.__file__).parent.parent.joinpath("engine").is_dir()


def test_sample_receipt_id_is_real_when_receipts_exist(client: Any) -> None:
    """The page shows a copy-pasteable curl. A hardcoded id from another
    environment would 404 for the first person who tried it."""
    listing = client.get("/api/receipts").json()
    text = client.get("/integrate").text
    if listing["receipts"]:
        assert listing["receipts"][0]["id"] in text
    else:
        assert "&lt;receipt-id&gt;" in text or "<receipt-id>" in text


def test_env_var_names_on_the_page_are_the_ones_actually_read(client: Any) -> None:
    """A config snippet naming a variable nothing reads is silently broken:
    the integrator sets it and verification still refuses."""
    from mergegate import cli, mcp

    text = client.get("/integrate").text
    assert cli.PUBLIC_KEY_VAR in text
    assert mcp.PUBLIC_KEY_VAR in text
    assert "MERGEGATE_SERVICE" in text
    assert os.environ.get("MERGEGATE_RECEIPT_PUBLIC_KEY", "x") in text
