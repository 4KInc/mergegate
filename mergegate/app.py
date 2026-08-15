"""The MergeGate API service.

Deployed to Cloud Run. This is the only component with outbound network access -
it has to reach Circle to settle and GitHub to read submissions. The verifier
job runs sealed on a separate VPC with no TCP egress; conflating the two would
either break settlement or unseal the sandbox.

The service is intentionally thin. It authenticates deliveries, hands them to
the settlement state machine under a Firestore transaction, and reports what
came back. It makes no decision about money: that was made by the buyer's
mandate at funding time.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import Request

from .settlement import TaskStateMachine
from .webhook import GitHubPush, WebhookReceiver

__all__ = ["create_app", "config_from_env"]


def config_from_env() -> dict[str, str]:
    """Read deployment configuration, failing loudly on anything missing.

    A webhook secret that silently defaults to empty would make every delivery
    verify against a digest an attacker can compute, so absence is fatal rather
    than defaulted.
    """
    required = ("GITHUB_WEBHOOK_SECRET", "DEMO_REPO")
    config = {key: os.environ.get(key, "") for key in required}
    missing = [key for key, value in config.items() if not value]
    if missing:
        raise RuntimeError(
            f"missing required configuration: {', '.join(missing)}. "
            "The service refuses to start without them rather than run in a "
            "state where deliveries cannot be authenticated."
        )
    return config


def _dashboard_public_key() -> Any:
    """Public half of the receipt signing key, for verifying on the dashboard.

    Returns None when unavailable rather than failing to boot: the dashboard
    then says it cannot verify, which is honest, instead of showing a green
    check it did not earn.
    """
    import base64

    raw = os.environ.get("MERGEGATE_RECEIPT_PUBLIC_KEY", "")
    if not raw:
        return None
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        padded = raw + "=" * (-len(raw) % 4)
        return Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(padded))
    except Exception:
        return None


def _contract_store() -> Any:
    """Funded contracts, for the contract page. None locally."""
    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        return None
    from .store import FirestoreContractStore

    return FirestoreContractStore()


def _advisory_store() -> Any:
    """Gemini's screening and forensics reports, for the evaluation page.

    Its own collection, never the receipt document. See store.AdvisoryStore for
    why that separation is load-bearing rather than tidiness.
    """
    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        return None
    from .store import FirestoreAdvisoryStore

    return FirestoreAdvisoryStore()


def _receipt_source() -> Any:
    """Where the dashboard reads receipts from.

     Firestore in deployment, so a settlement appears without a redeploy. Falls
     back to the bundle shipped in the image only when no project is configured
    : a local run. It does **not** fall back when Firestore is configured but
     unreachable: stale shipped receipts presented as live state would be a
     quiet lie, so the page reports the failure instead.
    """
    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        return None
    from .store import FirestoreReceiptStore

    return FirestoreReceiptStore()


def create_app(store: Any = None, receiver: WebhookReceiver | None = None) -> Any:
    """Build the FastAPI app.

    ``store`` and ``receiver`` are injectable so the app can be exercised
    without Firestore or a real secret.
    """
    from fastapi import FastAPI

    app = FastAPI(
        title="MergeGate",
        description=(
            "Deterministic evaluator and conditional USDC settlement for "
            "autonomous coding agents. Attests verified contract acceptance "
            "only, not code quality, security, or mergeworthiness."
        ),
    )

    if receiver is None:
        config = config_from_env()
        if store is None:
            from .store import FirestoreTaskStore

            store = FirestoreTaskStore()

        def resolve(push: GitHubPush) -> TaskStateMachine | None:
            # The webhook module is storage-agnostic on purpose; this is where
            # a push becomes the task it concerns. Events are applied through
            # store.apply so the read-modify-write is transactional.
            machine: TaskStateMachine | None = store.get(push.repository)
            return machine

        receiver = WebhookReceiver(
            secret=config["GITHUB_WEBHOOK_SECRET"],
            repository=config["DEMO_REPO"],
            resolve=resolve,
        )

    from .webhook import build_router

    app.include_router(build_router(receiver))

    # The dashboard is mounted last so its "/" does not shadow the API routes
    # above. Receipts are read from the bundle shipped in the image and
    # re-verified on each request against the published signing key.
    from .web import ReceiptBundle, build_web_router

    app.include_router(
        build_web_router(
            ReceiptBundle(public_key=_dashboard_public_key(), source=_receipt_source()),
            network=os.environ.get("MERGEGATE_NETWORK", "Base mainnet"),
            contracts=_contract_store(),
            advisory=_advisory_store(),
        )
    )

    # Deliberately /health, not /healthz. The deployed service showed /healthz
    # returning a generic HTML 404 with no "Server: Google Frontend" header
    # while every other path carried one: something upstream intercepts that
    # exact path and the request never reached the container. The route was
    # registered (it appeared in the served OpenAPI spec) and worked locally,
    # so this is infrastructure, not the app. Renaming is cheaper than
    # arguing with a middlebox.
    # x402: the verifier priced as a service. Serves the challenge; see
    # mergegate/x402.py for what it deliberately does not yet claim.
    @app.get("/x402/verify")
    def x402_verify(request: Request) -> Any:
        from fastapi.responses import JSONResponse

        from .x402 import X402Price, payment_requirements

        price = X402Price(
            pay_to=os.environ.get("VERIFIER_FEE_WALLET_ADDRESS", ""),
            asset=os.environ.get(
                "USDC_CONTRACT_ADDRESS", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
            ),
            amount_usdc=os.environ.get("VERIFIER_FEE_USDC", "0.05"),
        )
        if not price.pay_to:
            return JSONResponse({"error": "verifier fee wallet not configured"}, status_code=503)

        body = payment_requirements(price, resource=str(request.url))
        header = request.headers.get("X-PAYMENT")
        if not header:
            return JSONResponse(body, status_code=402)

        # A payment was presented. Verify it, then settle it. These are separate
        # outcomes: verification is pure computation, settlement needs a relayer
        # holding gas, and a caller told "paid" when nothing moved is worse off
        # than one told "verified but not settled".
        import time

        from .x402_settle import decode_payment, settle_payment, verify_payment

        payload = decode_payment(header)
        if payload is None:
            return JSONResponse(
                {**body, "error": "X-PAYMENT is not a decodable x402 payload"},
                status_code=402,
            )

        outcome = verify_payment(payload, price, now=int(time.time()))
        if not outcome.valid:
            return JSONResponse(
                {
                    **body,
                    "error": "payment verification failed",
                    "detail": outcome.reason,
                    "checks": {name: ok for name, ok in outcome.checks},
                },
                status_code=402,
            )

        tx_hash, error = settle_payment(payload, price)
        if error:
            # Verified but unsettled. Answering 200 here would claim a fee that
            # never moved, so the caller is told exactly where it stopped.
            return JSONResponse(
                {
                    **body,
                    "error": "payment verified but not settled",
                    "detail": error,
                    "verified": True,
                    "payer": outcome.payer,
                    "checks": {name: ok for name, ok in outcome.checks},
                },
                status_code=402,
            )

        return JSONResponse(
            {
                "verified": True,
                "settled": True,
                "payer": outcome.payer,
                "transaction": tx_hash,
                "amount_usdc": price.amount_usdc,
                "network": price.network,
            },
            status_code=200,
            headers={"X-PAYMENT-RESPONSE": tx_hash},
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        """Status, stated in the same terms the receipts use."""
        return {
            "service": "mergegate",
            "scope": (
                "Verified contract acceptance, not code quality, security, or mergeworthiness."
            ),
            "custody": (
                "Programmable USDC escrow with policy-bound conditional "
                "settlement. MergeGate holds escrow authority."
            ),
            "webhook": "/webhooks/github",
            "verification_key": "/api/verification-key",
            "integrate": "/integrate",
        }

    @app.get("/api/verification-key")
    def verification_key() -> Any:
        """Publish the public half of the receipt signing key.

        Receipts claimed to be independently verifiable while the key needed to
        verify them existed only in the deployment's environment, which made the
        claim unexercisable by anyone outside it. Publishing the *public* half is
        the entire point of signing.

        The caveat is served with it rather than left implicit: a key taken from
        the issuer proves the issuer is self-consistent, not that it is honest.
        Pin it out of band to get the guarantee the signature is meant to carry.
        """
        from fastapi.responses import JSONResponse

        raw = os.environ.get("MERGEGATE_RECEIPT_PUBLIC_KEY", "")
        if not raw:
            return JSONResponse(
                {"error": "this deployment publishes no verification key"}, status_code=503
            )
        return {
            "alg": "Ed25519",
            "encoding": "base64url, unpadded",
            "public_key": raw,
            "kid": os.environ.get("MERGEGATE_RECEIPT_KEY_ID", "mergegate-e5683130"),
            "caveat": (
                "Fetching this from the issuer proves self-consistency, not "
                "authenticity. Pin it out of band."
            ),
            # Not "pip install mergegate": it is not on PyPI, and a plain wheel
            # would omit the shared engine the verifier needs.
            "verify_with": (
                "git clone --recurse-submodules https://github.com/4KInc/mergegate.git"
                " && pip install -e . && mergegate verify <receipt>.json"
            ),
        }

    return app


app = None
if os.environ.get("MERGEGATE_EAGER_APP") == "1":  # pragma: no cover - deploy path
    app = create_app()
