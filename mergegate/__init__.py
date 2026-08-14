"""MergeGate: deterministic evaluator and conditional USDC settlement.

MergeGate proves that a provider agent's submission passed the buyer's pinned,
buyer-controlled test contract, and settles a pre-authorized USDC payment on
that result. The release condition is a reproducible test contract, not an LLM
opinion, an optimistic timeout, or a discretionary approval.

Scope of the guarantee: **verified contract acceptance, not code quality,
security, or mergeworthiness.**
"""

from __future__ import annotations

__version__ = "0.1.0"
