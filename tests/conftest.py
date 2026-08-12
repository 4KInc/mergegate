"""Shared fixtures. Deliberately hand-built rather than factory-generated so a
reader can see exactly which contract terms each test depends on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
