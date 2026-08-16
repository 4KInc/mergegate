"""The demo runner: one funded task, end to end, on a real chain.

This wires the pieces that are individually tested into the flow a judge
actually watches:

    buyer agent funds escrow and signs a mandate
        → provider agent pushes a commit
        → sealed evaluation against the buyer's pinned grader
        → the mandate is executed, USDC moves
        → one receipt binds the whole chain, verifiable offline

Two flows, and the second is the point. **PASS** releases to the provider.
**FAIL** is a submission whose code is functionally correct (it would pass the
buyer's tests) but which also edits a protected path. It is rejected before
the tests ever run, and escrow returns to the buyer. That is the difference
between a control layer and "CI plus a transfer".

Nothing here decides anything. The verdict is computed by the verifier and the
payment by the buyer's pre-signed mandate; this module only sequences them and
records what happened.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from .contract import SealedContract, TaskContract, build_contract
from .hashing import hash_directory
from .mandate import PaymentMandate
from .payments import CircleCliRail, SettlementExecutor
from .payments.base import SettlementRail
from .receipt import build_receipt, sign_receipt, verify_receipt
from .retry import files_to_revert
from .settlement import TaskState, TaskStateMachine, settlement_key
from .store import MemoryTaskStore, TaskStore
from .verifier.dispatch import CloudRunJob, build_request, run_sealed_evaluation
from .verifier.evaluate import evaluate
from .verifier.git_source import build_submission, materialize_base_tree, resolve_sha
from .verifier.manifest import Verdict, VerificationManifest

__all__ = ["DemoConfig", "DemoRunner", "load_config", "main"]

BUGGY_BASELINE = (
    "def add(a, b):\n"
    "    # BUG: negative operands short-circuit to zero.\n"
    "    # The buyer's pinned grader asserts add(-1, -1) == -2.\n"
    "    if a < 0 or b < 0:\n"
    "        return 0\n"
    "    return a + b\n"
)
"""The unfixed state the demo grades against.

Restored before every run, because the demo pushes its submission to ``main``
and then reads ``origin/main`` as the base for the next one. After a single
PASS flow the fix *is* the baseline, so ``git commit`` finds nothing to commit
and the run dies between funding escrow and submitting — money moved, no
verdict. Anyone cloning this to reproduce a receipt would hit that on their
first attempt, which makes an evidence-driven system unreproducible at exactly
the point it is claiming to be reproducible.
"""

PASS_PATCH = "def add(a, b):\n    return a + b\n"
"""An honest fix. The buyer's grader passes on this."""

DEPLOY_WORKFLOW = """# PROTECTED PATH.
#
# The task contract lists .github/** as protected. A submission that modifies
# this file is rejected regardless of whether the pinned tests pass \u2014 that is
# the FAIL\u2192refund demo flow.
name: deploy
on:
  push:
    branches: [main]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - run: echo "deploy gate"
"""
"""The intact protected file, restored alongside the baseline.

Kept here rather than only in the demo repository so a FAIL run, which
deliberately guts this file, cannot leave the next run with nothing to
violate. Without it the FAIL flow degrades into a PASS after one use.
"""

PROTECTED_PATCH = "on: []\njobs: {}\n"
"""Disables the deploy workflow. Paired with the correct fix above, this is the
FAIL flow: the code works, the submission still does not satisfy the contract."""


def _add_usdc(*amounts: str) -> str:
    """Sum USDC amounts exactly. Decimal, never float: USDC has 6 decimals
    and float arithmetic would produce an amount nobody authorized."""
    total = sum((Decimal(a) for a in amounts if a), Decimal(0))
    return f"{total:f}"


