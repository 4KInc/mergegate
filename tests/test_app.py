"""HTTP-layer tests for the deployed service.

These exist because of a bug the unit tests could not have caught.
``tests/test_webhook.py`` calls :class:`~mergegate.webhook.WebhookReceiver`
directly, so it never crossed the FastAPI boundary, and the endpoint was
entirely broken there: ``Request`` was imported inside ``build_router`` while
the module uses postponed annotations, so FastAPI could not resolve the
annotation, treated ``request`` as a query parameter, and returned 422 for
every delivery without ever running signature verification.

It failed closed, so nothing was mis-authenticated. But a webhook that rejects
GitHub is a webhook that never fires, and only a request through the app would
have shown it. Anything that can break between the handler and the transport
gets asserted here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mergegate.app import create_app
from mergegate.settlement import TaskState
from mergegate.store import MemoryTaskStore
from mergegate.webhook import GitHubPush, WebhookReceiver

from .test_store import _machine  # the same funded task fixture

SECRET = "test-webhook-secret"
REPO = "4KInc/mergegate-demo-task"
SHA = "1" * 40


def _client(store: MemoryTaskStore | None = None) -> TestClient:
    store = store or MemoryTaskStore()

    def resolve(push: GitHubPush) -> Any:
        return store.get(push.repository)

    receiver = WebhookReceiver(secret=SECRET, repository=REPO, resolve=resolve)
    return TestClient(create_app(receiver=receiver))


def _push_body(sha: str = SHA, repo: str = REPO) -> bytes:
    return json.dumps(
        {
            "repository": {"full_name": repo},
            "after": sha,
            "before": "0" * 40,
            "ref": "refs/heads/main",
            "deleted": False,
            "forced": False,
        }
    ).encode()


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_health() -> None:
    assert _client().get("/health").json() == {"status": "ok"}


def test_status_states_scope_and_custody() -> None:
    """The service's own description must carry the same limits the receipts do."""
    body = _client().get("/api/status").json()
    assert "not code quality, security, or mergeworthiness" in body["scope"]
    assert "escrow authority" in body["custody"]
    assert "non-custodial" not in json.dumps(body).lower()


# -- the regression that motivated this file ---------------------------------


def test_endpoint_actually_runs_the_handler() -> None:
    """A 422 here means FastAPI never reached the handler: the original bug."""
    r = _client().post(
        "/webhooks/github",
        content=_push_body(),
        headers={"X-GitHub-Event": "push", "X-GitHub-Delivery": "d1"},
    )
    assert r.status_code != 422, "FastAPI did not route the request body to the handler"
    assert r.status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {"X-GitHub-Event": "push", "X-GitHub-Delivery": "d1"},
        {"X-GitHub-Event": "push", "X-GitHub-Delivery": "d1", "X-Hub-Signature-256": "sha256=bad"},
        {"X-GitHub-Event": "push", "X-GitHub-Delivery": "d1", "X-Hub-Signature-256": "nonsense"},
    ],
)
def test_unauthenticated_deliveries_are_rejected(headers: dict[str, str]) -> None:
    r = _client().post("/webhooks/github", content=_push_body(), headers=headers)
    assert r.status_code == 401


def test_rejection_body_is_not_an_oracle() -> None:
    """A missing signature and a wrong one must look identical to the caller."""
    client = _client()
    missing = client.post(
        "/webhooks/github",
        content=_push_body(),
        headers={"X-GitHub-Event": "push", "X-GitHub-Delivery": "d1"},
    )
    wrong = client.post(
        "/webhooks/github",
        content=_push_body(),
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "d2",
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
        },
    )
    assert missing.status_code == wrong.status_code == 401
    assert missing.json() == wrong.json() == {"error": "rejected"}


def test_signature_is_checked_against_raw_bytes() -> None:
    """Re-serializing before verifying would compare against bytes GitHub never
    sent. Same JSON, different byte order: the signature must still hold."""
    body = (
        b'{"repository": {"full_name": "' + REPO.encode() + b'"}, '
        b'"after": "' + SHA.encode() + b'", "ref": "refs/heads/main"}'
    )
    r = _client().post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "d1",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert r.status_code == 202


