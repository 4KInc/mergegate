"""x402 ``exact``: verifying a presented payment, and settling it.

``x402.py`` serves the challenge. This is the other half: reading the
``X-PAYMENT`` header a client returns, checking it cryptographically, and
submitting it on-chain.

**The two halves have very different requirements, and conflating them is how
this endpoint ended up dishonest before.** Verification is pure computation:
recover the signer from an EIP-712 signature and compare the authorised terms
against what was quoted. It needs no key, no gas, and no network. Settlement
submits ``transferWithAuthorization`` to the USDC contract, which needs a
relayer holding ETH for gas.

MergeGate can therefore always verify and can only sometimes settle. Those are
reported separately rather than collapsed into one boolean, because a caller
told "paid" when nothing moved is worse off than one told "verified but not
settled".

**Why the payer signs and the server submits.** EIP-3009 exists so the party
with the funds never needs gas. The buyer signs an authorisation naming amount,
recipient and a validity window; anyone may relay it. That is what makes
machine-speed payment practical, and it is also why the server must check every
field itself: the signature proves the payer authorised *something*, not that
they authorised what was asked for.

Every check below exists because skipping it is exploitable:

* wrong ``to`` means someone else gets paid while this endpoint reports success
* short ``value`` means underpayment passes
* an expired or not-yet-valid window means the chain rejects a transfer this
  code already called good
* a reused nonce means a replay settles twice, or reverts after being accepted
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any

from .x402 import X402Price

__all__ = [
    "PaymentPayload",
    "VerificationOutcome",
    "decode_payment",
    "verify_payment",
    "settle_payment",
    "relayer_configured",
    "USDC_ABI",
]

# Only the two functions this module calls. A full ABI would be noise, and
# every extra entry is a method this code could be misread as using.
USDC_ABI = [
    {
        "name": "transferWithAuthorization",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "validAfter", "type": "uint256"},
            {"name": "validBefore", "type": "uint256"},
            {"name": "nonce", "type": "bytes32"},
            {"name": "v", "type": "uint8"},
            {"name": "r", "type": "bytes32"},
            {"name": "s", "type": "bytes32"},
        ],
        "outputs": [],
    },
    {
        "name": "authorizationState",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "authorizer", "type": "address"},
            {"name": "nonce", "type": "bytes32"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
]

RELAYER_KEY_VAR = "X402_RELAYER_PRIVATE_KEY"
RPC_VAR = "BASE_RPC_URL"
DEFAULT_RPC = "https://mainnet.base.org"


@dataclass(frozen=True, slots=True)
class PaymentPayload:
    """A decoded ``X-PAYMENT`` header."""

    scheme: str
    network: str
    signature: str
    authorization: dict[str, Any]
    x402_version: int = 2

    @property
    def payer(self) -> str:
        return str(self.authorization.get("from", ""))

    @property
    def recipient(self) -> str:
        return str(self.authorization.get("to", ""))

    @property
    def value(self) -> int:
        try:
            return int(self.authorization.get("value", 0))
        except (TypeError, ValueError):
            return -1


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    """Whether a presented payment is genuine and sufficient.

    ``valid`` says the authorisation is real and matches the quote. It does
    **not** say funds moved: that is ``settle_payment``, and keeping them apart
    is the whole point of this module.
    """

    valid: bool
    reason: str = ""
    payer: str = ""
    checks: tuple[tuple[str, bool], ...] = ()


def decode_payment(header: str) -> PaymentPayload | None:
    """Decode the base64 JSON a client sends. None if it is not one."""
    try:
        raw = base64.b64decode(header, validate=True)
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 - malformed input is one answer, not many
        return None
    if not isinstance(data, dict):
        return None

    payload = data.get("payload")
    if not isinstance(payload, dict):
        return None
    authorization = payload.get("authorization")
    if not isinstance(authorization, dict):
        return None

    return PaymentPayload(
        scheme=str(data.get("scheme", "")),
        network=str(data.get("network", "")),
        signature=str(payload.get("signature", "")),
        authorization=authorization,
        x402_version=int(data.get("x402Version", 2) or 2),
    )


def _typed_data(
    payload: PaymentPayload, price: X402Price, name: str, version: str
) -> dict[str, Any]:
    """The EIP-712 struct the payer signed.

    ``verifyingContract`` is the USDC address and ``chainId`` the settlement
    chain, so a signature captured from one token or chain cannot be replayed
    against another. Both come from the quote rather than from the payload:
    taking them from attacker-supplied input would let a signature over some
    other contract verify here.
    """
    a = payload.authorization
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ],
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {
            "name": name,
            "version": version,
            "chainId": price.chain_id,
            "verifyingContract": price.asset,
        },
        "message": {
            "from": a.get("from"),
            "to": a.get("to"),
            "value": int(a.get("value", 0)),
            "validAfter": int(a.get("validAfter", 0)),
            "validBefore": int(a.get("validBefore", 0)),
            "nonce": a.get("nonce"),
        },
    }


def verify_payment(
    payload: PaymentPayload,
    price: X402Price,
    *,
    now: int,
    token_name: str = "USD Coin",
    token_version: str = "2",
) -> VerificationOutcome:
    """Check a presented authorisation against what was quoted.

    Pure: no key, no gas, no network. Every check runs even after one fails, so
    a caller debugging a rejected payment learns everything wrong with it rather
    than the first thing.
    """
    checks: list[tuple[str, bool]] = []
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok))
        if not ok:
            failures.append(detail or name)

    check("scheme", payload.scheme == "exact", f"unsupported scheme {payload.scheme!r}")
    check(
        "network",
        payload.network == price.network,
        f"authorization is for {payload.network!r}, this endpoint settles on {price.network!r}",
    )
    check(
        "recipient",
        payload.recipient.lower() == price.pay_to.lower(),
        "authorization pays a different address than the one quoted",
    )

    required = int(price.amount_units)
    check(
        "amount",
        payload.value >= required,
        f"authorized {payload.value} units, endpoint requires {required}",
    )

    try:
        valid_after = int(payload.authorization.get("validAfter", 0))
        valid_before = int(payload.authorization.get("validBefore", 0))
    except (TypeError, ValueError):
        valid_after, valid_before = -1, -1
    check("validity_window", valid_after <= now < valid_before, "authorization is not valid now")

    # Signature last: it is the expensive check, and a mismatch here means
    # something quite different from a term mismatch above.
    signer = ""
    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        message = encode_typed_data(
            full_message=_typed_data(payload, price, token_name, token_version)
        )
        signer = Account.recover_message(message, signature=payload.signature)
        check(
            "signature",
            signer.lower() == payload.payer.lower(),
            "signature does not recover to the stated payer",
        )
    except Exception as exc:  # noqa: BLE001 - any failure to recover is a failure
        check("signature", False, f"could not verify signature: {type(exc).__name__}")

    ok = all(passed for _, passed in checks)
    return VerificationOutcome(
        valid=ok,
        reason="" if ok else "; ".join(failures),
        payer=signer or payload.payer,
        checks=tuple(checks),
    )


def relayer_configured() -> bool:
    """Whether this deployment can actually submit a settlement."""
    return bool(os.environ.get(RELAYER_KEY_VAR))


def settle_payment(payload: PaymentPayload, price: X402Price) -> tuple[str, str]:
    """Submit the authorisation on-chain. Returns ``(tx_hash, error)``.

    Called only after ``verify_payment`` passes. Requires a relayer holding ETH
    for gas: EIP-3009 lets the payer avoid gas by making someone else submit,
    and that someone is this function.

    Returns an error string rather than raising so the endpoint can answer 402
    with a precise reason instead of 500. A payment that could not be settled is
    a normal outcome of an underfunded relayer, not a bug.
    """
    key = os.environ.get(RELAYER_KEY_VAR, "")
    if not key:
        return "", (
            f"no relayer configured: set {RELAYER_KEY_VAR} to a key holding ETH on "
            "Base. EIP-3009 requires the recipient side to pay gas."
        )

    try:
        from eth_account import Account
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(os.environ.get(RPC_VAR, DEFAULT_RPC)))
        relayer = Account.from_key(key)
        contract = w3.eth.contract(address=Web3.to_checksum_address(price.asset), abi=USDC_ABI)

        a = payload.authorization
        signature = bytes.fromhex(payload.signature.removeprefix("0x"))
        if len(signature) != 65:
            return "", "signature is not 65 bytes"
        r, s, v = signature[:32], signature[32:64], signature[64]
        # Some signers emit 0/1 where the contract expects 27/28.
        if v < 27:
            v += 27

        call = contract.functions.transferWithAuthorization(
            Web3.to_checksum_address(a["from"]),
            Web3.to_checksum_address(a["to"]),
            int(a["value"]),
            int(a["validAfter"]),
            int(a["validBefore"]),
            bytes.fromhex(str(a["nonce"]).removeprefix("0x")),
            v,
            r,
            s,
        )
        tx = call.build_transaction(
            {
                "from": relayer.address,
                "nonce": w3.eth.get_transaction_count(relayer.address),
                "chainId": price.chain_id,
            }
        )
        signed = relayer.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex(), ""
    except Exception as exc:  # noqa: BLE001 - reported to the caller, not raised
        return "", f"{type(exc).__name__}: {exc}"
