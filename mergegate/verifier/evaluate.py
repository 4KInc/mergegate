"""The evaluation entry point: sealed contract + submission → manifest.

This is the only function that produces a verdict, and it produces it by
computing one — no caller can hand it a decision. It also enforces the one
precondition the workspace assembler takes on trust: that the grader bundle
about to be injected is the bundle the contract pinned.
"""

from __future__ import annotations

from pathlib import Path

from ..contract import ContractError, SealedContract
from ..hashing import hash_directory
from ..submission import Submission
from .manifest import VerificationManifest
from .runner import DEFAULT_TIMEOUT_SECONDS, run_pinned_commands
from .workspace import WorkspaceRejectedError, assemble_workspace

__all__ = ["evaluate"]


def evaluate(
    *,
    sealed: SealedContract,
    submission: Submission,
    base_tree: Path,
    grader_bundle: Path,
    destination: Path,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> VerificationManifest:
    """Evaluate one submission against one funded contract.

    Raises :class:`~mergegate.contract.ContractError` if the contract drifted
    from what was funded, or if the grader bundle on disk is not the one the
    contract pinned. Both are integrity failures of the evaluator's own inputs
    and must not be reported as an ordinary FAIL — a FAIL refunds the buyer and
    closes the task, which would quietly paper over a compromised verifier.
    """
    sealed.assert_intact()
    contract = sealed.contract

    actual_grader_hash = hash_directory(grader_bundle)
    if actual_grader_hash != contract.grader_hash:
        raise ContractError(
            "grader bundle does not match the hash pinned at funding: contract "
            f"pinned {contract.grader_hash}, bundle on disk hashes to "
            f"{actual_grader_hash}. Refusing to grade against an unpinned bundle."
        )

    try:
        workspace = assemble_workspace(
            base_tree=base_tree,
            submission=submission,
            contract=contract,
            grader_bundle=grader_bundle,
            destination=destination,
        )
    except WorkspaceRejectedError as rejection:
        # A contract-term violation is a complete verdict on its own. The pinned
        # commands never run: passing tests do not rescue a path violation.
        return VerificationManifest(
            task_id=contract.task_id,
            contract_hash=sealed.contract_hash,
            grader_hash=contract.grader_hash,
            base_sha=contract.base_sha,
            submission_sha=submission.submission_sha,
            tree_hash="",
            verifier_image_digest=contract.verifier_image_digest,
            commands=(),
            failed_terms=rejection.report.failed_terms,
            rejection_reason=rejection.report.summary(),
        )

    results = run_pinned_commands(
        workspace=workspace.root,
        commands=contract.required_commands,
        timeout_seconds=timeout_seconds,
    )

    return VerificationManifest(
        task_id=contract.task_id,
        contract_hash=sealed.contract_hash,
        grader_hash=contract.grader_hash,
        base_sha=contract.base_sha,
        submission_sha=submission.submission_sha,
        tree_hash=workspace.tree_hash,
        verifier_image_digest=contract.verifier_image_digest,
        commands=results,
        tamper_signals=workspace.tamper_signals,
        git_stripped=workspace.git_stripped,
    )