def test_authentic_push_reaches_the_state_machine() -> None:
    store = MemoryTaskStore()
    machine = _machine()
    machine.task_id = REPO
    store.put(machine)

    body = _push_body()
    r = _client(store).post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "d1",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert r.status_code == 202
    assert r.json()["disposition"] == "processed"
    assert store.get(REPO).state is TaskState.SUBMITTED  # type: ignore[union-attr]


def test_redelivery_is_deduplicated_over_http() -> None:
    """GitHub redelivers. Twice through the transport must settle one event."""
    store = MemoryTaskStore()
    machine = _machine()
    machine.task_id = REPO
    store.put(machine)
    client = _client(store)

    body = _push_body()
    headers = {
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": "same-delivery",
        "X-Hub-Signature-256": _sign(body),
    }
    first = client.post("/webhooks/github", content=body, headers=headers)
    second = client.post("/webhooks/github", content=body, headers=headers)

    assert first.json()["outcome"] == "applied"
    assert second.json()["outcome"] == "duplicate"


def test_ping_is_acknowledged() -> None:
    """GitHub sends ping when the hook is created; rejecting it shows a red X."""
    body = b'{"zen": "hello"}'
    r = _client().post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": "d1",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert r.status_code == 202
    assert r.json()["disposition"] == "ignored"


def test_delivery_for_another_repository_is_refused() -> None:
    body = _push_body(repo="someone/else")
    r = _client().post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "d1",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert r.status_code == 400


# -- the OpenAPI spec, as a download ------------------------------------------
#
# The spec is the one artefact whose entire job is to describe the service, so
# it is generated from the running app rather than committed. A checked-in copy
# is wrong the first time a route changes, and wrong silently: nothing fails,
# the file just stops being true.


def test_the_yaml_spec_is_served_as_a_named_download() -> None:
    response = _client().get("/openapi.yaml")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/yaml")
    # Named, because it lands in a downloads folder beside everyone else's
    # openapi.yaml.
    assert "mergegate-openapi.yaml" in response.headers["content-disposition"]
    assert response.headers["content-disposition"].startswith("attachment")


def test_the_yaml_spec_is_the_same_document_as_the_json_one() -> None:
    """The two cannot disagree, and this is what makes that true.

    Both come from ``app.openapi()``. If someone later "helpfully" serves a
    static file from one of them, this fails.
    """
    import yaml

    client = _client()
    from_yaml = yaml.safe_load(client.get("/openapi.yaml").text)
    from_json = client.get("/openapi.json").json()

    assert from_yaml == from_json


def test_a_new_route_appears_in_the_yaml_without_anyone_updating_it() -> None:
    """Drift is impossible by construction, proven rather than asserted.

    A committed spec would pass every other test here while describing a
    service that no longer exists. Adding a route and finding it in the YAML is
    the only check that distinguishes generated from transcribed.
    """
    import yaml

    app = create_app(
        store=MemoryTaskStore(),
        receiver=WebhookReceiver(secret=SECRET, repository=REPO, resolve=lambda push: None),
    )

    def added() -> dict[str, str]:
        return {}

    app.get("/a-route-added-after-the-app-was-built")(added)

    document = yaml.safe_load(TestClient(app).get("/openapi.yaml").text)
    assert "/a-route-added-after-the-app-was-built" in document["paths"]


def test_the_spec_parses_as_openapi_3() -> None:
    """A YAML file that parses but is not a spec would still fail a directory."""
    import yaml

    document = yaml.safe_load(_client().get("/openapi.yaml").text)

    assert document["openapi"].startswith("3.")
    assert document["info"]["title"] == "MergeGate"
    assert document["paths"], "a spec with no paths describes nothing"


def test_the_docs_page_links_the_download() -> None:
    """The button has to point at the route that exists.

    Checked through the rendered page rather than by reading the template,
    because a template that renders a broken href still renders.
    """
    html = _client().get("/docs").text

    assert "/openapi.yaml" in html
    assert "Download OpenAPI" in html
