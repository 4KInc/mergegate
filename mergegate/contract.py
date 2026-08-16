"""P0.2: the signed, immutable task contract with a buyer-pinned grader.

The contract is the whole trust anchor. If a provider can move the acceptance
criteria after seeing them, "the tests passed" means nothing: the provider is
grading its own homework. So every term the evaluator will consult is fixed,
canonicalized, and hashed *before* the provider is allowed to submit, and the
resulting ``contract_hash`` is what the buyer's payment mandate commits to.

Immutability here is structural, not a convention: :class:`TaskContract` is a
frozen dataclass, and once :meth:`TaskContract.seal` has been called the only
object the rest of the system accepts is a :class:`SealedContract`, which
re-derives the hash on every access and refuses to hand back terms that no
longer match what was funded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .hashing import CONTRACT_DOMAIN, hash_directory, hash_object

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST_RE = re.compile(r"^[a-z0-9./_\-]+@sha256:[0-9a-f]{64}$")
_GRADER_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

CONTRACT_SCHEMA_VERSION = "mergegate.contract/v1"


class ContractError(ValueError):
    """Raised when a task contract is malformed or has been tampered with."""


@dataclass(frozen=True, slots=True)
class TaskContract:
    """The buyer's pinned terms of acceptance.

    Every field is an input to ``contract_hash``. Adding a field without adding
    it to :meth:`to_canonical_dict` would silently unbind it from the receipt,
    so the canonical dict is written out explicitly rather than derived from
    ``__dict__``.
    """

    task_id: str
    repository: str
    base_sha: str
    """The commit the provider's diff is applied on top of. Pinned, not a branch."""

    grader_hash: str
    """Hash of the buyer's grader/test bundle. Committed before any submission."""

    verifier_image_digest: str
    """Fully-qualified image digest. A tag would let the runtime image drift."""

    required_commands: tuple[tuple[str, ...], ...]
    """Argv vectors, not shell strings: nothing to quote, nothing to inject."""

    allowed_source_paths: tuple[str, ...]
    protected_paths: tuple[str, ...]
    grader_paths: tuple[str, ...]
    """Paths the buyer's grader bundle occupies. Provider edits here are rejected
    *and* overwritten at inject time: belt and braces (P0.3 step 3, P1.1)."""

    reward_usdc: str
    """Decimal string, e.g. "250.00". Never a float: floats do not canonicalize."""

    buyer_agent: str
    provider_agent: str
    deadline: datetime
    schema_version: str = CONTRACT_SCHEMA_VERSION
    metadata: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    """Advisory only. Gemini-normalized task prose may land here; nothing in the
    evaluation or settlement path reads it."""

    def __post_init__(self) -> None:
        self._validate()

    # -- validation -----------------------------------------------------------

    def _validate(self) -> None:
        if not self.task_id:
            raise ContractError("task_id is required")
        if not _SHA1_RE.match(self.base_sha):
            raise ContractError(
                f"base_sha must be a full 40-hex commit SHA, got {self.base_sha!r}. "
                "Branch names and short SHAs are not pinnable."
            )
        if not _GRADER_HASH_RE.match(self.grader_hash):
            raise ContractError(f"grader_hash must be sha256:<64 hex>, got {self.grader_hash!r}")
        if not _IMAGE_DIGEST_RE.match(self.verifier_image_digest):
            raise ContractError(
                f"verifier_image_digest must be pinned by digest "
                f"(repo@sha256:...), got {self.verifier_image_digest!r}. "
                "A mutable tag would let the verifier environment drift."
            )
        if not self.required_commands:
            raise ContractError("at least one required command must be pinned")
        for cmd in self.required_commands:
            if not cmd or not all(isinstance(part, str) and part for part in cmd):
                raise ContractError(f"required command must be a non-empty argv vector: {cmd!r}")
        if not self.allowed_source_paths:
            raise ContractError(
                "allowed_source_paths must be explicit: an empty allow-list would "
                "let a provider write anywhere outside the protected set"
            )
        if not self.grader_paths:
            raise ContractError("grader_paths must name where the buyer's grader bundle lives")
        if not _is_decimal_amount(self.reward_usdc):
            raise ContractError(
                f"reward_usdc must be a decimal string like '250.00', got {self.reward_usdc!r}"
            )
        if self.deadline.tzinfo is None:
            raise ContractError("deadline must be timezone-aware")

        overlap = set(self.protected_paths) & set(self.allowed_source_paths)
        if overlap:
            raise ContractError(
                f"paths cannot be both protected and writable by the provider: {sorted(overlap)}"
            )
        grader_overlap = set(self.grader_paths) & set(self.allowed_source_paths)
        if grader_overlap:
            raise ContractError(
                "grader paths cannot be in allowed_source_paths: that would let the "
                f"provider supply its own graded tests: {sorted(grader_overlap)}"
            )

    # -- canonical form -------------------------------------------------------

    def to_canonical_dict(self) -> dict[str, object]:
        """The exact object that gets canonicalized and hashed.

        Ordering is irrelevant (the canonicalizer sorts keys per RFC 8785), but
        types matter: no floats, no datetimes, no sets.
        """
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "repository": self.repository,
            "base_sha": self.base_sha,
            "grader_hash": self.grader_hash,
            "verifier_image_digest": self.verifier_image_digest,
            "required_commands": [list(cmd) for cmd in self.required_commands],
            "allowed_source_paths": sorted(self.allowed_source_paths),
            "protected_paths": sorted(self.protected_paths),
            "grader_paths": sorted(self.grader_paths),
            "reward_usdc": self.reward_usdc,
            "buyer_agent": self.buyer_agent,
            "provider_agent": self.provider_agent,
            "deadline": self.deadline.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "metadata": [list(pair) for pair in sorted(self.metadata)],
        }

    @classmethod
    def from_canonical_dict(cls, data: dict[str, Any]) -> TaskContract:
        """The exact inverse of :meth:`to_canonical_dict`.

        Exists so a contract can cross a process boundary, which it now has to:
        grading happens in a sealed Cloud Run job that receives the terms as
        JSON on a mounted volume. The round trip has to preserve
        ``contract_hash`` exactly or the job would grade against terms whose
        hash no longer matches what the buyer funded, so a test asserts that
        rather than assuming it.

        Tuples, not lists: ``to_canonical_dict`` sorts the path collections, and
        rebuilding them as lists would leave the dataclass unhashable and
        mutable in a type the rest of the module treats as frozen.
        """
        return cls(
            task_id=str(data["task_id"]),
            repository=str(data["repository"]),
            base_sha=str(data["base_sha"]),
            grader_hash=str(data["grader_hash"]),
            verifier_image_digest=str(data["verifier_image_digest"]),
            required_commands=tuple(tuple(c) for c in data["required_commands"]),
            allowed_source_paths=tuple(data["allowed_source_paths"]),
            protected_paths=tuple(data["protected_paths"]),
            grader_paths=tuple(data["grader_paths"]),
            reward_usdc=str(data["reward_usdc"]),
            buyer_agent=str(data["buyer_agent"]),
            provider_agent=str(data["provider_agent"]),
            deadline=datetime.fromisoformat(str(data["deadline"]).replace("Z", "+00:00")),
            schema_version=str(data.get("schema_version", CONTRACT_SCHEMA_VERSION)),
            metadata=tuple((str(k), str(v)) for k, v in data.get("metadata", [])),
        )

    @property
    def contract_hash(self) -> str:
        """Recomputed on every access: never cached, so it cannot go stale."""
        return hash_object(CONTRACT_DOMAIN, self.to_canonical_dict())

    def seal(self, *, funding_tx: str, mandate_hash: str) -> SealedContract:
        """Freeze this contract against an on-chain funding event.

        After this point the contract is immutable: the buyer's mandate commits
        to ``contract_hash``, and the escrow holds real funds against those exact
        terms. :class:`SealedContract` is the only form the evaluator accepts.
        """
        if not funding_tx:
            raise ContractError("a sealed contract must reference its escrow funding transaction")
        return SealedContract(
            contract=self,
            contract_hash=self.contract_hash,
            funding_tx=funding_tx,
            mandate_hash=mandate_hash,
        )


