"""The CLI is the claim, so it gets tested like one.

``pyproject.toml`` advertised ``mergegate = "mergegate.cli:main"`` and the
receipt page told readers to run ``mergegate verify <id>.json`` while
``mergegate/cli.py`` did not exist, so the command raised ImportError. A project
selling independently verifiable receipts shipping a broken verifier is the
worst version of that bug, and the first test here is the one that would have
caught it.

The exit codes matter as much as the output: the intended caller is another
program, and "receipt is forged" (1) must not be confusable with "I could not
check" (2).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mergegate import cli
from mergegate.cli import EXIT_INVALID, EXIT_OK, EXIT_UNUSABLE, main


def test_console_script_target_is_importable() -> None:
    """The regression test for the shipped-broken-entry-point bug.

    pyproject declares this exact path; if it stops resolving, every installed
    ``mergegate`` command breaks at import and no other test would notice.
    """
    import importlib
    import tomllib

    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    for script, target in pyproject["project"]["scripts"].items():
        module_name, _, attribute = target.partition(":")
        module = importlib.import_module(module_name)
        assert callable(getattr(module, attribute)), f"{script} -> {target} is not callable"


def _public_b64(private: Ed25519PrivateKey) -> str:
    from cryptography.hazmat.primitives import serialization

    raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_verify_accepts_a_genuine_receipt(
    tmp_path: Path, signed_receipt: tuple[dict[str, Any], Ed25519PrivateKey], capsys: Any
) -> None:
    envelope, private = signed_receipt
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(envelope))

    code = main(["verify", str(path), "--public-key", _public_b64(private)])

    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "checks passed" in out
    assert "FAIL" not in out


def test_verify_rejects_a_tampered_receipt(
    tmp_path: Path, signed_receipt: tuple[dict[str, Any], Ed25519PrivateKey]
) -> None:
    """Exit 1: an answer, not a failure to answer."""
    envelope, private = signed_receipt
    envelope["body"]["binding"]["submission_sha"] = "9" * 40
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(envelope))

    assert main(["verify", str(path), "--public-key", _public_b64(private)]) == EXIT_INVALID


def test_verify_with_the_wrong_key_is_invalid_not_unusable(
    tmp_path: Path, signed_receipt: tuple[dict[str, Any], Ed25519PrivateKey]
) -> None:
    envelope, _ = signed_receipt
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(envelope))
    other = _public_b64(Ed25519PrivateKey.generate())

    assert main(["verify", str(path), "--public-key", other]) == EXIT_INVALID


def test_missing_key_is_unusable_not_invalid(
    tmp_path: Path, signed_receipt: tuple[dict[str, Any], Ed25519PrivateKey], monkeypatch: Any
) -> None:
    """The distinction that makes the exit codes worth having.

    Reporting 'invalid' when no key was supplied would tell a caller the receipt
    is forged, which is a different and much worse claim than 'I could not
    check'.
    """
    monkeypatch.delenv(cli.PUBLIC_KEY_VAR, raising=False)
    envelope, _ = signed_receipt
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(envelope))

    assert main(["verify", str(path)]) == EXIT_UNUSABLE


def test_unreadable_file_is_unusable(tmp_path: Path) -> None:
    assert main(["verify", str(tmp_path / "nope.json"), "--public-key", "AAAA"]) == EXIT_UNUSABLE


def test_malformed_json_is_unusable(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    assert main(["verify", str(path), "--public-key", "AAAA"]) == EXIT_UNUSABLE


def test_key_from_environment_is_used(
    tmp_path: Path, signed_receipt: tuple[dict[str, Any], Ed25519PrivateKey], monkeypatch: Any
) -> None:
    envelope, private = signed_receipt
    monkeypatch.setenv(cli.PUBLIC_KEY_VAR, _public_b64(private))
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(envelope))

    assert main(["verify", str(path)]) == EXIT_OK


def test_json_output_is_machine_readable(
    tmp_path: Path, signed_receipt: tuple[dict[str, Any], Ed25519PrivateKey], capsys: Any
) -> None:
    """An agent parses this, so it has to be JSON on stdout and nothing else."""
    envelope, private = signed_receipt
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(envelope))

    main(["verify", str(path), "--public-key", _public_b64(private), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["checks"]["signature"] is True
    assert payload["key_source"] == "--public-key"


def test_padded_and_unpadded_keys_both_load(
    signed_receipt: tuple[dict[str, Any], Ed25519PrivateKey],
) -> None:
    """Base64url from different tools differs only in padding, and a verifier
    that rejects one of them looks like a forged receipt to its user."""
    _, private = signed_receipt
    unpadded = _public_b64(private)
    padded = unpadded + "=" * (-len(unpadded) % 4)

    assert cli._load_public_key(unpadded).public_bytes_raw() == (
        cli._load_public_key(padded).public_bytes_raw()
    )


def test_a_key_starting_with_a_dash_is_usable(
    tmp_path: Path, signed_receipt: tuple[dict[str, Any], Ed25519PrivateKey]
) -> None:
    """base64url includes '-', so roughly one key in sixty starts with one.
    argparse read it as the next flag and exited 2 with "expected one
    argument", which tells the user their key is broken when it is not.

    This surfaced as a test failing about one run in three, because the key was
    generated fresh each time. Deterministic here: the key is forced to start
    with a dash rather than waited for.
    """
    envelope, private = signed_receipt
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(envelope))
    dashed = "-" + _public_b64(private)[1:]

    # Wrong key, so the answer is "invalid" (1), never "unusable" (2). Exit 2
    # here would mean argparse rejected the arguments before verifying at all.
    assert main(["verify", str(path), "--public-key", dashed]) == EXIT_INVALID


def test_option_values_starting_with_a_dash_are_rejoined() -> None:
    from mergegate.cli import _rejoin_leading_dash_values

    assert _rejoin_leading_dash_values(["verify", "r.json", "--public-key", "-abc"]) == [
        "verify",
        "r.json",
        "--public-key=-abc",
    ]
    # A real flag following an option must not be swallowed into it.
    assert _rejoin_leading_dash_values(["verify", "r.json", "--json"]) == [
        "verify",
        "r.json",
        "--json",
    ]