@dataclass(frozen=True, slots=True)
class DemoConfig:
    repo: str
    buyer: str
    provider: str
    escrow: str
    verifier_fee_wallet: str
    reward_usdc: str
    verifier_fee_usdc: str
    chain: str
    usdc_address: str
    verifier_image: str
    circle_cli: str
    deadline_hours: int = 6

    sealed_job: CloudRunJob | None = None
    """The Cloud Run job to grade in, or ``None`` to grade in this process.

    Left optional rather than required because the two modes make genuinely
    different claims, and the receipt says which one it was. An in-process run
    is reproducible on a laptop with no GCP project and is what the tests use;
    it reports :data:`EGRESS_UNRESTRICTED`, because that is what is true of a
    process running wherever the operator happens to be.

    A run with a job configured is graded inside the sealed container, and only
    then does the receipt carry the sealed posture. The distinction is the whole
    point: for a while every receipt asserted a sandbox that had never run.
    """

    verifier_python: str = "python"
    """The interpreter the contract pins for the graded run.

    Defaults to what exists in the verifier image, not to whatever built the
    contract. Pinning ``sys.executable`` put an absolute virtualenv path from
    the operator's laptop into the terms, which the sealed image cannot resolve:
    every graded run would exit 127 and FAIL a correct submission. Overridable
    so an in-process run can name an interpreter that exists locally, which is
    the only reason this is a knob rather than a constant.
    """

    @property
    def explorer_base(self) -> str:
        host = "sepolia.basescan.org" if "SEPOLIA" in self.chain.upper() else "basescan.org"
        return f"https://{host}/tx/"


def load_config(env_path: Path | str = ".env") -> DemoConfig:
    """Read the demo configuration, failing loudly on anything missing."""
    values: dict[str, str] = {}
    path = Path(env_path)
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    values.update({k: v for k, v in os.environ.items() if k in values or k.isupper()})

    def need(key: str) -> str:
        value = values.get(key, "")
        if not value:
            raise RuntimeError(
                f"{key} is not configured. The demo moves real USDC and pins a real "
                "image digest; it will not run against defaults."
            )
        return value

    return DemoConfig(
        repo=need("DEMO_REPO"),
        buyer=need("BUYER_AGENT_ADDRESS"),
        provider=need("PROVIDER_AGENT_ADDRESS"),
        escrow=need("ESCROW_ADDRESS"),
        verifier_fee_wallet=values.get("VERIFIER_FEE_WALLET_ADDRESS", ""),
        reward_usdc=need("DEMO_REWARD_USDC"),
        verifier_fee_usdc=values.get("VERIFIER_FEE_USDC", ""),
        chain=need("CIRCLE_BLOCKCHAIN"),
        usdc_address=need("USDC_CONTRACT_ADDRESS"),
        verifier_image=need("VERIFIER_IMAGE_DIGEST"),
        circle_cli=values.get("CIRCLE_CLI_PATH", ""),
        sealed_job=_sealed_job(values),
    )


def _sealed_job(values: dict[str, str]) -> CloudRunJob | None:
    """Build the sealed job from configuration, or return ``None`` deliberately.

    All four settings are required together. A partially configured job is
    refused rather than ignored, because the failure mode of ignoring it is the
    worst one available here: the run would silently grade in-process and the
    receipt would carry a weaker posture than the operator believed they had
    configured. Loud is better than quietly unsealed.
    """
    settings = {
        "VERIFIER_JOB_NAME": values.get("VERIFIER_JOB_NAME", ""),
        "VERIFIER_JOB_REGION": values.get("VERIFIER_JOB_REGION", ""),
        "GOOGLE_CLOUD_PROJECT": values.get("GOOGLE_CLOUD_PROJECT", ""),
        "EVIDENCE_BUCKET": values.get("EVIDENCE_BUCKET", ""),
    }

    missing = sorted(key for key, value in settings.items() if not value)
    if len(missing) == len(settings):
        return None
    if missing:
        raise RuntimeError(
            f"the sealed verifier job is half-configured; missing {', '.join(missing)}. "
            "Refusing to fall back to an in-process run, which would issue receipts "
            "claiming less isolation than intended."
        )
    return CloudRunJob(
        name=settings["VERIFIER_JOB_NAME"],
        region=settings["VERIFIER_JOB_REGION"],
        project=settings["GOOGLE_CLOUD_PROJECT"],
        bucket=settings["EVIDENCE_BUCKET"],
    )


