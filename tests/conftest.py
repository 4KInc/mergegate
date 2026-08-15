"""Shared fixtures. Deliberately hand-built rather than factory-generated so a
reader can see exactly which contract terms each test depends on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mergegate.contract import TaskContract

BASE_SHA = "a" * 40
IMAGE = "us-docker.pkg.dev/mergegate/verifier@sha256:" + "b" * 64


@pytest.fixture
def grader_bundle(tmp_path: Path) -> Path:
    """A minimal buyer grader bundle on disk."""
    bundle = tmp_path / "grader"
    (bundle / "tests").mkdir(parents=True)
    (bundle / "tests" / "test_contract.py").write_text(
        "def test_adds():\n    from calc import add\n    assert add(2, 2) == 4\n"
    )
    (bundle / "conftest.py").write_text("# buyer-controlled conftest\n")
    return bundle


@pytest.fixture
def signed_receipt() -> tuple[dict[str, Any], Ed25519PrivateKey]:
    """A genuine signed receipt and the key that signed it.

    Built through the real issuing path rather than hand-written JSON, so a
    change that breaks receipt construction fails the CLI and MCP tests too
    instead of leaving them passing against a shape nothing produces.
    """
    from mergegate.mandate import PaymentMandate, execute_mandate
    from mergegate.receipt import build_receipt, sign_receipt
    from mergegate.verifier.manifest import CommandResult, VerificationManifest

    contract_hash = "sha256:" + "c" * 64
    manifest = VerificationManifest(
        task_id="task-001",
        contract_hash=contract_hash,
        grader_hash="sha256:" + "d" * 64,
        base_sha=BASE_SHA,
        submission_sha="1" * 40,
        tree_hash="sha256:" + "e" * 64,
        verifier_image_digest=IMAGE,
        commands=(
            CommandResult(
                argv=("pytest", "-q"),
                exit_code=0,
                stdout_digest="sha256:" + "a" * 64,
                stderr_digest="sha256:" + "b" * 64,
                duration_ms=1200,
            ),
        ),
    )
    mandate = PaymentMandate(
        task_id="task-001",
        contract_hash=contract_hash,
        buyer_agent="0xBUYER",
        provider_agent="0xPROVIDER",
        amount_usdc="250.00",
        asset="USDC",
        chain="base",
        deadline=datetime.now(UTC) + timedelta(hours=6),
        nonce="nonce-1",
    )
    now = datetime.now(UTC)
    key = Ed25519PrivateKey.generate()
    body = build_receipt(
        manifest=manifest,
        mandate=mandate,
        directive=execute_mandate(mandate=mandate, manifest=manifest, now=now),
        issued_at=now,
        settlement_tx="0xsettle",
        verifier_fee_tx="0xfee",
    )
    return sign_receipt(body, private_key=key, kid="mergegate-test-key"), key


@pytest.fixture
def contract() -> TaskContract:
    return TaskContract(
        task_id="task-001",
        repository="4KInc/demo-repo",
        base_sha=BASE_SHA,
        grader_hash="sha256:" + "c" * 64,
        verifier_image_digest=IMAGE,
        required_commands=(("python", "-m", "pytest", "-q"),),
        allowed_source_paths=("src/**",),
        protected_paths=(".github/**", "deploy/**", "Dockerfile"),
        grader_paths=("tests/**", "conftest.py"),
        reward_usdc="250.00",
        buyer_agent="0xBUYER",
        provider_agent="0xPROVIDER",
        deadline=datetime.now(UTC) + timedelta(hours=6),
    )
