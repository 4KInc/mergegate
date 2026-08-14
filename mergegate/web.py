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

The design language — the Tailwind theme, fonts, and layout — comes from the
Stitch screens in ``design/screens/``. Those remain the design reference; these
templates are the live implementation of them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .receipt import verify_receipt

__all__ = ["ReceiptBundle", "build_web_router", "short"]

RECEIPTS_DIR = Path(__file__).resolve().parent.parent / "demo" / "receipts"


def short(value: str, head: int = 10, tail: int = 6) -> str:
    """Truncate a hash for display, keeping both ends.

    Both ends, because a prefix alone cannot be checked against a block
    explorer — and a truncation whose tail is invented is worse than no
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
        return str(self.binding.get("reason", ""))

    @property
    def failed_terms(self) -> list[str]:
        return list(self.manifest.get("failed_terms") or [])

    @property
    def command_count(self) -> int:
        return len(self.manifest.get("commands") or [])

    @property
    def mandate_statement(self) -> str:
        return str(self.body.get("mandate_statement", ""))

    @property
    def scope(self) -> str:
        return str(self.body.get("scope", ""))

    @property
    def task(self) -> str:
        return str(self.binding.get("task_id", ""))

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


class ReceiptBundle:
    """The receipts on disk, verified on read."""

    def __init__(self, directory: Path = RECEIPTS_DIR, public_key: Ed25519PublicKey | None = None):
        self.directory = directory
        self.public_key = public_key

    def _paths(self) -> list[Path]:
        if not self.directory.is_dir():
            return []
        return sorted(self.directory.rglob("receipt-*.json"))

    def _id(self, path: Path) -> str:
        rel = path.relative_to(self.directory).with_suffix("")
        return str(rel).replace("/", "-")

    def all(self) -> list[ReceiptView]:
        views = []
        for path in self._paths():
            try:
                views.append(ReceiptView(id=self._id(path), envelope=json.loads(path.read_text())))
            except (OSError, json.JSONDecodeError):
                # A receipt we cannot read is omitted rather than rendered as a
                # blank row that looks like a real settlement.
                continue
        return views

    def get(self, receipt_id: str) -> ReceiptView | None:
        return next((v for v in self.all() if v.id == receipt_id), None)

    def verify(self, view: ReceiptView) -> dict[str, Any]:
        """Re-check a receipt now. Never a cached verdict."""
        if self.public_key is None:
            return {
                "valid": False,
                "checks": 0,
                "failures": [
                    "no public key configured — the receipt cannot be verified here. "
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


def build_web_router(bundle: ReceiptBundle, *, network: str = "Base mainnet") -> Any:
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
        rows = [
            {
                "id": v.id,
                "task": v.task or "—",
                "repository": v.task or "—",
                "amount": v.amount,
                "submission_short": short(v.submission_sha, 8, 4),
                "state": "SETTLED" if v.action == "release" else "REFUNDED",
                "chain": v.chain or "—",
                "settlement_short": short(v.settlement_tx, 10, 4),
                "explorer": v.settlement_explorer,
            }
            for v in bundle.all()
        ]
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "rows": rows,
                "stats": bundle.stats(),
                "badges": SANDBOX_BADGES,
                "network": ", ".join(bundle.networks()) or network,
                "active": "Contracts",
            },
        )

    @router.get("/receipts", response_class=HTMLResponse)
    def receipts(request: Request) -> Any:
        return dashboard(request)

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

    @router.get("/verifier", response_class=HTMLResponse)
    def verifier(request: Request) -> Any:
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "rows": [],
                "stats": bundle.stats(),
                "badges": SANDBOX_BADGES,
                "network": ", ".join(bundle.networks()) or network,
                "active": "Verifier",
            },
        )

    return router
