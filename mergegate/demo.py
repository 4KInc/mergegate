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
import sys
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
from .settlement import TaskState, TaskStateMachine, settlement_key
from .store import MemoryTaskStore, TaskStore
from .verifier.evaluate import evaluate
from .verifier.git_source import build_submission, materialize_base_tree, resolve_sha
from .verifier.manifest import Verdict, VerificationManifest

__all__ = ["DemoConfig", "DemoRunner", "load_config", "main"]

PASS_PATCH = "def add(a, b):\n    return a + b\n"
"""An honest fix. The buyer's grader passes on this."""

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
    ) -> None:
        self.config = config
        self.rail = rail
        self.store = store or MemoryTaskStore()
        self.receipts = receipts
        self.contracts = contracts
        self.workdir = workdir or Path(tempfile.mkdtemp(prefix="mergegate-demo-"))
        self.signing = signing
        self.repo_dir = self.workdir / "repo"
        self.grader_dir = Path(__file__).resolve().parent.parent / "demo" / "grader"

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

    def build_contract(self, base_sha: str) -> TaskContract:
        """Pin every term the evaluator will consult, before any submission."""
        return build_contract(
            grader_bundle=self.grader_dir,
            task_id=self.config.repo,
            repository=self.config.repo,
            base_sha=base_sha,
            verifier_image_digest=self.config.verifier_image,
            required_commands=((sys.executable, "-m", "pytest", "-q"),),
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

        return sealed, mandate, funding.tx_hash

    # -- the provider agent --------------------------------------------------

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
        return evaluate(
            sealed=sealed,
            submission=submission,
            base_tree=base,
            grader_bundle=self.grader_dir,
            destination=self.workdir / "workspace",
        )

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
        "flow", choices=["pass", "fail"], help="pass -> release; fail -> protected-path refund"
    )
    parser.add_argument("--env", default=".env")
    args = parser.parse_args(argv)

    config = load_config(args.env)
    rail = CircleCliRail(
        chain=config.chain,
        usdc_address=config.usdc_address,
        binary=config.circle_cli or None,
    )
    runner = DemoRunner(config, rail=rail)

    print(f"chain {config.chain}  reward {config.reward_usdc} USDC  repo {config.repo}")
    repo = runner.clone()
    base_sha = resolve_sha(repo, "origin/main")
    contract = runner.build_contract(base_sha)
    print(f"  base sha          {base_sha}")
    print(f"  contract hash     {contract.contract_hash}")
    print(f"  grader hash       {hash_directory(runner.grader_dir)}")

    sealed, mandate, funding_tx = runner.fund(contract)
    print(f"  escrow funded     {config.explorer_base}{funding_tx}")

    submission_sha = runner.submit(touch_protected=args.flow == "fail")
    print(f"  submission sha    {submission_sha}")

    manifest = runner.evaluate(sealed, submission_sha)
    print(f"  verdict           {manifest.verdict.value}")
    if manifest.failed_terms:
        print(f"  failed terms      {', '.join(manifest.failed_terms)}")

    result = runner.settle(manifest, mandate)
    _print_flow(
        "PASS -> release" if manifest.verdict is Verdict.PASS else "FAIL -> refund",
        config,
        result,
    )

    out = Path(f"receipt-{args.flow}.json")
    out.write_text(json.dumps(result["receipt"], indent=2))
    print(f"\n  receipt written   {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
