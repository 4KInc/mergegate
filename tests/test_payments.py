"""Settlement rails and the executor.

The double-payment guard has two independent layers — the state machine and the
rail's idempotency key — and these tests exercise the rail layer on its own, so
a regression in one is not masked by the other still working.
"""

from __future__ import annotations

import json
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from mergegate.mandate import SettlementAction, SettlementDirective
from mergegate.payments import (
    CircleCliRail,
    FakeRail,
    RailError,
    SettlementExecutor,
    resolve_circle_binary,
)

ESCROW = "0xESCROW"
PROVIDER = "0xPROVIDER"
BUYER = "0xBUYER"
VERIFIER = "0xVERIFIER"
KEY = "sha256:" + "a" * 64


def _directive(action: SettlementAction, recipient: str) -> SettlementDirective:
    return SettlementDirective(
        action=action,
        recipient=recipient,
        amount_usdc="1.00",
        asset="USDC",
        chain="BASE",
        reason="contract evaluated PASS before the deadline",
        mandate_hash="sha256:" + "b" * 64,
        contract_hash="sha256:" + "c" * 64,
        submission_sha="1" * 40,
    )


@pytest.fixture
def rail() -> FakeRail:
    return FakeRail(balances={ESCROW: Decimal("100")})


# -- rail idempotency ---------------------------------------------------------


def test_repeated_key_does_not_send_twice(rail: FakeRail) -> None:
    first = rail.transfer(
        source=ESCROW, destination=PROVIDER, amount_usdc="1.00", idempotency_key=KEY
    )
    second = rail.transfer(
        source=ESCROW, destination=PROVIDER, amount_usdc="1.00", idempotency_key=KEY
    )

    assert first.tx_hash == second.tx_hash
    assert not first.deduplicated
    assert second.deduplicated
    assert rail.settled_count == 1
    assert rail.balances[PROVIDER] == Decimal("1.00")


def test_transfer_without_a_key_is_refused(rail: FakeRail) -> None:
    with pytest.raises(RailError, match="idempotency key"):
        rail.transfer(source=ESCROW, destination=PROVIDER, amount_usdc="1.00", idempotency_key="")


def test_insufficient_balance_raises(rail: FakeRail) -> None:
    with pytest.raises(RailError, match="cannot send"):
        rail.transfer(source=ESCROW, destination=PROVIDER, amount_usdc="1000", idempotency_key=KEY)


# -- executor -----------------------------------------------------------------


def test_release_pays_the_provider(rail: FakeRail) -> None:
    executor = SettlementExecutor(rail=rail, escrow_address=ESCROW)
    result = executor.execute(_directive(SettlementAction.RELEASE, PROVIDER), settlement_key=KEY)

    assert result.transfer.destination == PROVIDER
    assert result.settlement_tx
    assert rail.balances[PROVIDER] == Decimal("1.00")


def test_refund_returns_to_the_buyer(rail: FakeRail) -> None:
    executor = SettlementExecutor(rail=rail, escrow_address=ESCROW)
    result = executor.execute(_directive(SettlementAction.REFUND, BUYER), settlement_key=KEY)

    assert result.transfer.destination == BUYER
    assert rail.balances[BUYER] == Decimal("1.00")


def test_replayed_execution_does_not_double_pay(rail: FakeRail) -> None:
    """The rail-level guard, tested without the state machine in the way."""
    executor = SettlementExecutor(rail=rail, escrow_address=ESCROW)
    directive = _directive(SettlementAction.RELEASE, PROVIDER)

    first = executor.execute(directive, settlement_key=KEY)
    second = executor.execute(directive, settlement_key=KEY)

    assert first.settlement_tx == second.settlement_tx
    assert second.transfer.deduplicated
    assert rail.settled_count == 1
    assert rail.balances[PROVIDER] == Decimal("1.00")