@dataclass(frozen=True, slots=True)
class SealedContract:
    """A funded contract. Terms are fixed; any drift is a hard error.

    ``contract_hash`` is stored at seal time *and* recomputed on validation, so
    a mutated contract object is caught rather than silently re-hashed to a new
    value that still looks internally consistent.
    """

    contract: TaskContract
    contract_hash: str
    funding_tx: str
    mandate_hash: str

    def assert_intact(self) -> None:
        """Raise if the underlying terms no longer hash to what was funded."""
        current = self.contract.contract_hash
        if current != self.contract_hash:
            raise ContractError(
                "task contract changed after funding: sealed as "
                f"{self.contract_hash}, now hashes to {current}. "
                "Post-funding contract changes are rejected."
            )

    def amend(self, **_changes: object) -> SealedContract:
        """Always raises. Present so that 'can I amend it?' has a written answer.

        A funded contract has no amendment path by design. The buyer's mandate
        authorized payment against one specific ``contract_hash``; changing a
        term would leave the escrow committed to terms nobody signed. Cancel and
        re-fund instead.
        """
        raise ContractError(
            "a funded task contract is immutable: no post-funding amendments. "
            "Cancel the contract and fund a new one to change terms."
        )

    def with_metadata(self, **_changes: object) -> SealedContract:
        raise ContractError("a funded task contract is immutable, including its metadata")


def build_contract(*, grader_bundle: Path, **kwargs: object) -> TaskContract:
    """Construct a contract, deriving ``grader_hash`` from the bundle on disk.

    This is the buyer-side entry point: it guarantees the pinned hash actually
    corresponds to a real bundle rather than a hash the caller typed in.
    """
    grader_hash = hash_directory(grader_bundle)
    return TaskContract(grader_hash=grader_hash, **kwargs)  # type: ignore[arg-type]


def _is_decimal_amount(value: str) -> bool:
    return bool(re.match(r"^\d+(\.\d{1,6})?$", value))


__all__ = [
    "CONTRACT_SCHEMA_VERSION",
    "ContractError",
    "SealedContract",
    "TaskContract",
    "build_contract",
    "replace",
]
