"""The command line a third party uses to check a receipt for themselves.

``pyproject.toml`` has declared ``mergegate = "mergegate.cli:main"`` since the
start, and the receipt page tells the reader to run ``mergegate verify
<id>.json``. Until this module existed, that command raised ImportError. A
project whose central claim is *independently verifiable receipts* has to ship
the thing that does the verifying, so this is the claim's implementation rather
than a convenience wrapper.

**Why the key is not fetched by default.** Verification needs the issuer's
public key, and the obvious convenience is to download it from the same service
that served the receipt. That is circular: a service that forged a receipt would
happily serve the key that matches it. So the default is to require the key out
of band, and ``--key-from-service`` exists but says plainly what it costs. The
guarantee being defended is "this receipt was issued by the holder of key K",
which means the reader has to pin K by some route other than asking the
signer.

**Exit codes**, because the intended caller is another program:

* ``0`` receipt verified
* ``1`` receipt failed verification (this is a result, not a crash)
* ``2`` could not attempt verification: bad usage, unreadable file, no key
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .receipt import verify_receipt

__all__ = ["main"]

PUBLIC_KEY_VAR = "MERGEGATE_RECEIPT_PUBLIC_KEY"
SERVICE_VAR = "MERGEGATE_SERVICE"
DEFAULT_SERVICE = "https://mergegate.dev"

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_UNUSABLE = 2


class CliError(Exception):
    """Something stopped verification from being attempted at all.

    Distinct from a receipt that verifies to False: that is an answer, this is
    the absence of one, and they must not share an exit code.
    """


# -- inputs -------------------------------------------------------------------


def _load_public_key(raw: str) -> Ed25519PublicKey:
    """Parse a base64url Ed25519 public key, padded or not."""
    try:
        padded = raw.strip() + "=" * (-len(raw.strip()) % 4)
        return Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(padded))
    except Exception as exc:  # noqa: BLE001 - any parse failure is the same answer
        raise CliError(f"not a usable Ed25519 public key: {exc}") from exc


def _read_json(source: str) -> Any:
    """Read JSON from a path, or from stdin when the path is ``-``."""
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text()
    except OSError as exc:
        raise CliError(f"cannot read {source}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CliError(f"{source} is not valid JSON: {exc}") from exc


def _get(url: str, timeout: float = 15.0) -> Any:
    """Fetch and parse JSON over HTTP."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise CliError(f"{url} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise CliError(f"cannot reach {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CliError(f"{url} did not return JSON: {exc}") from exc


def _service(args: argparse.Namespace) -> str:
    return str(getattr(args, "service", None) or os.environ.get(SERVICE_VAR) or DEFAULT_SERVICE)


def _resolve_key(args: argparse.Namespace) -> tuple[Ed25519PublicKey, str]:
    """Find the verification key and report where it came from.

    The provenance string is printed, not just used, because "which key did you
    trust" is the whole question and a silent answer is a bad one.
    """
    if args.public_key:
        return _load_public_key(args.public_key), "--public-key"
    if args.key_from_service:
        base = _service(args).rstrip("/")
        payload = _get(f"{base}/api/verification-key")
        key = payload.get("public_key")
        if not isinstance(key, str) or not key:
            raise CliError(f"{base} did not publish a verification key")
        return _load_public_key(key), f"{base} (issuer-supplied, see warning)"
    env = os.environ.get(PUBLIC_KEY_VAR, "")
    if env:
        return _load_public_key(env), f"${PUBLIC_KEY_VAR}"
    raise CliError(
        "no verification key. Pass --public-key <base64url>, set "
        f"${PUBLIC_KEY_VAR}, or use --key-from-service to take the issuer's "
        "own word for it."
    )


# -- commands -----------------------------------------------------------------


def _cmd_verify(args: argparse.Namespace) -> int:
    envelope = _read_json(args.receipt)
    if not isinstance(envelope, dict):
        raise CliError("a receipt envelope must be a JSON object")
    public_key, provenance = _resolve_key(args)
    result = verify_receipt(envelope, public_key=public_key)

    if args.json:
        print(
            json.dumps(
                {
                    "valid": result.valid,
                    "checks": {name: ok for name, ok in result.checks},
                    "failures": list(result.failures),
                    "key_source": provenance,
                },
                indent=2,
            )
        )
        return EXIT_OK if result.valid else EXIT_INVALID

    print(f"key: {provenance}")
    if args.key_from_service:
        print(
            "  warning: this key came from the same service that served the "
            "receipt, so it proves internal consistency, not authenticity."
        )
    print()
    for name, ok in result.checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print()
    print(result.summary())
    if not result.valid:
        return EXIT_INVALID
    print(
        "\nThis attests verified contract acceptance, not code quality, "
        "security, or mergeworthiness.\nsettlement_tx, verifier_fee_tx, reason, "
        "settlement_asset and settlement_chain rest on\nthe signature alone; "
        "confirming the money moved means looking at the chain."
    )
    return EXIT_OK


def _cmd_fetch(args: argparse.Namespace) -> int:
    base = _service(args).rstrip("/")
    envelope = _get(f"{base}/receipts/{args.receipt_id}.json")
    text = json.dumps(envelope, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(text)
    return EXIT_OK


def _cmd_key(args: argparse.Namespace) -> int:
    base = _service(args).rstrip("/")
    payload = _get(f"{base}/api/verification-key")
    print(payload.get("public_key", ""))
    print(
        f"kid: {payload.get('kid', 'unknown')}\n"
        "Pin this out of band. Taking it from the issuer proves only that the "
        "issuer is self-consistent.",
        file=sys.stderr,
    )
    return EXIT_OK


# -- entry point --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mergegate",
        description="Verify MergeGate settlement receipts without trusting MergeGate.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    verify = sub.add_parser("verify", help="check a receipt envelope offline")
    verify.add_argument("receipt", help="path to a receipt JSON file, or - for stdin")
    verify.add_argument("--public-key", help="base64url Ed25519 public key")
    verify.add_argument(
        "--key-from-service",
        action="store_true",
        help="fetch the key from the issuing service (weaker: see the printed warning)",
    )
    verify.add_argument("--service", help=f"service base URL (default ${SERVICE_VAR})")
    verify.add_argument("--json", action="store_true", help="machine-readable output")
    verify.set_defaults(func=_cmd_verify)

    fetch = sub.add_parser("fetch", help="download a receipt envelope by id")
    fetch.add_argument("receipt_id")
    fetch.add_argument("--service", help=f"service base URL (default ${SERVICE_VAR})")
    fetch.add_argument("-o", "--output", help="write here instead of stdout")
    fetch.set_defaults(func=_cmd_fetch)

    key = sub.add_parser("key", help="print the key a service publishes")
    key.add_argument("--service", help=f"service base URL (default ${SERVICE_VAR})")
    key.set_defaults(func=_cmd_key)

    return parser


#: Options whose value can legitimately begin with "-".
_VALUE_OPTIONS = ("--public-key", "--service", "--output", "-o")


def _rejoin_leading_dash_values(argv: list[str]) -> list[str]:
    """Let option values start with ``-``.

    base64url keys are drawn from an alphabet including ``-``, so about one key
    in sixty begins with one. argparse reads that as the next flag and exits 2
    with "expected one argument", which is a baffling way to tell someone their
    key is fine but their shell parsing is not. Rewriting to ``--opt=value``
    ahead of parsing is the only thing argparse honours.

    Found by a test that failed roughly one run in three, because the key it
    generated was random each time.
    """
    out: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in _VALUE_OPTIONS and index + 1 < len(argv) and argv[index + 1].startswith("-"):
            out.append(f"{token}={argv[index + 1]}")
            index += 2
            continue
        out.append(token)
        index += 1
    return out


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(_rejoin_leading_dash_values(list(argv or sys.argv[1:])))
    try:
        return int(args.func(args))
    except CliError as exc:
        print(f"mergegate: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE


if __name__ == "__main__":  # pragma: no cover - process entry
    raise SystemExit(main())
