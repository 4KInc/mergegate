"""Circle CLI agent-wallet rail.

Circle ships two distinct wallet products. **Agent Wallets** authenticate by
email OTP into a local session and are driven through the ``circle`` CLI;
**Developer-Controlled Wallets** authenticate with an API key plus an entity
secret and are driven through the REST API. They hold different wallets: an
address that exists in one is invisible to the other.

MergeGate uses Agent Wallets because that is where the funded Base mainnet
wallets already are, and because the sibling Verigate deployment has been moving
real USDC through this path.

**On the eligibility rule.** The OTP login provisions a credential once, the way
a service account is provisioned. Each transfer after that is initiated by the
buyer agent with no human action: nobody clicks approve, and there is no
checkout step. That is what the prize rule is about. The credential's *origin*
being interactive does not put a human in the funding path, but it does mean the
session can expire, which is an operational risk rather than a design one.

Shelling out to a CLI inside a payment path is not elegant. It is behind
:class:`~mergegate.payments.base.SettlementRail` precisely so it can be replaced
without touching settlement logic.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .base import RailError, TransferReceipt

__all__ = ["CircleCliRail", "resolve_circle_binary"]

DEFAULT_TIMEOUT = 180

# Fixed namespace for deriving Circle idempotency UUIDs from settlement keys.
# Must never change: altering it would remap every existing settlement to a
# different UUID, so a replayed event would look new to Circle and pay twice.
_IDEMPOTENCY_NAMESPACE = uuid.UUID("6f9d3c1e-4a2b-5e8d-9c7f-1b2a3c4d5e6f")


def resolve_circle_binary() -> str:
    """Locate the ``circle`` CLI, or raise with something actionable.

    Checked in order: ``CIRCLE_CLI_PATH``, the user-local npm install, then
    ``PATH``. Matches how the sibling deployment resolves it.
    """
    override = os.environ.get("CIRCLE_CLI_PATH")
    if override:
        if not Path(override).is_file():
            raise RailError(f"CIRCLE_CLI_PATH points at {override}, which does not exist")
        return override

    local = Path.home() / ".local" / "bin" / "circle"
    if local.is_file():
        return str(local)

    found = shutil.which("circle")
    if found:
        return found

    raise RailError(
        "the Circle CLI is not installed. MergeGate settles through Circle agent "
        "wallets, which are driven by the CLI rather than the REST API. Install it, "
        "run `circle login`, or set CIRCLE_CLI_PATH."
    )


class CircleCliRail:
    """Settlement over Circle agent wallets via the CLI."""

    def __init__(
        self,
        *,
        chain: str = "BASE",
        usdc_address: str,
        binary: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self._chain = chain
        self._usdc = usdc_address
        self._binary = binary
        self._timeout = timeout

    @property
    def chain(self) -> str:
        return self._chain

    # -- plumbing -------------------------------------------------------------

    def _run(self, args: list[str], *, timeout: int | None = None) -> dict[str, Any]:
        binary = self._binary or resolve_circle_binary()
        cmd = [binary, *args, "-o", "json"]
        env = {**os.environ, "CIRCLE_ACCEPT_TERMS": "1"}
        try:
            proc = subprocess.run(  # noqa: S603 - argv vector, shell=False
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout or self._timeout,
                env=env,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RailError(
                f"circle {' '.join(args)} timed out after {timeout or self._timeout}s. "
                "A transfer may still have been submitted: check the wallet before retrying."
            ) from exc
        except OSError as exc:
            raise RailError(f"could not execute the Circle CLI: {exc}") from exc

        if proc.returncode != 0:
            stderr = proc.stderr.strip()
            if _looks_like_auth_failure(stderr):
                raise RailError(
                    "the Circle CLI session is not valid: it has expired or was never "
                    f"established. Run `circle login`. Underlying error: {stderr}"
                )
            raise RailError(f"circle {' '.join(args)} failed: {stderr}")

        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RailError(
                f"circle {' '.join(args)} returned output that is not JSON: {proc.stdout[:200]!r}"
            ) from exc
        if not isinstance(parsed, dict):
            raise RailError(
                f"circle {' '.join(args)} returned {type(parsed).__name__}, not an object"
            )
        return parsed

    # -- rail ------------------------------------------------------------------

    def transfer(
        self,
        *,
        source: str,
        destination: str,
        amount_usdc: str,
        idempotency_key: str,
    ) -> TransferReceipt:
        """Move USDC, idempotent on ``idempotency_key``.

        Verified against real Circle infrastructure on BASE-SEPOLIA: sending the
        same key twice returns the identical transaction hash and moves the
        funds once.

        ``TransferReceipt.deduplicated`` is always ``False`` here. Circle
        returns the original transaction on a repeated key but does not signal
        that it deduplicated, and inferring it from a local record would report
        our own bookkeeping rather than what Circle did. The field stays
        meaningful for rails that do signal it.
        """
        if not idempotency_key:
            raise RailError(
                "refusing to transfer without an idempotency key: it is the rail-level "
                "guard against double-payment"
            )

        payload = self._run(
            [
                "wallet",
                "transfer",
                destination,
                "--amount",
                amount_usdc,
                "--address",
                source,
                "--chain",
                self._chain,
                "--token",
                self._usdc,
                "--idempotency-key",
                _cli_idempotency_key(idempotency_key),
            ]
        )
        tx = payload.get("data") or {}
        tx_hash = str(tx.get("txHash", ""))
        if not tx_hash:
            raise RailError(
                f"Circle reported no transaction hash for the transfer to {destination}: {tx}"
            )

        return TransferReceipt(
            tx_hash=tx_hash,
            state=str(tx.get("state", "UNKNOWN")),
            source=str(tx.get("sourceAddress", source)),
            destination=str(tx.get("destinationAddress", destination)),
            amount_usdc=amount_usdc,
            chain=self._chain,
            explorer_url=self.explorer_url(tx_hash),
            idempotency_key=idempotency_key,
            block_height=tx.get("blockHeight"),
        )

    def balance_usdc(self, address: str) -> str:
        payload = self._run(["wallet", "balance", "--address", address, "--chain", self._chain])
        balances = (payload.get("data") or {}).get("balances") or []
        for entry in balances:
            token = entry.get("token") or {}
            symbol = str(token.get("symbol", "")).upper()
            if symbol == "USDC" or str(token.get("tokenAddress", "")).lower() == self._usdc.lower():
                return str(entry.get("amount", "0"))
        return "0"

    def explorer_url(self, tx_hash: str) -> str:
        host = "sepolia.basescan.org" if "SEPOLIA" in self._chain.upper() else "basescan.org"
        return f"https://{host}/tx/{tx_hash}"


def _cli_idempotency_key(settlement_key: str) -> str:
    """Adapt a MergeGate settlement key to the UUID that Circle requires.

    Circle's API rejects a bare ``sha256:<hex>`` (and the stripped 64-hex form)
    with ``400 Invalid request body``; it accepts only UUIDs. Verified against
    the live CLI, not inferred.

    The mapping is UUIDv5 over a fixed namespace, so it is **deterministic**:
    the same settlement always derives the same UUID. That is the whole point -
    a random UUID would satisfy the format and silently destroy the guard,
    because a retry would present a fresh key and Circle would send again.
    """
    return str(uuid.uuid5(_IDEMPOTENCY_NAMESPACE, settlement_key))


def _looks_like_auth_failure(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(
        marker in lowered
        for marker in ("unauthorized", "not logged in", "session", "expired", "401", "authenticate")
    )