def test_a_different_verdict_is_a_different_settlement(rail: FakeRail) -> None:
    """Distinct settlement keys must not be deduplicated together."""
    executor = SettlementExecutor(rail=rail, escrow_address=ESCROW)
    executor.execute(_directive(SettlementAction.RELEASE, PROVIDER), settlement_key=KEY)
    executor.execute(
        _directive(SettlementAction.REFUND, BUYER), settlement_key="sha256:" + "f" * 64
    )
    assert rail.settled_count == 2


# -- verifier fee (P2.2) ------------------------------------------------------


def test_verifier_fee_is_a_separate_transfer(rail: FakeRail) -> None:
    executor = SettlementExecutor(
        rail=rail,
        escrow_address=ESCROW,
        verifier_fee_address=VERIFIER,
        verifier_fee_usdc="0.05",
    )
    result = executor.execute(_directive(SettlementAction.RELEASE, PROVIDER), settlement_key=KEY)

    assert result.verifier_fee_tx
    assert result.verifier_fee_tx != result.settlement_tx
    assert rail.balances[VERIFIER] == Decimal("0.05")


def test_fee_to_the_settlement_recipient_is_skipped(rail: FakeRail) -> None:
    """Two transfers to one beneficiary is not 'escrow pays the verifier'.

    Configuring the fee wallet as the provider's address would produce a receipt
    claiming a verifier fee that is really just more of the payout. Skip it
    rather than overstate.
    """
    executor = SettlementExecutor(
        rail=rail,
        escrow_address=ESCROW,
        verifier_fee_address=PROVIDER,
        verifier_fee_usdc="0.05",
    )
    result = executor.execute(_directive(SettlementAction.RELEASE, PROVIDER), settlement_key=KEY)

    assert result.verifier_fee_tx == ""
    assert rail.balances[PROVIDER] == Decimal("1.00")


def test_a_failed_fee_does_not_fail_the_settlement(rail: FakeRail) -> None:
    """The provider is owed the settlement; our fee is our problem."""
    executor = SettlementExecutor(
        rail=rail,
        escrow_address=ESCROW,
        verifier_fee_address=VERIFIER,
        verifier_fee_usdc="0.05",
    )
    directive = _directive(SettlementAction.RELEASE, PROVIDER)

    # The settlement transfer succeeds; the fee transfer is made to fail.
    rail.transfer(source=ESCROW, destination=PROVIDER, amount_usdc="1.00", idempotency_key=KEY)
    rail.fail_next_with = "gateway unavailable"

    result = executor.execute(directive, settlement_key=KEY)
    assert result.settlement_tx
    assert result.verifier_fee_tx == ""


# -- Circle CLI rail ----------------------------------------------------------


def _fake_cli(
    tmp_path: Path, payload: dict[str, Any], *, exit_code: int = 0, stderr: str = ""
) -> str:
    """A stand-in ``circle`` binary that records argv and prints a fixed response."""
    script = tmp_path / "circle"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, pathlib\n"
        f"pathlib.Path({str(tmp_path / 'argv.json')!r}).write_text(json.dumps(sys.argv[1:]))\n"
        f"sys.stderr.write({stderr!r})\n"
        f"print(json.dumps({payload!r}))\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(0o755)
    return str(script)


def test_cli_rail_builds_the_expected_command(tmp_path: Path) -> None:
    binary = _fake_cli(
        tmp_path,
        {"data": {"txHash": "0xdeadbeef", "state": "COMPLETE", "blockHeight": 42}},
    )
    rail = CircleCliRail(chain="BASE", usdc_address="0xUSDC", binary=binary)
    receipt = rail.transfer(
        source=ESCROW, destination=PROVIDER, amount_usdc="1.00", idempotency_key=KEY
    )

    argv = json.loads((tmp_path / "argv.json").read_text())
    assert argv[:2] == ["wallet", "transfer"]
    assert PROVIDER in argv
    assert "--chain" in argv and argv[argv.index("--chain") + 1] == "BASE"
    assert "--token" in argv and argv[argv.index("--token") + 1] == "0xUSDC"
    # The settlement key travels to the rail, minus the sha256: prefix.
    assert argv[argv.index("--idempotency-key") + 1] == "a" * 64

    assert receipt.tx_hash == "0xdeadbeef"
    assert receipt.explorer_url == "https://basescan.org/tx/0xdeadbeef"
    assert receipt.block_height == 42


