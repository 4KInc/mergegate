"""Adapter onto the shared agent-authorization-gateway engine.

MergeGate does NOT reimplement canonical JSON, Merkle hashing, Ed25519 receipt
signing, or receipt verification. Those live in the shared engine (vendored at
``engine/`` as a git submodule) and are shared with Verigate. This module is the
only place that reaches into it, so the coupling stays auditable and the rest of
MergeGate imports from here.

The submodule ships a top-level ``gateway`` package with no imports in its
``__init__``, so importing ``gateway.canonical`` costs only ``cryptography``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parent.parent / "engine"

if _ENGINE_ROOT.is_dir() and str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

try:
    from gateway.canonical import CanonicalizationError, canonicalize
    from gateway.merkle import leaf_hash, node_hash
except ImportError as exc:  # pragma: no cover - configuration failure, not logic
    raise RuntimeError(
        "The shared engine is not available. MergeGate wires into "
        "agent-authorization-gateway rather than reimplementing the proof layer. "
        "Run: git submodule update --init --recursive"
    ) from exc

__all__ = [
    "CanonicalizationError",
    "canonicalize",
    "leaf_hash",
    "node_hash",
]
