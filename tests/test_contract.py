"""P0.2: contract immutability and grader pinning."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mergegate.contract import ContractError, TaskContract, build_contract
from mergegate.hashing import hash_directory

from .conftest import BASE_SHA, IMAGE


def test_contract_hash_is_stable_across_equal_contracts(contract: TaskContract) -> None:
    twin = dataclasses.replace(contract)
    assert contract.contract_hash == twin.contract_hash


def test_contract_hash_is_order_independent(contract: TaskContract) -> None:
    """Reordering set-like terms must not change the hash: otherwise two buyers
    expressing identical terms would fund different-looking contracts."""
    reordered = dataclasses.replace(
        contract,
        protected_paths=("Dockerfile", "deploy/**", ".github/**"),
    )
    assert reordered.contract_hash == contract.contract_hash


@pytest.mark.parametrize(
    "field,value",
    [
        ("base_sha", "f" * 40),
        ("grader_hash", "sha256:" + "d" * 64),
        ("reward_usdc", "250.01"),
        ("required_commands", (("python", "-m", "pytest", "-x"),)),
        ("allowed_source_paths", ("src/**", "lib/**")),
        ("protected_paths", (".github/**",)),
    ],
)
def test_every_term_is_bound_into_the_hash(
    contract: TaskContract, field: str, value: object
) -> None:
    """If a term can change without moving contract_hash, it is not really pinned."""
    mutated = dataclasses.replace(contract, **{field: value})  # type: ignore[arg-type]
    assert mutated.contract_hash != contract.contract_hash


def test_sealed_contract_rejects_post_funding_change(contract: TaskContract) -> None:
    """P0.2 done-when: any post-funding contract change is rejected."""
    sealed = contract.seal(funding_tx="0xfund", mandate_hash="sha256:" + "e" * 64)
    sealed.assert_intact()

    tampered = dataclasses.replace(contract, reward_usdc="1.00")
    forged = dataclasses.replace(sealed, contract=tampered)

    with pytest.raises(ContractError, match="changed after funding"):
        forged.assert_intact()


def test_sealed_contract_has_no_amendment_path(contract: TaskContract) -> None:
    sealed = contract.seal(funding_tx="0xfund", mandate_hash="sha256:" + "e" * 64)
    with pytest.raises(ContractError, match="immutable"):
        sealed.amend(reward_usdc="1.00")
    with pytest.raises(ContractError, match="immutable"):
        sealed.with_metadata(note="whatever")


def test_task_contract_is_frozen(contract: TaskContract) -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        contract.reward_usdc = "1.00"  # type: ignore[misc]


def test_seal_requires_a_funding_transaction(contract: TaskContract) -> None:
    with pytest.raises(ContractError, match="funding transaction"):
        contract.seal(funding_tx="", mandate_hash="sha256:" + "e" * 64)


# -- validation ---------------------------------------------------------------


def _kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "task_id": "task-001",
        "repository": "4KInc/demo-repo",
        "base_sha": BASE_SHA,
        "grader_hash": "sha256:" + "c" * 64,
        "verifier_image_digest": IMAGE,
        "required_commands": (("pytest",),),
        "allowed_source_paths": ("src/**",),
        "protected_paths": (".github/**",),
        "grader_paths": ("tests/**",),
        "reward_usdc": "250.00",
        "buyer_agent": "0xBUYER",
        "provider_agent": "0xPROVIDER",
        "deadline": datetime.now(UTC) + timedelta(hours=1),
    }
    base.update(overrides)
    return base


def test_base_sha_must_be_a_pinned_full_sha() -> None:
    with pytest.raises(ContractError, match="40-hex commit SHA"):
        TaskContract(**_kwargs(base_sha="main"))  # type: ignore[arg-type]


def test_verifier_image_must_be_pinned_by_digest() -> None:
    with pytest.raises(ContractError, match="pinned by digest"):
        TaskContract(**_kwargs(verifier_image_digest="mergegate/verifier:latest"))  # type: ignore[arg-type]


def test_reward_must_not_be_a_float() -> None:
    with pytest.raises(ContractError, match="decimal string"):
        TaskContract(**_kwargs(reward_usdc="250.0000001"))  # type: ignore[arg-type]


def test_deadline_must_be_timezone_aware() -> None:
    with pytest.raises(ContractError, match="timezone-aware"):
        TaskContract(**_kwargs(deadline=datetime(2030, 1, 1)))  # type: ignore[arg-type]


def test_allowed_paths_cannot_be_empty() -> None:
    with pytest.raises(ContractError, match="allowed_source_paths must be explicit"):
        TaskContract(**_kwargs(allowed_source_paths=()))  # type: ignore[arg-type]


def test_grader_paths_cannot_be_provider_writable() -> None:
    """The provider must never be able to supply the graded tests."""
    terms = _kwargs(
        allowed_source_paths=("src/**", "tests/**"),
        grader_paths=("tests/**",),
    )
    with pytest.raises(ContractError, match="own graded tests"):
        TaskContract(**terms)  # type: ignore[arg-type]


def test_protected_paths_cannot_also_be_writable() -> None:
    terms = _kwargs(allowed_source_paths=("src/**",), protected_paths=("src/**",))
    with pytest.raises(ContractError, match="both protected and writable"):
        TaskContract(**terms)  # type: ignore[arg-type]


# -- grader bundle hashing ----------------------------------------------------


def test_build_contract_derives_grader_hash_from_disk(grader_bundle: Path) -> None:
    """The buyer cannot pin a grader_hash that no real bundle produces."""
    terms = _kwargs()
    terms.pop("grader_hash")
    built = build_contract(grader_bundle=grader_bundle, **terms)
    assert built.grader_hash == hash_directory(grader_bundle)


def test_grader_hash_changes_when_a_test_changes(grader_bundle: Path) -> None:
    before = hash_directory(grader_bundle)
    test_file = grader_bundle / "tests" / "test_contract.py"
    test_file.write_text("def test_adds():\n    assert False\n")
    assert hash_directory(grader_bundle) != before


def test_grader_hash_ignores_filesystem_ordering(tmp_path: Path, grader_bundle: Path) -> None:
    """Same content written in a different order must hash identically."""
    twin = tmp_path / "grader-twin"
    (twin / "tests").mkdir(parents=True)
    (twin / "conftest.py").write_text("# buyer-controlled conftest\n")
    (twin / "tests" / "test_contract.py").write_text(
        "def test_adds():\n    from calc import add\n    assert add(2, 2) == 4\n"
    )
    assert hash_directory(twin) == hash_directory(grader_bundle)


def test_empty_grader_bundle_is_rejected(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="empty"):
        hash_directory(empty)
