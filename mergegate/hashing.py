"""Deterministic hashing helpers shared across contract, grader, and receipt.

Every hash MergeGate binds into a receipt is produced here or by the shared
engine, so there is exactly one definition of "what these bytes hash to".
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .engine import canonicalize

# Domain separation: a contract hash must never collide with a grader-bundle
# hash even if the two happened to serialize to identical bytes.
CONTRACT_DOMAIN = b"MERGEGATE_CONTRACT_V1"
GRADER_DOMAIN = b"MERGEGATE_GRADER_BUNDLE_V1"
GRADER_FILE_DOMAIN = b"MERGEGATE_GRADER_FILE_V1"
MANDATE_DOMAIN = b"MERGEGATE_MANDATE_V1"
RESULT_DOMAIN = b"MERGEGATE_RESULT_V1"
OUTPUT_DOMAIN = b"MERGEGATE_COMMAND_OUTPUT_V1"
BINDING_DOMAIN = b"MERGEGATE_BINDING_V1"


def digest(domain: bytes, payload: bytes) -> str:
    """Domain-separated SHA-256, rendered as ``sha256:<hex>``."""
    return "sha256:" + hashlib.sha256(domain + b"\x00" + payload).hexdigest()


def hash_object(domain: bytes, obj: object) -> str:
    """Canonicalize (RFC 8785 subset, shared engine) then domain-separated hash."""
    return digest(domain, canonicalize(obj))


def hash_bytes(domain: bytes, data: bytes) -> str:
    return digest(domain, data)


def hash_directory(root: Path, domain: bytes = GRADER_DOMAIN) -> str:
    """Hash a directory tree deterministically, independent of filesystem order.

    The digest covers, for every regular file, its repo-relative POSIX path, its
    executable bit, and its contents. It deliberately ignores mtimes, owners, and
    directory entries so the same bundle hashes identically on any machine —
    that is what makes ``grader_hash`` reproducible for the buyer and for any
    third party re-checking a receipt.

    Symlinks are hashed by their target string rather than followed, so a grader
    bundle cannot smuggle in out-of-tree content that a verifier would resolve
    differently than the buyer did.
    """
    if not root.is_dir():
        raise NotADirectoryError(f"grader bundle is not a directory: {root}")

    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append(
                {
                    "path": rel,
                    "kind": "symlink",
                    "target": os.readlink(path),
                }
            )
            continue
        if not path.is_file():
            continue
        data = path.read_bytes()
        entries.append(
            {
                "path": rel,
                "kind": "file",
                "executable": bool(path.stat().st_mode & 0o111),
                "content": hash_bytes(GRADER_FILE_DOMAIN, data),
            }
        )

    if not entries:
        raise ValueError(f"grader bundle is empty: {root}")

    return hash_object(domain, {"entries": entries})
