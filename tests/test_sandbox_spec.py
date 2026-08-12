"""P1.4 — the sandbox spec refuses to describe a weakened sandbox."""

from __future__ import annotations

import pytest

from mergegate.verifier.sandbox import SandboxPolicyError, SandboxSpec, build_job_request

from .conftest import IMAGE

ARGV = ("python", "-m", "pytest", "-q")


def test_strict_defaults() -> None:
    spec = SandboxSpec(image_digest=IMAGE, argv=ARGV)
    assert spec.egress == "default-deny"
    assert spec.execution_environment == "gen2"
    assert spec.service_account == ""


def test_image_must_be_pinned_by_digest() -> None:
    with pytest.raises(SandboxPolicyError, match="pinned by digest"):
        SandboxSpec(image_digest="mergegate/verifier:latest", argv=ARGV)


def test_egress_cannot_be_opened() -> None:
    """Network access would make the release condition non-reproducible."""
    with pytest.raises(SandboxPolicyError, match="not deterministic"):
        SandboxSpec(image_digest=IMAGE, argv=ARGV, egress="allow-all")


def test_gen1_is_refused() -> None:
    with pytest.raises(SandboxPolicyError, match="gVisor"):
        SandboxSpec(image_digest=IMAGE, argv=ARGV, execution_environment="gen1")


def test_service_account_is_refused() -> None:
    with pytest.raises(SandboxPolicyError, match="credential"):
        SandboxSpec(image_digest=IMAGE, argv=ARGV, service_account="verifier@project.iam")


@pytest.mark.parametrize(
    "key",
    ["GITHUB_TOKEN", "CIRCLE_API_KEY", "aws_secret_access_key", "DB_PASSWORD"],
)
def test_secret_shaped_env_is_refused(key: str) -> None:
    with pytest.raises(SandboxPolicyError, match="no secrets"):
        SandboxSpec(image_digest=IMAGE, argv=ARGV, env=((key, "value"),))


def test_ordinary_env_is_allowed() -> None:
    spec = SandboxSpec(image_digest=IMAGE, argv=ARGV, env=(("TZ", "UTC"),))
    assert spec.env == (("TZ", "UTC"),)


@pytest.mark.parametrize("timeout", [0, -1, 3601])
def test_timeout_bounds(timeout: int) -> None:
    with pytest.raises(SandboxPolicyError, match="timeout"):
        SandboxSpec(image_digest=IMAGE, argv=ARGV, timeout_seconds=timeout)


def test_job_request_carries_the_strict_posture() -> None:
    """The asserted spec and the submitted body are the same object."""
    spec = SandboxSpec(image_digest=IMAGE, argv=ARGV)
    body = build_job_request(spec, project="demo", region="us-central1", job_name="eval-1")

    template = body["job"]["template"]["template"]
    assert template["executionEnvironment"] == "EXECUTION_ENVIRONMENT_GEN2"
    assert template["maxRetries"] == 0
    container = template["containers"][0]
    assert container["image"] == IMAGE
    assert container["command"] == ["python"]
    assert container["args"] == ["-m", "pytest", "-q"]
    assert "serviceAccount" not in template


def test_retries_are_zero() -> None:
    """A retried evaluation is a second evaluation, not the same one again —
    it would produce a second result for one submission (P0.5)."""
    spec = SandboxSpec(image_digest=IMAGE, argv=ARGV)
    body = build_job_request(spec, project="demo", region="us-central1", job_name="eval-1")
    assert body["job"]["template"]["template"]["maxRetries"] == 0
