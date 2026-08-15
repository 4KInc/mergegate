"""x402 verification, against signatures that are actually signed.

Every payment here is produced with a real key through ``eth_account`` and
verified through the same EIP-712 path a client would use. A fixture blob would
prove only that the parser accepts its own output.

The endpoint previously rejected every presented payment with "not
implemented". These tests exist so it can accept a genuine one and, more
importantly, so the ways it must keep refusing are pinned: wrong recipient,
short amount, expired window, wrong chain, forged signature.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from typing import Any

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from mergegate.x402 import X402Price
from mergegate.x402_settle import _typed_data as typed_data
from mergegate.x402_settle import decode_payment, verify_payment

PAY_TO = "0xe36b612ba0fd6bed653e997d5060228e548825f5"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


@pytest.fixture
def price() -> X402Price:
    return X402Price(pay_to=PAY_TO, asset=USDC, amount_usdc="0.05")


def _sign(
    price: X402Price,
    *,
    to: str | None = None,
    value: int | None = None,
    valid_after: int | None = None,
    valid_before: int | None = None,
    network: str | None = None,
    key: Any = None,
    stated_from: str | None = None,
) -> str:
    """Produce a real X-PAYMENT header, with any field overridable."""
    account = key or Account.create()
    now = int(time.time())
    authorization = {
        "from": stated_from or account.address,
        "to": to or price.pay_to,
        "value": str(value if value is not None else int(price.amount_units)),
        "validAfter": str(valid_after if valid_after is not None else now - 60),
        "validBefore": str(valid_before if valid_before is not None else now + 600),
        "nonce": "0x" + secrets.token_hex(32),
    }
    payload = decode_payment(
        base64.b64encode(
            json.dumps(
                {
                    "x402Version": 2,
                    "scheme": "exact",
                    "network": network or price.network,
                    "payload": {"signature": "0x" + "00" * 65, "authorization": authorization},
                }
            ).encode()
        ).decode()
    )
    assert payload is not None
    signed = Account.sign_message(
        encode_typed_data(full_message=typed_data(payload, price, "USD Coin", "2")),
        private_key=account.key,
    )
    return base64.b64encode(
        json.dumps(
            {
                "x402Version": 2,
                "scheme": "exact",
                "network": network or price.network,
                "payload": {
                    "signature": signed.signature.hex(),
                    "authorization": authorization,
                },
            }
        ).encode()
    ).decode()


def _verify(header: str, price: X402Price) -> Any:
    payload = decode_payment(header)
    assert payload is not None
    return verify_payment(payload, price, now=int(time.time()))


def test_a_genuine_payment_verifies(price: X402Price) -> None:
    outcome = _verify(_sign(price), price)
    assert outcome.valid, outcome.reason
    assert outcome.payer.startswith("0x")


def test_overpayment_is_accepted(price: X402Price) -> None:
    """The quote is a minimum. Rejecting more than was asked for would fail a
    payer who rounded up."""
    assert _verify(_sign(price, value=999_999), price).valid


def test_underpayment_is_refused(price: X402Price) -> None:
    outcome = _verify(_sign(price, value=1), price)
    assert not outcome.valid
    assert "requires" in outcome.reason


def test_paying_a_different_address_is_refused(price: X402Price) -> None:
    """Otherwise the endpoint reports success while someone else got the money."""
    outcome = _verify(_sign(price, to="0x000000000000000000000000000000000000dEaD"), price)
    assert not outcome.valid
    assert "different address" in outcome.reason


def test_an_expired_authorization_is_refused(price: X402Price) -> None:
    """The chain would reject it. Calling it good first means reporting a
    settlement that cannot happen."""
    now = int(time.time())
    outcome = _verify(_sign(price, valid_after=now - 600, valid_before=now - 60), price)
    assert not outcome.valid
    assert "not valid now" in outcome.reason


def test_a_not_yet_valid_authorization_is_refused(price: X402Price) -> None:
    now = int(time.time())
    outcome = _verify(_sign(price, valid_after=now + 600, valid_before=now + 1200), price)
    assert not outcome.valid


def test_an_authorization_for_another_chain_is_refused(price: X402Price) -> None:
    """A signature is bound to a chain id. Accepting one signed for a testnet
    would settle nothing while reporting payment."""
    outcome = _verify(_sign(price, network="eip155:84532"), price)
    assert not outcome.valid
    assert "settles on" in outcome.reason


def test_a_forged_signature_is_refused(price: X402Price) -> None:
    """The core check: someone else's address cannot be spent by claiming it."""
    victim = Account.create()
    attacker = Account.create()
    header = _sign(price, key=attacker, stated_from=victim.address)

    outcome = _verify(header, price)
    assert not outcome.valid
    assert "does not recover" in outcome.reason


def test_a_signature_over_different_terms_is_refused(price: X402Price) -> None:
    """Sign for 0.05 to us, then present an authorization claiming 5.00: the
    recovered signer no longer matches, because the amount is inside the
    signed struct."""
    header = _sign(price)
    data = json.loads(base64.b64decode(header))
    data["payload"]["authorization"]["value"] = "5000000"
    tampered = base64.b64encode(json.dumps(data).encode()).decode()

    assert not _verify(tampered, price).valid


def test_every_check_runs_even_after_one_fails(price: X402Price) -> None:
    """A caller debugging a rejected payment should learn everything that is
    wrong with it, not only the first thing."""
    outcome = _verify(_sign(price, value=1, to="0x000000000000000000000000000000000000dEaD"), price)
    names = {name for name, _ in outcome.checks}
    assert {"scheme", "network", "recipient", "amount", "validity_window", "signature"} <= names


@pytest.mark.parametrize(
    "header",
    ["", "not-base64", base64.b64encode(b"[]").decode(), base64.b64encode(b'{"a":1}').decode()],
)
def test_malformed_headers_decode_to_none(header: str) -> None:
    """Garbage must be one answer rather than an exception out of the endpoint."""
    assert decode_payment(header) is None


def test_settlement_without_a_relayer_reports_why(
    price: X402Price, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verified-but-unsettled is a real state and has to be legible. Answering
    200 here would claim a fee that never moved."""
    from mergegate.x402_settle import RELAYER_KEY_VAR, settle_payment

    monkeypatch.delenv(RELAYER_KEY_VAR, raising=False)
    payload = decode_payment(_sign(price))
    assert payload is not None

    tx_hash, error = settle_payment(payload, price)
    assert tx_hash == ""
    assert "no relayer configured" in error
    assert "gas" in error


def test_settlement_never_raises(price: X402Price, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreachable RPC or a broken key is an outcome the endpoint reports,
    not a 500."""
    from mergegate.x402_settle import RELAYER_KEY_VAR, RPC_VAR, settle_payment

    monkeypatch.setenv(RELAYER_KEY_VAR, "0x" + "11" * 32)
    monkeypatch.setenv(RPC_VAR, "http://127.0.0.1:1")  # nothing listening
    payload = decode_payment(_sign(price))
    assert payload is not None

    tx_hash, error = settle_payment(payload, price)
    assert tx_hash == ""
    assert error