def load_signing_key(secret_name: str = "mergegate-signing-key") -> tuple[str, Ed25519PrivateKey]:
    """Load the receipt signing key from Secret Manager.

    Falls back to an ephemeral key only when explicitly asked, because a receipt
    signed by a key nobody can look up proves nothing to a third party.
    """
    if os.environ.get("MERGEGATE_EPHEMERAL_KEY") == "1":
        return "mergegate-ephemeral", Ed25519PrivateKey.generate()

    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    proc = subprocess.run(  # noqa: S603 - argv vector, shell=False
        [
            "gcloud",
            "secrets",
            "versions",
            "access",
            "latest",
            f"--secret={secret_name}",
            *([f"--project={project}"] if project else []),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"could not read the receipt signing key from Secret Manager: "
            f"{proc.stderr.strip()}. Set MERGEGATE_EPHEMERAL_KEY=1 for a dry run "
            "whose receipts are not independently verifiable."
        )
    payload = json.loads(proc.stdout)
    key = load_pem_private_key(payload["private_pem"].encode(), password=None)
    assert isinstance(key, Ed25519PrivateKey)
    return str(payload["kid"]), key


class DemoRunner:
    """Sequences one task from funding to receipt."""

    def __init__(
        self,
        config: DemoConfig,
        *,
        rail: SettlementRail,
        store: TaskStore | None = None,
        workdir: Path | None = None,
        signing: tuple[str, Ed25519PrivateKey] | None = None,
        receipts: Any = None,
        contracts: Any = None,
        advisory: Any = None,
    ) -> None:
        self.config = config
        self.rail = rail
        self.store = store or MemoryTaskStore()
        self.receipts = receipts
        self.contracts = contracts
        self.advisory = advisory
        self.workdir = workdir or Path(tempfile.mkdtemp(prefix="mergegate-demo-"))
        self.signing = signing
        self.repo_dir = self.workdir / "repo"
        self.grader_dir = Path(__file__).resolve().parent.parent / "demo" / "grader"
        self.last_funding_tx: str = ""
        """The escrow funding transaction, carried from ``fund`` to the receipt.

        The receipt embeds the manifest and mandate whole, but neither records
        where the money came from, so without this a reader can confirm what was
        *decided* and what was *paid out* while having to take the funding on
        trust. It is the one link in the chain that was missing.
        """

        self.last_execution_id: str = ""
        """The sealed job execution that produced the most recent manifest.

        Empty after an in-process run, and that emptiness is meaningful: it is
        how a reader tells a receipt whose verdict came out of a sealed
        container from one whose verdict came out of whatever machine happened
        to be running the demo.
        """

    # -- setup ----------------------------------------------------------------

    def clone(self) -> Path:
        """Clone the demo repository. The verifier reads only pinned SHAs from it."""
        if self.repo_dir.exists():
            shutil.rmtree(self.repo_dir)
        subprocess.run(  # noqa: S603 - argv vector, shell=False
            ["gh", "repo", "clone", self.config.repo, str(self.repo_dir), "--", "-q"],
            check=True,
            capture_output=True,
        )
        return self.repo_dir

    def build_contract(self, base_sha: str, *, retry_of: str = "") -> TaskContract:
        """Pin every term the evaluator will consult, before any submission.

        ``retry_of`` links a second attempt to the contract it follows. It goes
        in ``metadata``, which is hashed but never read by the evaluator: the
        link is provenance, so an auditor can follow a retry back to the failure
        that prompted it, and it must not be something a verdict could depend on.
        """
        return build_contract(
            metadata=(("retry_of", retry_of),) if retry_of else (),
            grader_bundle=self.grader_dir,
            task_id=self.config.repo,
            repository=self.config.repo,
            base_sha=base_sha,
            verifier_image_digest=self.config.verifier_image,
            # "python", not sys.executable. The contract commits to grading in
            # the pinned verifier image, and sys.executable is a path on
            # whatever machine built the contract. The mainnet receipts issued
            # before the sealed job existed record
            # "/Users/.../.venv/bin/python", which does not exist in the image:
            # once the job is the executor, every one of those would exit 127,
            # command not found, and FAIL a correct submission.
            #
            # The runner sets PATH to the container's own directories, so this
            # resolves deterministically inside the image and cannot be
            # redirected by the surrounding environment.
            required_commands=((self.config.verifier_python, "-m", "pytest", "-q"),),
            allowed_source_paths=("src/**",),
            protected_paths=(".github/**",),
            grader_paths=("tests/**", "conftest.py"),
            reward_usdc=self.config.reward_usdc,
            buyer_agent=self.config.buyer,
            provider_agent=self.config.provider,
            deadline=datetime.now(UTC) + timedelta(hours=self.config.deadline_hours),
        )

    # -- P0.1: the buyer agent funds and signs -------------------------------

    def fund(self, contract: TaskContract) -> tuple[SealedContract, PaymentMandate, str]:
        """Move the reward into escrow and seal the contract against that tx.

        This is the agent-funded step: no human approves the transfer, and the
        contract becomes immutable the moment it is sealed.
        """
        mandate = PaymentMandate(
            task_id=contract.task_id,
            contract_hash=contract.contract_hash,
            buyer_agent=self.config.buyer,
            provider_agent=self.config.provider,
            amount_usdc=self.config.reward_usdc,
            asset="USDC",
            chain=self.config.chain,
            deadline=contract.deadline,
            nonce=contract.contract_hash[-16:],
        )
        # Escrow must hold the reward *and* the verifier fee. Funding only the
        # reward leaves nothing for the fee once the provider is paid, and the
        # executor treats a failed fee as non-fatal, so the shortfall would not
        # break settlement, it would just silently produce an empty
        # verifier_fee_tx and quietly drop P2.2.
        funding_amount = _add_usdc(self.config.reward_usdc, self.config.verifier_fee_usdc)
        funding = self.rail.transfer(
            source=self.config.buyer,
            destination=self.config.escrow,
            amount_usdc=funding_amount,
            # Funding is idempotent on the mandate: re-running the demo with the
            # same contract must not fund twice.
            idempotency_key=mandate.mandate_hash,
        )
        sealed = contract.seal(funding_tx=funding.tx_hash, mandate_hash=mandate.mandate_hash)

        machine = TaskStateMachine(
            task_id=contract.task_id,
            contract_hash=sealed.contract_hash,
            mandate=mandate,
        )
        self.store.put(machine)

        # Persist the terms and the funding transaction. A receipt binds only
        # contract_hash and carries no funding tx, so without this the contract
        # page would have nothing real to render.
        if self.contracts is not None:
            self.contracts.put(
                {
                    "contract_hash": sealed.contract_hash,
                    "task_id": contract.task_id,
                    "repository": contract.repository,
                    "funding_tx": funding.tx_hash,
                    "funded_amount_usdc": funding_amount,
                    "chain": self.config.chain,
                    "mandate_hash": mandate.mandate_hash,
                    "mandate_statement": mandate.statement(),
                    "terms": contract.to_canonical_dict(),
                }
            )

        self.last_funding_tx = funding.tx_hash
        return sealed, mandate, funding.tx_hash

    # -- the provider agent --------------------------------------------------

    def reset_baseline(self) -> str:
        """Restore the unfixed repository state and return the base SHA.

        Idempotent in both directions: it pushes only when something actually
        differs, so a repository already at the baseline is left untouched and
        keeps its SHA. That matters because the base SHA goes into the contract
        hash, and a reset that always produced a new commit would give every
        run a different contract for identical terms.
        """
        (self.repo_dir / "src" / "calc.py").write_text(BUGGY_BASELINE)
        (self.repo_dir / ".github" / "workflows" / "deploy.yml").write_text(DEPLOY_WORKFLOW)

        status = subprocess.run(  # noqa: S603 - argv vector, shell=False
            ["git", "-C", str(self.repo_dir), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
        if status.stdout.strip():
            self._commit_and_push("Restore the demo baseline")

        return resolve_sha(self.repo_dir, "HEAD")

    def _commit_and_push(self, message: str) -> None:
        for argv in (
            ["git", "-C", str(self.repo_dir), "add", "-A"],
            [
                "git",
                "-C",
                str(self.repo_dir),
                "-c",
                "user.name=MergeGate Provider Agent",
                "-c",
                "user.email=provider@mergegate.invalid",
                "commit",
                "-q",
                "-m",
                message,
            ],
            ["git", "-C", str(self.repo_dir), "push", "-q", "origin", "HEAD:main"],
        ):
            subprocess.run(argv, check=True, capture_output=True)  # noqa: S603

    def remediate(self, contract: TaskContract, base_sha: str) -> tuple[str, tuple[str, ...]]:
        """Undo the term violation, keep the work, and push the result.

        This is the step that closes the loop, and what it does *not* do is the
        point. It does not ask a model to write code. Gemini explains why the
        submission failed; the repair is computed by
        :func:`~mergegate.retry.files_to_revert` from the contract's own path
        guard, so the same failure always produces the same remediation.

        Reverting is the whole remedy here because the failure is a term
        violation rather than a wrong answer: the submission's code was correct
        and would have passed. Restoring the protected file to the base commit
        leaves exactly the work behind. A submission that failed because the
        tests genuinely did not pass has nothing to revert, and this returns an
        empty tuple rather than pretending otherwise.

        Returns the new submission SHA and the files it restored.
        """
        changed = tuple(
            line.strip()
            for line in subprocess.run(  # noqa: S603 - argv vector, shell=False
                ["git", "-C", str(self.repo_dir), "diff", "--name-only", f"{base_sha}..HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            if line.strip()
        )

        reverted = files_to_revert(changed, contract)
        if not reverted:
            return "", ()

        for path in reverted:
            subprocess.run(  # noqa: S603 - argv vector, shell=False
                ["git", "-C", str(self.repo_dir), "checkout", base_sha, "--", path],
                check=True,
                capture_output=True,
            )

        self._commit_and_push("Remove the protected-path change and keep the fix")
        return resolve_sha(self.repo_dir, "HEAD"), reverted

    def submit(self, *, touch_protected: bool) -> str:
        """Push a submission as the provider agent and return its SHA.

        ``touch_protected`` produces the FAIL flow: correct code that also
        disables the deploy workflow.
        """
        calc = self.repo_dir / "src" / "calc.py"
        calc.write_text(PASS_PATCH)
        message = "Fix negative-operand bug"
        if touch_protected:
            (self.repo_dir / ".github" / "workflows" / "deploy.yml").write_text(PROTECTED_PATCH)
            message += " and disable the deploy gate"

        self._commit_and_push(message)
        return resolve_sha(self.repo_dir, "HEAD")

    # -- P0.3: sealed evaluation ---------------------------------------------

    def evaluate(self, sealed: SealedContract, submission_sha: str) -> VerificationManifest:
        base = materialize_base_tree(
            repo=self.repo_dir,
            base_sha=sealed.contract.base_sha,
            destination=self.workdir / "base",
        )
        submission = build_submission(
            repo=self.repo_dir,
            base_sha=sealed.contract.base_sha,
            submission_sha=submission_sha,
        )

        if self.config.sealed_job is None:
            return evaluate(
                sealed=sealed,
                submission=submission,
                base_tree=base,
                grader_bundle=self.grader_dir,
                destination=self.workdir / "workspace",
            )

        # Dispatched, not called. Nothing in this process can produce the
        # manifest: it is written by the job and then re-checked against the
        # request that asked for it, and a mismatch raises rather than
        # degrading to a FAIL. An orchestrator that could turn "I could not
        # reach the verifier" into "the work is rejected" would be a way to
        # refuse payment by breaking infrastructure.
        outcome = run_sealed_evaluation(
            build_request(
                sealed=sealed,
                submission=submission,
                grader_bundle=self.grader_dir,
            ),
            base_tree=base,
            grader_bundle=self.grader_dir,
            job=self.config.sealed_job,
            workdir=self.workdir / "sealed",
        )
        self.last_execution_id = outcome.execution_id
        return outcome.manifest

    # -- P0.5 / P0.6 / P0.7: settle and bind ---------------------------------

    def settle(
        self, manifest: VerificationManifest, mandate: PaymentMandate, delivery_prefix: str = "demo"
    ) -> dict[str, Any]:
        """Drive the state machine, execute the mandate, emit a bound receipt."""
        machine = self.store.get(manifest.task_id)
        if machine is None:
            raise RuntimeError(f"no funded task for {manifest.task_id}")

        machine.on_submission(
            submission_sha=manifest.submission_sha, delivery_id=f"{delivery_prefix}-sub"
        )
        machine.on_verification_started(
            submission_sha=manifest.submission_sha, delivery_id=f"{delivery_prefix}-start"
        )
        machine.on_verification_completed(manifest=manifest, delivery_id=f"{delivery_prefix}-done")
        outcome = machine.on_settlement(
            manifest=manifest, now=datetime.now(UTC), delivery_id=f"{delivery_prefix}-settle"
        )
        if outcome.directive is None:
            raise RuntimeError(f"settlement produced no directive: {outcome.detail}")

        key = settlement_key(
            task_id=manifest.task_id,
            submission_sha=manifest.submission_sha,
            contract_hash=manifest.contract_hash,
            terminal_verdict=machine.state.value
            if machine.state in (TaskState.SETTLED, TaskState.REFUNDED)
            else manifest.verdict.value,
        )
        executor = SettlementExecutor(
            rail=self.rail,
            escrow_address=self.config.escrow,
            verifier_fee_address=self.config.verifier_fee_wallet,
            verifier_fee_usdc=self.config.verifier_fee_usdc,
        )
        executed = executor.execute(outcome.directive, settlement_key=key)
        machine.record_settlement_tx(executed.settlement_tx)
        self.store.put(machine)

        body = build_receipt(
            manifest=manifest,
            mandate=mandate,
            directive=outcome.directive,
            issued_at=datetime.now(UTC),
            settlement_tx=executed.settlement_tx,
            verifier_fee_tx=executed.verifier_fee_tx,
            funding_tx=self.last_funding_tx,
            execution_id=self.last_execution_id,
        )
        kid, key_obj = self.signing or load_signing_key()
        envelope = sign_receipt(body, private_key=key_obj, kid=kid)

        # Verify immediately, from the receipt alone. A receipt that does not
        # verify is not evidence, and finding that out later is worse.
        result = verify_receipt(envelope, public_key=key_obj.public_key())
        if not result.valid:
            raise RuntimeError(f"the receipt we just issued does not verify: {result.summary()}")

        # Persist so the dashboard reflects this settlement without a redeploy.
        # Done after verification: an unverifiable receipt is not evidence and
        # should not be published as though it were.
        if self.receipts is not None:
            receipt_id = f"{manifest.task_id}-{manifest.submission_sha[:12]}".replace("/", "-")
            self.receipts.put(receipt_id, envelope)

        return {"receipt": envelope, "state": machine.state.value, "executed": executed}

    # -- advisory: Gemini, strictly after the money has moved ------------------

    def advise(
        self, contract: TaskContract, manifest: VerificationManifest, submission_sha: str
    ) -> None:
        """Screen the diff and explain a failure, then store both.

        Called after ``settle``. Screening logically belongs before the sandbox,
        but running it there would put a model call on the path a provider waits
        on, and would invite exactly the change this project refuses: someone
        noticing the score is already computed and gating the run on it. Ordering
        it last makes that impossible rather than merely discouraged.

        Everything here is best-effort. A run that settled correctly must not be
        reported as failed because an API was slow.
        """
        from .gemini import available

        if not available():
            print("  advisory          skipped, no GEMINI_API_KEY")
            return
        if self.advisory is None:
            print("  advisory          skipped, no store configured")
            return

        try:
            from dataclasses import asdict

            from .forensics import explain_failure
            from .screening import screen_diff

            diff = subprocess.run(  # noqa: S603 - argv vector, shell=False
                ["git", "-C", str(self.repo_dir), "show", "--format=", submission_sha],
                check=True,
                capture_output=True,
                text=True,
            ).stdout

            def flatten(d: dict[str, Any]) -> dict[str, Any]:
                return {k: (list(v) if isinstance(v, tuple) else v) for k, v in d.items()}

            screening = screen_diff(diff, contract)
            record: dict[str, Any] = {
                "model": screening.model,
                "screening": flatten(asdict(screening)),
            }
            print(f"  advisory screen   {screening.score}/100 {screening.band}")

            if manifest.verdict is Verdict.FAIL:
                from .retry import check_plan, plan_retry

                forensics = explain_failure(manifest, diff=diff)
                record["forensics"] = flatten(asdict(forensics))
                print(f"  advisory forensic retry likelihood {forensics.retry_likelihood or 'n/a'}")

                # The retry plan is stored with its policy check already applied.
                # Serving a plan without saying whether a provider may act on it
                # would invite an agent to spend another attempt discovering
                # what the checker already knows.
                plan = plan_retry(manifest, contract, diff=diff)
                checked = check_plan(plan, contract)
                record["retry_plan"] = {
                    **flatten(asdict(plan)),
                    "actionable": checked.ok,
                    "refusal_reasons": list(checked.reasons),
                    "disallowed_files": list(checked.disallowed_files),
                }
                print(
                    f"  advisory retry     {plan.recommendation or 'n/a'}, actionable={checked.ok}"
                )

            receipt_id = f"{manifest.task_id}-{manifest.submission_sha[:12]}".replace("/", "-")
            self.advisory.put(receipt_id, record)
        except Exception as exc:  # noqa: BLE001 - never fail a settled run
            print(f"  advisory          unavailable: {type(exc).__name__}: {exc}")


def _firestore_client() -> Any:
    """A Firestore client, preferring Application Default Credentials.

    Falls back to the gcloud access token when ADC is not configured. A local
    operator has usually run ``gcloud auth login`` but not
    ``gcloud auth application-default login``, and those are separate
    credentials; without the fallback a real run would settle on-chain and then
    fail to record anything, which is the worst of both.
    """
    from google.cloud import firestore

    try:
        return firestore.Client()
    except Exception:
        from google.oauth2.credentials import Credentials

        proc = subprocess.run(  # noqa: S603 - argv vector, shell=False
            ["gcloud", "auth", "print-access-token"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "no Firestore credentials: run `gcloud auth application-default "
                f"login`, or `gcloud auth login`. {proc.stderr.strip()}"
            ) from None
        # google-auth ships partial stubs; the constructor is untyped upstream.
        creds = Credentials(token=proc.stdout.strip())  # type: ignore[no-untyped-call]
        return firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT"), credentials=creds)


def _stores() -> tuple[Any, Any, Any]:
    """Receipt, contract and advisory stores, when a project is configured."""
    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        return None, None, None
    from .store import FirestoreAdvisoryStore, FirestoreContractStore, FirestoreReceiptStore

    client = _firestore_client()
    return (
        FirestoreReceiptStore(client),
        FirestoreContractStore(client),
        FirestoreAdvisoryStore(client),
    )


def _print_flow(title: str, config: DemoConfig, result: dict[str, Any]) -> None:
    binding = result["receipt"]["body"]["binding"]
    print(f"\n=== {title} ===")
    print(f"  verdict           {binding['decision']}")
    print(
        f"  action            {binding['settlement_action']} -> {binding['settlement_recipient']}"
    )
    print(f"  amount            {binding['settlement_amount_usdc']} USDC")
    print(f"  reason            {binding['reason'][:100]}")
    print(f"  settlement tx     {config.explorer_base}{binding['settlement_tx']}")
    if binding["verifier_fee_tx"]:
        print(f"  verifier fee tx   {config.explorer_base}{binding['verifier_fee_tx']}")
    print(f"  final state       {result['state']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the MergeGate demo flows.")
    parser.add_argument(
        "flow",
        choices=["pass", "fail", "retry"],
        help=(
            "pass -> release; fail -> protected-path refund; "
            "retry -> fail, remediate, then a linked second contract that passes"
        ),
    )
    parser.add_argument("--env", default=".env")
    args = parser.parse_args(argv)

    config = load_config(args.env)
    rail = CircleCliRail(
        chain=config.chain,
        usdc_address=config.usdc_address,
        binary=config.circle_cli or None,
    )
    receipts, contracts, advisory = _stores()
    runner = DemoRunner(
        config, rail=rail, receipts=receipts, contracts=contracts, advisory=advisory
    )
    if receipts is None:
        print("  note: no GOOGLE_CLOUD_PROJECT, this run will not reach the dashboard")

    print(f"chain {config.chain}  reward {config.reward_usdc} USDC  repo {config.repo}")
    runner.clone()
    # Before reading the base, not after. The base SHA is an input to the
    # contract hash, so restoring the baseline afterwards would grade against
    # a tree the contract never named.
    base_sha = runner.reset_baseline()
    contract = runner.build_contract(base_sha)
    print(f"  base sha          {base_sha}")
    print(f"  contract hash     {contract.contract_hash}")
    print(f"  grader hash       {hash_directory(runner.grader_dir)}")

    sealed, mandate, funding_tx = runner.fund(contract)
    print(f"  escrow funded     {config.explorer_base}{funding_tx}")

    submission_sha = runner.submit(touch_protected=args.flow in ("fail", "retry"))
    print(f"  submission sha    {submission_sha}")

    manifest = runner.evaluate(sealed, submission_sha)
    # Printed because the two modes make different claims and the operator
    # should not have to infer which one just ran from the absence of a line.
    where = (
        f"sealed job {runner.last_execution_id}"
        if runner.last_execution_id
        else "IN-PROCESS (not sealed)"
    )
    print(f"  graded in         {where}")
    print(f"  egress policy     {manifest.egress_policy}")
    print(f"  verdict           {manifest.verdict.value}")
    if manifest.failed_terms:
        print(f"  failed terms      {', '.join(manifest.failed_terms)}")

    result = runner.settle(manifest, mandate)
    _print_flow(
        "PASS -> release" if manifest.verdict is Verdict.PASS else "FAIL -> refund",
        config,
        result,
    )

    # Advisory only, and last on purpose. Settlement has already executed by the
    # time any of this runs, so there is no ordering in which a model could have
    # influenced it. Failures here are printed and ignored: an advisory layer
    # that can fail a settled run is worse than no advisory layer.
    runner.advise(contract, manifest, submission_sha)

    out = Path(f"receipt-{args.flow}.json")
    out.write_text(json.dumps(result["receipt"], indent=2))
    print(f"\n  receipt written   {out}")

    if args.flow == "retry":
        return _retry_flow(runner, config, contract, base_sha, manifest)
    return 0


def _retry_flow(
    runner: DemoRunner,
    config: DemoConfig,
    failed_contract: TaskContract,
    base_sha: str,
    failed_manifest: VerificationManifest,
) -> int:
    """The second half of the loop: remediate, refund already done, try again.

    A retry is a **new contract**, not a second attempt at the old one. The
    settled task is terminal and the state machine refuses every later event,
    because the buyer's mandate authorized exactly one payment decision. So this
    funds fresh escrow on the same terms, linked to its predecessor by
    ``retry_of``.

    That link is the honest cost of the design, and it is worth reading twice:
    **the buyer pays a second verifier fee.** A retry is not free to anyone. It
    is why :class:`~mergegate.retry.RetryBudget` exists and why the plan is
    policy-checked before an attempt rather than after.
    """
    if failed_manifest.verdict is not Verdict.FAIL:
        print("\n  retry             skipped: the first attempt passed")
        return 0

    print("\n=== retry: remediate and resubmit ===")
    new_sha, reverted = runner.remediate(failed_contract, base_sha)
    if not new_sha:
        print("  remediation       nothing to revert; this failure is not fixable that way")
        return 0
    print(f"  reverted          {', '.join(reverted)}")
    print(f"  submission sha    {new_sha}")

    retry_contract = runner.build_contract(base_sha, retry_of=failed_contract.contract_hash)
    print(f"  contract hash     {retry_contract.contract_hash}")
    print(f"  retry of          {failed_contract.contract_hash}")

    sealed, mandate, funding_tx = runner.fund(retry_contract)
    print(f"  escrow funded     {config.explorer_base}{funding_tx}")

    manifest = runner.evaluate(sealed, new_sha)
    print(f"  graded in         sealed job {runner.last_execution_id or 'IN-PROCESS'}")
    print(f"  verdict           {manifest.verdict.value}")

    result = runner.settle(manifest, mandate, delivery_prefix="retry")
    _print_flow(
        "RETRY PASS -> release" if manifest.verdict is Verdict.PASS else "RETRY FAIL -> refund",
        config,
        result,
    )
    runner.advise(retry_contract, manifest, new_sha)

    out = Path("receipt-retry.json")
    out.write_text(json.dumps(result["receipt"], indent=2))
    print(f"\n  receipt written   {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
