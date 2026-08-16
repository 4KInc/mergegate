"""MCP server: how an agent talks to MergeGate.

The point of MergeGate is that an autonomous agent can be paid without a human
approving the payment. An agent that cannot *query* the settlement layer has to
have a human read the dashboard for it, which puts the human straight back in
the loop this project exists to remove. So the read side is exposed over MCP.

**Read only, deliberately.** No tool here moves money, funds escrow or issues a
mandate. Those need the buyer's wallet credentials, and an MCP server is
reachable by whatever the model decides to call: a prompt-injected agent with a
``fund_escrow`` tool is a wallet-draining primitive. Funding stays in the buyer's
own process where it belongs. What an agent genuinely needs from outside is to
find out whether it got paid, to check the receipt saying so, to draft terms it
can fund, and to learn what a failure would take to fix.

**Read only, still.** Drafting returns a *proposal plus a policy verdict*; it
does not create a contract, and no tool funds one.

**No SDK dependency.** The tool half of MCP is a small JSON-RPC surface
(``initialize``, ``tools/list``, ``tools/call``) over stdio, so it is
implemented directly rather than pulling a new dependency into a project days
from submission. The dispatch is a plain function on plain dicts, which also
means the tests exercise the real protocol handler instead of a mock.

Run it::

    mergegate-mcp                    # console script
    python -m mergegate.mcp          # equivalently

Configure it in any MCP client::

    {"mcpServers": {"mergegate": {"command": "mergegate-mcp",
      "env": {"MERGEGATE_SERVICE": "https://...",
              "MERGEGATE_RECEIPT_PUBLIC_KEY": "<base64url>"}}}}

``MERGEGATE_RECEIPT_PUBLIC_KEY`` is what makes ``verify_receipt`` mean anything.
Without it the tool reports that it cannot verify rather than fetching the key
from the service being checked, because a forged service would serve a matching
key and the check would pass while proving nothing.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from .cli import PUBLIC_KEY_VAR, CliError, _get, _load_public_key
from .receipt import verify_receipt

# PUBLIC_KEY_VAR is re-exported deliberately: the name of the variable a client
# must set is part of this server's interface, and the config snippet on the
# /integrate page is checked against it.
__all__ = ["TOOLS", "PUBLIC_KEY_VAR", "call_tool", "handle_message", "serve", "main"]

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "mergegate", "version": "0.1.0"}
DEFAULT_SERVICE = "https://mergegate-api-1031148889398.us-central1.run.app"


def _service() -> str:
    return os.environ.get("MERGEGATE_SERVICE", DEFAULT_SERVICE).rstrip("/")


TOOLS: list[dict[str, Any]] = [
    {
        "name": "mergegate_status",
        "description": (
            "What this MergeGate deployment attests and what custody it holds. "
            "Call this first: the scope is narrower than 'the code is good'."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "mergegate_list_receipts",
        "description": (
            "List settled tasks with decision, amount and settlement transaction. "
            "Use this to find out whether a task you submitted has been paid."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Optional exact task id to filter by, e.g. 'owner/repo'.",
                }
            },
        },
    },
    {
        "name": "mergegate_get_receipt",
        "description": (
            "Fetch one full signed receipt envelope by id, including the "
            "verification manifest and the mandate it executed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"receipt_id": {"type": "string"}},
            "required": ["receipt_id"],
        },
    },
    {
        "name": "mergegate_draft_task",
        "description": (
            "Turn a natural-language software request into structured task-contract "
            "terms, then validate them against the buyer's policy. Returns the draft "
            "AND the policy verdict: a draft that fails validation cannot be funded, "
            "and the violations say why. Gemini proposes; the policy decides."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request": {"type": "string", "description": "What the buyer wants done."},
                "repository": {"type": "string"},
                "base_sha": {"type": "string"},
                "max_reward_usdc": {"type": "string"},
                "tree": {"type": "string", "description": "Optional repository layout."},
            },
            "required": ["request", "repository", "base_sha"],
        },
    },
    {
        "name": "mergegate_get_retry_plan",
        "description": (
            "For a failed evaluation, produce a structured remediation plan and check "
            "it against the contract's path policy. Returns whether a provider agent "
            "may act on it. A plan proposing a protected path is refused here rather "
            "than after another paid attempt."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"receipt_id": {"type": "string"}},
            "required": ["receipt_id"],
        },
    },
    {
        "name": "mergegate_assess_contract",
        "description": (
            "Before accepting work: assess a contract's feasibility, sketch an "
            "implementation, and check the files it expects to touch against the "
            "contract's path policy. Returns ACCEPT / REQUEST_CLARIFICATION / "
            "DECLINE. Advisory only, and under HASH_ONLY terms the acceptance tests "
            "were not readable, so feasibility is inferred rather than verified."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "contract_hash": {"type": "string"},
                "task": {"type": "string"},
                "tree": {"type": "string"},
                "fee_usdc": {"type": "string"},
            },
            "required": ["contract_hash"],
        },
    },
    {
        "name": "mergegate_inspect_contract",
        "description": (
            "The pinned terms behind a receipt: writable paths, protected paths, "
            "commands, reward and deadline. What a provider agent reads before "
            "deciding whether to attempt the work."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"contract_hash": {"type": "string"}},
            "required": ["contract_hash"],
        },
    },
    {
        "name": "mergegate_wallet_policies",
        "description": (
            "The spending policy each agent wallet runs under, read live from Circle. "
            "Tells a counterparty what this deployment can and cannot spend."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "mergegate_verify_receipt",
        "description": (
            "Re-verify a receipt against a pinned Ed25519 public key: signature, "
            "digests recomputed from the embedded manifest, and every bound field "
            "cross-checked. Requires MERGEGATE_RECEIPT_PUBLIC_KEY to be set, and "
            "reports that it cannot verify rather than trusting the issuer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "receipt_id": {"type": "string", "description": "Fetched from the service."},
                "envelope": {"type": "object", "description": "Or supply the receipt inline."},
            },
        },
    },
]


# -- tool implementations -----------------------------------------------------


def _tool_status(_: dict[str, Any]) -> dict[str, Any]:
    return dict(_get(f"{_service()}/api/status"))


def _tool_list_receipts(args: dict[str, Any]) -> dict[str, Any]:
    payload = _get(f"{_service()}/api/receipts")
    receipts = list(payload.get("receipts", []))
    wanted = args.get("task_id")
    if wanted:
        receipts = [r for r in receipts if r.get("task_id") == wanted]
    return {
        "count": len(receipts),
        "receipts": receipts,
        # Passed through, never swallowed: "no receipts" and "cannot reach the
        # datastore" must not look the same to a caller waiting to be paid.
        "source_error": payload.get("source_error", ""),
    }


def _tool_get_receipt(args: dict[str, Any]) -> dict[str, Any]:
    receipt_id = args.get("receipt_id")
    if not receipt_id:
        raise CliError("receipt_id is required")
    return dict(_get(f"{_service()}/receipts/{receipt_id}.json"))


def _tool_verify_receipt(args: dict[str, Any]) -> dict[str, Any]:
    envelope = args.get("envelope")
    if not envelope:
        receipt_id = args.get("receipt_id")
        if not receipt_id:
            raise CliError("pass either receipt_id or envelope")
        envelope = _get(f"{_service()}/receipts/{receipt_id}.json")
    if not isinstance(envelope, dict):
        raise CliError("a receipt envelope must be a JSON object")

    raw = os.environ.get(PUBLIC_KEY_VAR, "")
    if not raw:
        return {
            "verified": False,
            "reason": (
                f"no pinned key: set {PUBLIC_KEY_VAR} in this server's environment. "
                "The key is deliberately not fetched from the service being "
                "checked, because a forged service would serve a key that matches "
                "its forged receipt."
            ),
        }

    result = verify_receipt(envelope, public_key=_load_public_key(raw))
    return {
        "verified": result.valid,
        "checks_passed": sum(1 for _, ok in result.checks if ok),
        "checks_total": len(result.checks),
        "checks": {name: ok for name, ok in result.checks},
        "failures": list(result.failures),
        "caveat": (
            "settlement_tx, verifier_fee_tx, reason, settlement_asset and "
            "settlement_chain rest on the signature alone. Confirming the money "
            "moved means reading the chain, which this cannot do."
        ),
    }


def _tool_draft_task(args: dict[str, Any]) -> dict[str, Any]:
    """Draft terms and validate them in one call.

    Returning the draft without the verdict would invite an agent to fund
    something the policy would refuse, so both travel together and the verdict
    is not optional.
    """
    from .drafting import DraftPolicy, draft_contract, validate_draft

    request = str(args.get("request", "")).strip()
    if not request:
        raise CliError("request is required")

    policy = DraftPolicy(
        repository=str(args.get("repository", "")),
        base_sha=str(args.get("base_sha", "")),
        max_reward_usdc=str(args.get("max_reward_usdc", "1.00")),
    )
    draft = draft_contract(request, policy, tree=str(args.get("tree", "")))
    verdict = validate_draft(draft, policy)

    return {
        "draft": {
            "title": draft.title,
            "scope": draft.scope,
            "allowed_source_paths": list(draft.allowed_source_paths),
            "protected_paths": list(draft.protected_paths),
            "required_commands": [list(c) for c in draft.required_commands],
            "acceptance_criteria": list(draft.acceptance_criteria),
            "reward_usdc": draft.reward_usdc,
            "deadline_hours": draft.deadline_hours,
            "assumptions": list(draft.assumptions),
            "ambiguities": list(draft.ambiguities),
            "risk_flags": list(draft.risk_flags),
            "available": draft.available,
            "error": draft.error,
        },
        "policy_verdict": {
            "may_be_funded": verdict.ok,
            "violations": list(verdict.violations),
            "checks": {name: ok for name, ok in verdict.checks},
        },
        "note": (
            "A draft is a proposal. It becomes fundable only after passing this "
            "policy check, and the buyer agent signs the validated terms."
        ),
    }


def _tool_get_retry_plan(args: dict[str, Any]) -> dict[str, Any]:
    """A remediation plan for a failed receipt, plus whether it is actionable."""
    receipt_id = args.get("receipt_id")
    if not receipt_id:
        raise CliError("receipt_id is required")

    envelope = _get(f"{_service()}/receipts/{receipt_id}.json")
    binding = (envelope.get("body") or {}).get("binding") or {}
    if str(binding.get("decision", "")).upper() != "FAIL":
        return {
            "actionable": False,
            "reason": "this receipt did not fail; there is nothing to retry",
        }

    advisory = _get(f"{_service()}/api/receipts/{receipt_id}/advisory")
    plan = advisory.get("retry_plan") or {}
    if not plan:
        return {
            "actionable": False,
            "reason": "no retry plan was stored for this evaluation",
            "failed_terms": list(
                (envelope.get("body") or {}).get("manifest", {}).get("failed_terms", [])
            ),
        }
    return plan


def _tool_assess_contract(args: dict[str, Any]) -> dict[str, Any]:
    """Assess a live contract before the provider agent commits to it.

    Fetches the pinned terms from the service and assesses them, rather than
    accepting terms from the caller. An agent that could pass in its own terms
    would be assessing a contract that may not exist, and any conclusion it drew
    would be about the wrong one.

    Like the retry plan, the assessment and the path check travel together. The
    check is the part with teeth: it uses the contract's own guard, so a plan
    expecting to edit a protected path is refused here — before any work — where
    it costs nothing.
    """
    from .contract import TaskContract
    from .feasibility import assess_contract, check_assessment

    contract_hash = str(args.get("contract_hash", "")).strip()
    if not contract_hash:
        raise CliError("contract_hash is required")

    record = _get(f"{_service()}/api/contracts/{contract_hash}")
    terms = (record or {}).get("terms") or {}
    if not terms:
        return {
            "available": False,
            "error": f"no pinned terms stored for {contract_hash}",
        }

    contract = TaskContract.from_canonical_dict(terms)
    assessment = assess_contract(
        contract,
        task=str(args.get("task", "")),
        repo_tree=str(args.get("tree", "")),
        fee_usdc=str(args.get("fee_usdc", "0.05")),
    )
    check = check_assessment(assessment, contract)

    return {
        "assessment": {
            "summary": assessment.summary,
            "implementation_plan": list(assessment.implementation_plan),
            "files_likely_to_change": list(assessment.files_likely_to_change),
            "warnings": list(assessment.warnings),
            "open_questions": list(assessment.open_questions),
            "feasibility": assessment.feasibility,
            "attempt_risk": assessment.attempt_risk,
            "recommendation": assessment.recommendation,
            "estimated_attempt_cost_usdc": assessment.estimated_attempt_cost_usdc,
            "criteria_visible": assessment.criteria_visible,
            "available": assessment.available,
            "error": assessment.error,
            "advisory": True,
        },
        "path_check": {
            "ok": check.ok,
            "reasons": list(check.reasons),
            "disallowed_files": list(check.disallowed_files),
        },
        "terms_visibility": str(contract.terms_visibility),
        "note": (
            "Advisory. Nothing here accepts the contract or moves funds, and under "
            "HASH_ONLY the acceptance tests were not readable: feasibility is "
            "inferred from the task and repository, not from the criteria that will "
            "decide payment."
        ),
    }


def _tool_inspect_contract(args: dict[str, Any]) -> dict[str, Any]:
    contract_hash = args.get("contract_hash")
    if not contract_hash:
        raise CliError("contract_hash is required")
    return dict(_get(f"{_service()}/api/contracts/{contract_hash}"))


def _tool_wallet_policies(_: dict[str, Any]) -> dict[str, Any]:
    return dict(_get(f"{_service()}/api/wallets"))


_IMPLEMENTATIONS = {
    "mergegate_status": _tool_status,
    "mergegate_list_receipts": _tool_list_receipts,
    "mergegate_get_receipt": _tool_get_receipt,
    "mergegate_draft_task": _tool_draft_task,
    "mergegate_get_retry_plan": _tool_get_retry_plan,
    "mergegate_assess_contract": _tool_assess_contract,
    "mergegate_inspect_contract": _tool_inspect_contract,
    "mergegate_wallet_policies": _tool_wallet_policies,
    "mergegate_verify_receipt": _tool_verify_receipt,
}


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run one tool and shape it as an MCP tool result.

    Failures come back as ``isError`` results rather than JSON-RPC errors: the
    model should see what went wrong and be able to correct the call, which a
    transport-level error does not let it do.
    """
    implementation = _IMPLEMENTATIONS.get(name)
    if implementation is None:
        return {"isError": True, "content": [{"type": "text", "text": f"no such tool: {name}"}]}
    try:
        payload = implementation(arguments or {})
    except CliError as exc:
        return {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
    except Exception as exc:  # noqa: BLE001 - never kill the server over one call
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
        }
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}]}


# -- protocol -----------------------------------------------------------------


def handle_message(message: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC message, or return None if no reply is owed.

    Notifications carry no id and must not be answered; replying to one is a
    protocol violation that some clients treat as fatal.
    """
    method = message.get("method")
    message_id = message.get("id")

    if message_id is None:
        return None

    def ok(result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    if method == "initialize":
        # Echo the client's protocol version when we can speak it, which is how
        # a client learns it is not talking to something it must fall back for.
        requested = (message.get("params") or {}).get("protocolVersion")
        return ok(
            {
                "protocolVersion": requested if isinstance(requested, str) else PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            }
        )
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        params = message.get("params") or {}
        return ok(call_tool(str(params.get("name", "")), params.get("arguments") or {}))
    if method == "ping":
        return ok({})

    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def serve(stdin: Any = None, stdout: Any = None) -> int:
    """Read newline-delimited JSON-RPC from stdin until it closes."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue  # a client that sends garbage is not a reason to exit
        response = handle_message(message)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv and argv[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    return serve()


if __name__ == "__main__":  # pragma: no cover - process entry
    raise SystemExit(main())
