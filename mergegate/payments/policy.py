"""Reading the spending policy each agent wallet actually runs under.

The claim "agents move money autonomously" is only reassuring if the autonomy
is bounded. A wallet an agent can drain is not a delegation, it is a liability,
and the difference is a policy the wallet enforces rather than a promise the
software makes.

So this reads the live policy from Circle rather than describing an intended
one. Every figure on the wallets page comes from ``circle wallet limit``, and a
wallet whose policy cannot be read says so instead of rendering an empty table
that looks like "no limits".

**Read-only, deliberately.** ``circle wallet limit set`` requires a human OTP,
and that is the correct design: a spending policy an agent could widen is not a
spending policy. Nothing in MergeGate raises its own limits, and there is no
code path here that tries.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["WalletRole", "SpendingLimit", "WalletPolicy", "read_policy", "wallet_roles"]


def _binary() -> str:
    return (
        os.environ.get("CIRCLE_CLI_PATH")
        or shutil.which("circle")
        or str(Path.home() / ".local" / "bin" / "circle")
    )


@dataclass(frozen=True, slots=True)
class WalletRole:
    """What one wallet is for, and what it must never be able to do.

    The constraint text is the point of the table. A reader can check "receives
    the fee, never releases escrow" against the policy beside it and see whether
    the arrangement matches the description.
    """

    name: str
    address: str
    purpose: str
    constraint: str


@dataclass(frozen=True, slots=True)
class SpendingLimit:
    """One enforced rule, as Circle reports it."""

    policy_type: str
    rule_type: str
    per_tx: str = "uncapped"
    daily: str = "uncapped"
    weekly: str = "uncapped"
    monthly: str = "uncapped"
    origin: str = ""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> SpendingLimit:
        def value(key: str) -> str:
            raw = data.get(key)
            return str(raw) if raw not in (None, "") else "uncapped"

        return cls(
            policy_type=str(data.get("policyType", "")),
            rule_type=str(data.get("ruleType", "")),
            per_tx=value("perTransactionLimit"),
            daily=value("dailyLimit"),
            weekly=value("weeklyLimit"),
            monthly=value("monthlyLimit"),
            origin=str(data.get("origin", "")),
        )


@dataclass(frozen=True, slots=True)
class WalletPolicy:
    """A wallet's live policy, or why it could not be read."""

    address: str
    chain: str
    wallet_id: str = ""
    limits: tuple[SpendingLimit, ...] = field(default_factory=tuple)
    available: bool = False
    error: str = ""

    @classmethod
    def unavailable(cls, address: str, chain: str, reason: str) -> WalletPolicy:
        """No policy was read.

        Distinct from an empty limit list, which would mean "read successfully,
        no limits". Rendering those the same way would turn a CLI timeout into
        an apparently unlimited wallet.
        """
        return cls(address=address, chain=chain, available=False, error=reason)


#: The four wallets and what each is allowed to be. Addresses come from the
#: environment so this file carries no deployment specifics.
def wallet_roles() -> tuple[WalletRole, ...]:
    return (
        WalletRole(
            "Buyer agent",
            os.environ.get("BUYER_AGENT_ADDRESS", ""),
            "Funds escrow and signs the payment mandate",
            "Spends only into escrow, under a monthly cap it cannot raise",
        ),
        WalletRole(
            "Escrow",
            os.environ.get("ESCROW_ADDRESS", ""),
            "Holds reward plus verifier fee until a verdict exists",
            "Pays out only on a mandate the buyer signed before the work began",
        ),
        WalletRole(
            "Provider agent",
            os.environ.get("PROVIDER_AGENT_ADDRESS", ""),
            "Receives the reward on PASS",
            "Receive-only in this flow; cannot change where a settlement goes",
        ),
        WalletRole(
            "Verifier fee",
            os.environ.get("VERIFIER_FEE_WALLET_ADDRESS", ""),
            "Receives the per-evaluation fee, whatever the verdict",
            "Never holds escrow, so it cannot release or refund a reward",
        ),
    )


def read_policy(address: str, *, chain: str = "BASE", timeout: int = 45) -> WalletPolicy:
    """Read one wallet's enforced policy. Never raises.

    A page that cannot reach the CLI should say so rather than fail to render:
    the policy table is evidence about a live system, and evidence that
    disappears when a subprocess is slow is worse than an honest gap.
    """
    if not address:
        return WalletPolicy.unavailable(address, chain, "no address configured")

    try:
        completed = subprocess.run(  # noqa: S603 - argv vector, shell=False
            [_binary(), "wallet", "limit", "--address", address, "--chain", chain, "-o", "json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return WalletPolicy.unavailable(address, chain, f"{type(exc).__name__}: {exc}")

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        return WalletPolicy.unavailable(
            address, chain, detail[0] if detail else f"exit {completed.returncode}"
        )

    try:
        payload = json.loads(completed.stdout).get("data", {})
    except json.JSONDecodeError as exc:
        return WalletPolicy.unavailable(address, chain, f"unreadable CLI output: {exc}")

    return WalletPolicy(
        address=address,
        chain=str(payload.get("blockchain", chain)),
        wallet_id=str(payload.get("walletId", "")),
        limits=tuple(SpendingLimit.from_api(p) for p in payload.get("policies", [])),
        available=True,
    )
