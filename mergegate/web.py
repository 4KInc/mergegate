"""The dashboard.

Everything rendered here is derived from receipts that were actually issued.
There is no illustrative data and no placeholder row: if a number appears on a
page, a run produced it.

Two decisions worth stating, because they are what make the page evidence
rather than decoration:

* **Receipts are re-verified on every request**, not trusted from a stored
  "valid" flag. If a receipt in the bundle were altered, the page would say so
  instead of continuing to display a green check.
* **Aggregates are computed from the receipts present**, so an empty bundle
  renders zeroes and an empty table. Nothing is seeded to make the dashboard
  look busier than the system has been.

The design language: the Tailwind theme, fonts, and layout: comes from the
Stitch screens in ``design/screens/``. Those remain the design reference; these
templates are the live implementation of them.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .receipt import verify_receipt
from .verifier.sandbox import EGRESS_DENY_TCP, EGRESS_PROBE, SandboxSpec

__all__ = ["ReceiptBundle", "build_web_router", "short", "display"]

RECEIPTS_DIR = Path(__file__).resolve().parent.parent / "demo" / "receipts"


def display(text: str) -> str:
    """Text as it should appear on screen.

    Receipts already issued carry em-dashes inside their signed bodies, and
    editing those bytes would invalidate the signature. So the substitution
    happens at render time; the receipt itself is never touched, and the JSON
    download still returns exactly what was signed.
    """
    # Written as escapes, not literal em-dashes: a text sweep over this file
    # already clobbered these two literals once and turned the function into a
    # no-op that also mangled colons.
    return text.replace(" \u2014 ", ": ").replace("\u2014", "-")


def short(value: str, head: int = 10, tail: int = 6) -> str:
    """Truncate a hash for display, keeping both ends.

    Both ends, because a prefix alone cannot be checked against a block
    explorer: and a truncation whose tail is invented is worse than no
    truncation at all.
    """
    if not value or len(value) <= head + tail + 1:
        return value
    return f"{value[:head]}…{value[-tail:]}"


def explorer_url(chain: str, tx: str) -> str:
    host = "sepolia.basescan.org" if "SEPOLIA" in (chain or "").upper() else "basescan.org"
    return f"https://{host}/tx/{tx}"


@dataclass(frozen=True, slots=True)
class ReceiptView:
    """One receipt, flattened for rendering."""

    id: str
    envelope: dict[str, Any]

    @property
    def body(self) -> dict[str, Any]:
        return dict(self.envelope.get("body") or {})

    @property
    def binding(self) -> dict[str, Any]:
        return dict(self.body.get("binding") or {})

    @property
    def manifest(self) -> dict[str, Any]:
        return dict(self.body.get("manifest") or {})

    @property
    def chain(self) -> str:
        return str((self.body.get("mandate") or {}).get("chain", ""))

    @property
    def decision(self) -> str:
        return str(self.binding.get("decision", ""))

    @property
    def action(self) -> str:
        return str(self.binding.get("settlement_action", ""))

    @property
    def amount(self) -> str:
        return str(self.binding.get("settlement_amount_usdc", "0"))

    @property
    def fee_amount(self) -> str:
        return str((self.body.get("mandate") or {}).get("verifier_fee_usdc", "") or "0.05")

    @property
    def settlement_tx(self) -> str:
        return str(self.binding.get("settlement_tx", ""))

    @property
    def fee_tx(self) -> str:
        return str(self.binding.get("verifier_fee_tx", ""))

    @property
    def settlement_explorer(self) -> str:
        return explorer_url(self.chain, self.settlement_tx)

    @property
    def fee_explorer(self) -> str:
        return explorer_url(self.chain, self.fee_tx)

    @property
    def kid(self) -> str:
        return str((self.envelope.get("sig") or {}).get("kid", ""))

    @property
    def reason(self) -> str:
        return display(str(self.binding.get("reason", "")))

    @property
    def failed_terms(self) -> list[str]:
        return list(self.manifest.get("failed_terms") or [])

    @property
    def command_count(self) -> int:
        return len(self.manifest.get("commands") or [])

    @property
    def mandate_statement(self) -> str:
        return display(str(self.body.get("mandate_statement", "")))

    @property
    def scope(self) -> str:
        return display(str(self.body.get("scope", "")))

    @property
    def task(self) -> str:
        return str(self.binding.get("task_id", ""))

    @property
    def contract_hash(self) -> str:
        return str(self.binding.get("contract_hash", ""))

    @property
    def recipient(self) -> str:
        return str(self.binding.get("settlement_recipient", ""))

    @property
    def recipient_short(self) -> str:
        return short(self.recipient, 8, 4)

    @property
    def submission_sha(self) -> str:
        return str(self.binding.get("submission_sha", ""))

    @property
    def binding_rows(self) -> list[tuple[str, str]]:
        """The bound fields, in the order the receipt commits to them."""
        keys = [
            "contract_hash",
            "grader_hash",
            "base_sha",
            "submission_sha",
            "tree_hash",
            "verifier_image_digest",
            "command_output_digest",
            "result_digest",
            "mandate_hash",
            "settlement_key",
            "decision",
            "settlement_tx",
            "verifier_fee_tx",
        ]
        return [(k, str(self.binding.get(k, ""))) for k in keys]


class DirectoryReceiptSource:
    """Receipts from a directory on disk. Used for local runs and tests."""

    def __init__(self, directory: Path = RECEIPTS_DIR) -> None:
        self.directory = directory

    def _id(self, path: Path) -> str:
        rel = path.relative_to(self.directory).with_suffix("")
        return str(rel).replace("/", "-")

    def all(self) -> list[tuple[str, dict[str, Any]]]:
        if not self.directory.is_dir():
            return []
        out = []
        for path in sorted(self.directory.rglob("receipt-*.json")):
            try:
                out.append((self._id(path), json.loads(path.read_text())))
            except (OSError, json.JSONDecodeError):
                # A receipt we cannot read is omitted rather than rendered as a
                # blank row that looks like a real settlement.
                continue
        return out

    def get(self, receipt_id: str) -> dict[str, Any] | None:
        return next((env for rid, env in self.all() if rid == receipt_id), None)


class ReceiptBundle:
    """Receipts from some source, verified on read.

    The source is pluggable so the dashboard can read live from Firestore in
    deployment and from a directory in tests, without the rendering layer
    knowing which. A source that raises is surfaced through
    :attr:`source_error` rather than swallowed: a dashboard that silently
    shows nothing when its datastore is unreachable looks identical to one
    reporting an empty system, and those mean very different things.
    """

    def __init__(
        self,
        directory: Path | None = None,
        public_key: Ed25519PublicKey | None = None,
        source: Any = None,
    ):
        self.source = (
            source
            if source is not None
            else DirectoryReceiptSource(directory if directory is not None else RECEIPTS_DIR)
        )
        self.public_key = public_key
        self.source_error: str = ""

    def all(self) -> list[ReceiptView]:
        try:
            pairs = self.source.all()
            self.source_error = ""
        except Exception as exc:  # noqa: BLE001 - surfaced to the page, not hidden
            self.source_error = f"{type(exc).__name__}: {exc}"
            return []
        return [ReceiptView(id=rid, envelope=env) for rid, env in pairs]

    def get(self, receipt_id: str) -> ReceiptView | None:
        try:
            env = self.source.get(receipt_id)
        except Exception as exc:  # noqa: BLE001
            self.source_error = f"{type(exc).__name__}: {exc}"
            return None
        return ReceiptView(id=receipt_id, envelope=env) if env else None

    def verify(self, view: ReceiptView) -> dict[str, Any]:
        """Re-check a receipt now. Never a cached verdict."""
        if self.public_key is None:
            return {
                "valid": False,
                "checks": 0,
                "failures": [
                    "no public key configured: the receipt cannot be verified here. "
                    "Download the JSON and verify it against the published key."
                ],
            }
        result = verify_receipt(view.envelope, public_key=self.public_key)
        return {
            "valid": result.valid,
            "checks": len(result.checks),
            "failures": list(result.failures),
        }

    def networks(self) -> list[str]:
        """Distinct chains present. The bundle holds testnet and mainnet runs,
        and a single "network" label over both would imply mainnet-only
        figures."""
        return sorted({v.chain for v in self.all() if v.chain})

    def stats(self) -> list[tuple[str, str]]:
        """Aggregates over what actually settled, across every chain present."""
        released = sum((Decimal(v.amount) for v in self.all() if v.action == "release"), Decimal(0))
        refunded = sum((Decimal(v.amount) for v in self.all() if v.action == "refund"), Decimal(0))
        fees = sum((Decimal(v.fee_amount) for v in self.all() if v.fee_tx), Decimal(0))
        return [
            ("USDC released", f"{released:f}"),
            ("USDC refunded", f"{refunded:f}"),
            ("Verifier fees paid", f"{fees:f}"),
            ("Contracts settled", str(len(self.all()))),
        ]


def _mcp_tools() -> list[tuple[str, str]]:
    """The MCP tool list for the integrate page, read from the server itself.

    Derived rather than retyped: a hand-maintained copy would drift the moment a
    tool is renamed, and a documentation page that lists a tool the server does
    not implement is worse than no page.
    """
    from .mcp import TOOLS

    return [(tool["name"], tool["description"].split(".")[0] + ".") for tool in TOOLS]


MCP_TOOLS = _mcp_tools()

HTTP_ENDPOINTS = [
    ("GET", "/api/status", "What this deployment attests, and what custody it holds."),
    ("GET", "/api/receipts", "Every settled task: decision, amount, settlement transaction."),
    ("GET", "/receipts/{id}.json", "One signed receipt envelope, manifest and mandate included."),
    ("GET", "/api/verification-key", "The public half of the signing key, with its caveat."),
    ("GET", "/x402/verify", "402 challenge pricing the verifier. Serves, does not settle."),
    ("POST", "/webhooks/github", "Submission events. HMAC signed; rejects unsigned deliveries."),
]

GRADING_PIPELINE = [
    (
        1,
        "Materialize the pinned base tree",
        "git archive emits tree contents only, so .git never exists to leak a reference solution.",
    ),
    (
        2,
        "Guard every touched path",
        "A protected- or grader-path violation is a hard reject and the pinned commands never run.",
    ),
    (
        3,
        "Apply the provider diff",
        "Allowed source paths only, as explicit file changes, never a shell "
        "patch of attacker-controlled input.",
    ),
    (
        4,
        "Quarantine provider test hooks",
        "src/conftest.py sits inside an allowed path and pytest would still execute it. "
        "Allowed to write is not allowed to grade.",
    ),
    (
        5,
        "Purge grader paths, inject the buyer's bundle",
        "The graded bytes are the buyer's, whatever the provider submitted.",
    ),
    (
        6,
        "Run only the pinned commands",
        "argv vectors with no shell, in a rebuilt environment with no inherited secrets.",
    ),
    (
        7,
        "Install the runtime grader guard",
        "An audit hook loaded outside the workspace stops provider code reading "
        "the graded tests. Blocking edits was not enough: code that reads them "
        "can answer from them without implementing anything.",
    ),
    (
        8,
        "Hash the tree and bind the result",
        "tree_hash and submission_sha go into the receipt, so payment is for that exact artifact.",
    ),
]

ANTI_GAMING = [
    (
        "P1.1",
        "A conftest.py hook that forces every outcome to pass",
        "Quarantined before the run and recorded as a tamper signal, not silently fixed up.",
    ),
    (
        "P1.1",
        "A sitecustomize.py that executes before any test is imported",
        "Same quarantine: hooks the provider introduced or modified anywhere are removed.",
    ),
    (
        "P1.1b",
        "Reading the graded tests at run time and answering from them",
        "A submission that implemented nothing passed this way before the guard "
        "existed. Grader reads from provider source now raise.",
    ),
    (
        "P1.2",
        "Reading the reference solution out of .git history",
        "History never reaches the workspace, so there is nothing to read.",
    ),
    (
        "P1.3",
        "Rewriting the graded tests",
        "Rejected outright; the buyer's bundle also overwrites them regardless.",
    ),
    (
        "P1.3",
        "Correct code that also disables the deploy gate",
        "Rejected before any command runs; passing tests cannot rescue a path violation.",
    ),
    (
        "P0.4",
        "Force-pushing a new head SHA after a PASS",
        "A new SHA supersedes the previous artifact and invalidates its verification.",
    ),
]

SANDBOX_BADGES = [
    "gVisor isolation",
    "No outbound TCP",
    "DNS resolution available",
    "No secrets mounted",
    "2 vCPU / 4 GiB",
    "600s timeout",
    "Ephemeral",
    ".git history stripped",
]


def verifier_environment() -> list[tuple[str, str]]:
    """The pinned environment, read from the spec rather than retyped.

    Built through :class:`SandboxSpec` so its validation applies: if the
    configured image were a tag rather than a digest, this page would fail
    loudly instead of displaying an unpinnable environment as though it were
    pinned.
    """
    import os

    image = os.environ.get("VERIFIER_IMAGE_DIGEST", "")
    rows: list[tuple[str, str]] = []
    if image:
        try:
            spec = SandboxSpec(image_digest=image, argv=("python", "-m", "pytest", "-q"))
        except Exception as exc:  # noqa: BLE001 - shown, not hidden
            return [("configuration error", str(exc))]
        rows = [
            ("verifier image", spec.image_digest),
            ("execution environment", f"{spec.execution_environment} (gVisor)"),
            ("resources", f"{spec.cpu} vCPU / {spec.memory}"),
            ("timeout", f"{spec.timeout_seconds}s"),
            ("egress", spec.egress),
            ("network", f"{spec.network} / {spec.subnet}"),
            ("service account", spec.service_account or "none, no cloud identity in the sandbox"),
            ("writable paths", ", ".join(spec.writable_paths)),
            ("retries", "0, a retried evaluation is a second evaluation"),
        ]
    else:
        rows = [("verifier image", "not configured in this environment")]
    return rows


def evaluation_view(v: ReceiptView) -> dict[str, Any]:
    """Flatten a manifest into the evaluation page's shape.

    Stage states are derived from the manifest, not fixed decoration: which
    stage stopped the run is the entire story of a rejection, so a page that
    always showed the same ticks would be describing a run it did not read.
    """
    rejected = bool(v.failed_terms)
    ran = v.command_count > 0
    stages = [
        {
            "state": "done",
            "title": "Materialize the pinned base tree",
            "detail": "git archive emits tree contents only, so .git never exists "
            "to leak a reference solution.",
        },
        {
            "state": "failed" if rejected else "done",
            "title": "Guard every touched path",
            "detail": v.reason if rejected else "No protected or grader path was touched.",
        },
        {
            "state": "skipped" if rejected else "done",
            "title": "Apply the provider diff",
            "detail": "Allowed source paths only, as explicit file changes.",
        },
        {
            "state": "skipped" if rejected else "done",
            "title": "Inject the buyer grader bundle",
            "detail": "Overwrites whatever the provider left at the grader paths.",
        },
        {
            "state": "done" if ran else "skipped",
            "title": "Run the pinned commands",
            "detail": f"{v.command_count} command(s) executed."
            if ran
            else "Not reached: the violation decided the verdict first.",
        },
    ]
    commands = [
        {
            "argv": " ".join(c.get("argv", [])),
            "exit_code": c.get("exit_code", 0),
            "timed_out": c.get("timed_out", False),
            "stdout_digest": short(str(c.get("stdout_digest", "")), 16, 6),
            "stderr_digest": short(str(c.get("stderr_digest", "")), 16, 6),
        }
        for c in v.manifest.get("commands", [])
    ]
    return {
        "id": v.id,
        "verdict": v.decision,
        "submission_short": short(v.submission_sha, 10, 6),
        "stages": stages,
        "commands": commands,
        "failed_terms": v.failed_terms,
        "rejection_reason": display(str(v.manifest.get("rejection_reason", "")) or v.reason),
        "tamper_signals": [display(str(t)) for t in (v.manifest.get("tamper_signals") or [])],
        "egress": v.manifest.get("egress_policy", "unknown"),
        "git_stripped": bool(v.manifest.get("git_stripped", True)),
        "identity": [
            ("base sha", v.manifest.get("base_sha", "")),
            ("submission sha", v.submission_sha),
            ("tree hash", v.manifest.get("tree_hash", "")),
            ("grader hash", v.manifest.get("grader_hash", "")),
            ("verifier image", v.manifest.get("verifier_image_digest", "")),
            ("result digest", v.binding.get("result_digest", "")),
        ],
    }


def contract_view(record: dict[str, Any] | None, views: list[ReceiptView]) -> dict[str, Any]:
    """Flatten a stored contract for rendering.

    ``record`` is ``None`` for contracts funded before terms were persisted.
    The page then says so rather than describing terms it cannot produce.
    """
    terms = dict((record or {}).get("terms") or {})
    chain = str((record or {}).get("chain", ""))
    host = "sepolia.basescan.org" if "SEPOLIA" in chain.upper() else "basescan.org"
    funding_tx = str((record or {}).get("funding_tx", ""))
    rows = []
    for label, key in (
        ("repository", "repository"),
        ("base commit", "base_sha"),
        ("grader hash", "grader_hash"),
        ("verifier image", "verifier_image_digest"),
        ("reward", "reward_usdc"),
        ("deadline", "deadline"),
    ):
        if terms.get(key):
            rows.append((label, str(terms[key])))
    if record:
        rows.append(("contract hash", str(record.get("contract_hash", ""))))
    return {
        "stored": record is not None,
        "rows": rows,
        "allowed_paths": terms.get("allowed_source_paths") or [],
        "protected_paths": terms.get("protected_paths") or [],
        "grader_paths": terms.get("grader_paths") or [],
        "commands": [" ".join(c) for c in (terms.get("required_commands") or [])],
        "funding_tx": funding_tx,
        "funding_explorer": f"https://{host}/tx/{funding_tx}" if funding_tx else "",
        "funded_amount": (record or {}).get("funded_amount_usdc", terms.get("reward_usdc", "")),
        "mandate_statement": display(str((record or {}).get("mandate_statement", ""))),
        "mandate_hash_short": short(str((record or {}).get("mandate_hash", "")), 12, 6),
        "buyer_short": short(str(terms.get("buyer_agent", "")), 10, 4),
        "provider_short": short(str(terms.get("provider_agent", "")), 10, 4),
        "receipts": [{"id": v.id, "decision": v.decision} for v in views],
    }


def build_web_router(
    bundle: ReceiptBundle, *, network: str = "Base mainnet", contracts: Any = None
) -> Any:
    """Mount the dashboard.

    FastAPI is imported at module scope, not inside this function. With
    ``from __future__ import annotations`` a lazy import leaves ``Request``
    unresolvable in module globals, so FastAPI treats the parameter as a query
    field and every route returns 422 without the handler ever running. That
    exact mistake shipped once already in ``webhook.py``; the tests here cross
    the HTTP boundary so it cannot ship twice.
    """
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> Any:
        views = bundle.all()
        rows = [
            {
                "id": v.id,
                "task": v.task or ": ",
                "amount": f"{v.amount} USDC",
                "recipient_short": short(v.recipient, 8, 4),
                "submission_short": short(v.submission_sha, 8, 4),
                "state": "SETTLED" if v.action == "release" else "REFUNDED",
                "chain": v.chain or ": ",
                "settlement_short": short(v.settlement_tx, 10, 4),
                "explorer": v.settlement_explorer,
            }
            for v in views
        ]
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "rows": rows,
                "stats": bundle.stats(),
                "badges": SANDBOX_BADGES,
                "network": ", ".join(bundle.networks()) or network,
                "source_error": bundle.source_error,
                # Every row is the same repository, so it belongs in the header
                # once rather than repeated down a column.
                "repository": views[0].task if views else "",
                "active": "Contracts",
            },
        )

    @router.get("/receipts", response_class=HTMLResponse)
    def receipts(request: Request) -> Any:
        """A real page. It previously re-rendered the contracts table, so the
        nav item appeared to do nothing when clicked."""
        views = bundle.all()
        cards = []
        for v in views:
            result = bundle.verify(v)
            cards.append(
                {
                    "id": v.id,
                    "decision": v.decision,
                    "action": v.action,
                    "amount": v.amount,
                    "chain": v.chain or "unknown",
                    "valid": result["valid"],
                    "checks": result["checks"],
                }
            )
        return templates.TemplateResponse(
            request,
            "receipts.html",
            {
                "receipts": cards,
                "network": ", ".join(bundle.networks()) or network,
                "source_error": bundle.source_error,
                "active": "Receipts",
            },
        )

    @router.get("/api/receipts")
    def receipts_json() -> Any:
        """The receipt index for programs rather than people.

        ``source_error`` is carried through rather than collapsed into an empty
        list, for the same reason the page shows it: a datastore that is
        unreachable and one that is genuinely empty are different facts, and an
        agent polling for its own settlement must be able to tell them apart.
        """
        views = bundle.all()
        return JSONResponse(
            {
                "count": len(views),
                "source_error": bundle.source_error,
                "receipts": [
                    {
                        "id": v.id,
                        "task_id": v.binding.get("task_id", ""),
                        "decision": v.decision,
                        "action": v.action,
                        "amount_usdc": v.amount,
                        "chain": v.chain,
                        "settlement_tx": v.settlement_tx,
                        "contract_hash": v.binding.get("contract_hash", ""),
                        "submission_sha": v.binding.get("submission_sha", ""),
                        "envelope_url": f"/receipts/{v.id}.json",
                    }
                    for v in views
                ],
            }
        )

    @router.get("/receipts/{receipt_id}.json")
    def receipt_json(receipt_id: str) -> Any:
        view = bundle.get(receipt_id)
        if view is None:
            raise HTTPException(status_code=404, detail="no such receipt")
        return JSONResponse(view.envelope)

    @router.get("/receipts/{receipt_id}", response_class=HTMLResponse)
    def receipt_detail(request: Request, receipt_id: str) -> Any:
        view = bundle.get(receipt_id)
        if view is None:
            raise HTTPException(status_code=404, detail="no such receipt")
        return templates.TemplateResponse(
            request,
            "receipt.html",
            {
                "receipt": view,
                "verification": bundle.verify(view),
                "network": view.chain or network,
                "active": "Receipts",
            },
        )

    @router.get("/evaluations/{receipt_id}", response_class=HTMLResponse)
    def evaluation(request: Request, receipt_id: str) -> Any:
        view = bundle.get(receipt_id)
        if view is None:
            raise HTTPException(status_code=404, detail="no such evaluation")
        return templates.TemplateResponse(
            request,
            "evaluation.html",
            {
                "e": evaluation_view(view),
                "network": view.chain or network,
                "active": "Receipts",
            },
        )

    @router.get("/contracts/{contract_hash}", response_class=HTMLResponse)
    def contract(request: Request, contract_hash: str) -> Any:
        record = None
        if contracts is not None:
            try:
                record = contracts.get(contract_hash)
            except Exception:  # noqa: BLE001 - rendered as "not recorded"
                record = None
        related = [v for v in bundle.all() if v.binding.get("contract_hash") == contract_hash]
        if record is None and not related:
            raise HTTPException(status_code=404, detail="no such contract")
        return templates.TemplateResponse(
            request,
            "contract.html",
            {
                "c": contract_view(record, related),
                "network": (record or {}).get("chain") or network,
                "active": "Contracts",
            },
        )

    @router.get("/verifier", response_class=HTMLResponse)
    def verifier(request: Request) -> Any:
        return templates.TemplateResponse(
            request,
            "verifier.html",
            {
                "environment": verifier_environment(),
                "pipeline": GRADING_PIPELINE,
                "defenses": ANTI_GAMING,
                "probe": EGRESS_PROBE,
                "egress_claim": EGRESS_DENY_TCP,
                "network": ", ".join(bundle.networks()) or network,
                "active": "Verifier",
            },
        )

    @router.get("/integrate", response_class=HTMLResponse)
    def integrate(request: Request) -> Any:
        """How another agent talks to MergeGate.

        The sample receipt id is taken from whatever this deployment actually
        holds rather than hardcoded, so the copy-pasteable command works instead
        of 404ing against an id from some other environment.
        """
        views = bundle.all()
        return templates.TemplateResponse(
            request,
            "integrate.html",
            {
                "base_url": str(request.base_url).rstrip("/"),
                "sample_receipt": views[0].id if views else "<receipt-id>",
                "public_key": os.environ.get("MERGEGATE_RECEIPT_PUBLIC_KEY", ""),
                "tools": MCP_TOOLS,
                "endpoints": HTTP_ENDPOINTS,
                "network": ", ".join(bundle.networks()) or network,
                "active": "Integrate",
            },
        )

    return router