def test_cli_rail_uses_the_sepolia_explorer_on_testnet(tmp_path: Path) -> None:
    binary = _fake_cli(tmp_path, {"data": {"txHash": "0xabc", "state": "COMPLETE"}})
    rail = CircleCliRail(chain="BASE-SEPOLIA", usdc_address="0xUSDC", binary=binary)
    receipt = rail.transfer(
        source=ESCROW, destination=PROVIDER, amount_usdc="1.00", idempotency_key=KEY
    )
    assert receipt.explorer_url == "https://sepolia.basescan.org/tx/0xabc"


def test_cli_rail_refuses_a_response_without_a_tx_hash(tmp_path: Path) -> None:
    """No hash means nothing verifiable moved; do not report success."""
    binary = _fake_cli(tmp_path, {"data": {"state": "PENDING"}})
    rail = CircleCliRail(chain="BASE", usdc_address="0xUSDC", binary=binary)
    with pytest.raises(RailError, match="no transaction hash"):
        rail.transfer(source=ESCROW, destination=PROVIDER, amount_usdc="1.00", idempotency_key=KEY)


def test_cli_rail_reports_an_expired_session_actionably(tmp_path: Path) -> None:
    """Session expiry is the operational risk of OTP auth; say so plainly."""
    binary = _fake_cli(tmp_path, {}, exit_code=1, stderr="Error: unauthorized (401)")
    rail = CircleCliRail(chain="BASE", usdc_address="0xUSDC", binary=binary)
    with pytest.raises(RailError, match="circle login"):
        rail.transfer(source=ESCROW, destination=PROVIDER, amount_usdc="1.00", idempotency_key=KEY)


def test_cli_rail_refuses_a_transfer_without_a_key(tmp_path: Path) -> None:
    binary = _fake_cli(tmp_path, {"data": {"txHash": "0xabc"}})
    rail = CircleCliRail(chain="BASE", usdc_address="0xUSDC", binary=binary)
    with pytest.raises(RailError, match="idempotency key"):
        rail.transfer(source=ESCROW, destination=PROVIDER, amount_usdc="1.00", idempotency_key="")


def test_cli_rail_parses_a_usdc_balance(tmp_path: Path) -> None:
    binary = _fake_cli(
        tmp_path,
        {"data": {"balances": [{"token": {"symbol": "USDC"}, "amount": "2.64"}]}},
    )
    rail = CircleCliRail(chain="BASE", usdc_address="0xUSDC", binary=binary)
    assert rail.balance_usdc(ESCROW) == "2.64"


def test_missing_cli_explains_which_circle_product_is_needed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The failure mode we actually hit: CLI absent, and the REST API is a
    different product with different wallets."""
    monkeypatch.delenv("CIRCLE_CLI_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda _name: None)

    with pytest.raises(RailError, match="agent wallets"):
        resolve_circle_binary()


def test_cli_path_override_must_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIRCLE_CLI_PATH", "/nonexistent/circle")
    with pytest.raises(RailError, match="does not exist"):
        resolve_circle_binary()


def test_timeout_warns_that_a_transfer_may_have_landed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed-out transfer is ambiguous, not failed. Blind retry could double-pay."""
    binary = _fake_cli(tmp_path, {"data": {"txHash": "0xabc"}})

    def _timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="circle", timeout=1)

    monkeypatch.setattr(subprocess, "run", _timeout)
    rail = CircleCliRail(chain="BASE", usdc_address="0xUSDC", binary=binary, timeout=1)
    with pytest.raises(RailError, match="may still have been submitted"):
        rail.transfer(source=ESCROW, destination=PROVIDER, amount_usdc="1.00", idempotency_key=KEY)
