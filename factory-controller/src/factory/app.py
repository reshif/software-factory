"""The FastAPI app: `/webhooks/github`, `/inbox`, `/healthz`, `/metrics` (build spec §3 B7).

The webhook handler is intentionally thin and returns fast: verify the
signature, require GitHub's own delivery id (`X-GitHub-Delivery`), parse just
enough of the payload to know whether it's an event the pipeline acts on at
all, and hand it to `Factory.enqueue_webhook(delivery_id, event, payload)` --
a **durable, deduplicated** store write, not an in-memory queue (a redelivery
of the same `X-GitHub-Delivery` id, which GitHub does on its own retries, must
enqueue at most once). It never runs an agent, touches the sandbox or calls
GitHub itself; the worker loop (`worker.py`) drains the store
(`Factory.dispatch_webhooks`) and does that work off the request path (see the
fix-wave note this build incorporated: `ClaudeRuntime` will refuse to run
synchronously inside a running event loop, and even before that change, an
agent run has no place blocking an inbound webhook request).

`event`/`payload` are passed to `enqueue_webhook` as a plain kind string and
a JSON-serializable dict (never the `github.webhooks` dataclasses) precisely
because the queue is durable: whatever gets written there must survive a
round trip through the state store (Postgres in production), and
`worker.dispatch_webhooks` is the one place that re-derives a typed event
from them (`github.webhooks.parse_event`).

A payload GitHub signed but that doesn't match the shape its own
`X-GitHub-Event` header promises (a field `github.webhooks.parse_event`
expects is missing or the wrong type) is a **malformed delivery, not a server
error**: `parse_event` raising `KeyError`/`TypeError`/`AttributeError`/
`ValueError` while picking the payload apart is reported to GitHub as 400, not
500 -- fail closed on ambiguous input rather than enqueue something the
worker can't parse either.

The inbox router (`factory.inbox.router.build_inbox_router`) is B4's; its
POST handler is a plain `def`, so FastAPI already runs it in a worker thread,
off the event loop -- safe for `Factory.decide`, which never itself calls the
agent runtime (only GitHub/deploy calls, which are fine there).
"""
from __future__ import annotations

import dataclasses
import json
import logging

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse

from .clock import SystemClock
from .config import Settings
from .github.webhooks import (CheckSuiteCompleted, IssueCommentCreated, IssueLabeled, PullRequestReview,
                              PushToBranch, PushToDefault, verify_signature)
from .github import webhooks as gh_webhooks
from .inbox.router import build_inbox_router
from .inbox.signing import TokenSigner
from .pipeline.orchestrator import Factory
from .telemetry import metrics as telemetry
from .wiring import build_factory

logger = logging.getLogger(__name__)

_EVENT_KIND = {
    IssueLabeled: "issue_labeled",
    PullRequestReview: "pull_request_review",
    CheckSuiteCompleted: "check_suite_completed",
    PushToDefault: "push_to_default",
    PushToBranch: "push_to_branch",
}

# `parse_event` deliberately isn't defensive about a recognized event+action
# whose payload is missing/mis-shaped fields -- that's exactly what makes a
# malformed delivery detectable here as a 400 rather than silently enqueueing
# something `dispatch_webhooks` would fail to parse later, off the request path
# where GitHub can no longer be told to retry with a corrected delivery.
_MALFORMED_PAYLOAD_ERRORS = (KeyError, TypeError, AttributeError, ValueError)


def create_app(settings: Settings, *, factory: Factory | None = None) -> FastAPI:
    factory = factory or build_factory(settings)
    app = FastAPI(title="factory-controller")

    @app.post("/webhooks/github")
    async def webhook_github(request: Request, x_github_event: str = Header(default=""),
                             x_hub_signature_256: str = Header(default="", alias="X-Hub-Signature-256"),
                             x_github_delivery: str = Header(default="", alias="X-GitHub-Delivery")) -> Response:
        body = await request.body()
        if not verify_signature(body, x_hub_signature_256, settings.github_webhook_secret or ""):
            return JSONResponse(status_code=401, content={"detail": "invalid signature"})
        if not x_github_delivery:
            return JSONResponse(status_code=400, content={"detail": "missing X-GitHub-Delivery"})
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"detail": "malformed JSON body"})
        try:
            event = gh_webhooks.parse_event(x_github_event, payload)
        except _MALFORMED_PAYLOAD_ERRORS as exc:
            logger.info("delivery %s: malformed %s payload: %s", x_github_delivery, x_github_event, exc)
            return JSONResponse(status_code=400, content={"detail": "malformed webhook payload"})
        if event is None:
            return Response(status_code=204)
        kind = _EVENT_KIND.get(type(event))
        if kind is None:  # e.g. IssueCommentCreated: parsed, but nothing to act on in v1
            return Response(status_code=204)
        # A dict of the already-validated, typed event -- not the raw GitHub
        # payload -- so a redelivery that enqueues the identical `delivery_id`
        # twice (durable dedup is `Factory.enqueue_webhook`'s job, not ours) is
        # comparing apples to apples, and so `dispatch_webhooks` never has to
        # re-derive `kind` from a raw payload it would need `x_github_event` for.
        factory.enqueue_webhook(x_github_delivery, kind, dataclasses.asdict(event))
        return Response(status_code=204)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/metrics")
    def metrics_endpoint() -> Response:
        mission_ids = [m.mission_id for m in factory.store.list_missions()]
        report = telemetry.from_store(factory.store, mission_ids)
        return Response(content=telemetry.render_prometheus(report), media_type="text/plain; version=0.0.4")

    if settings.inbox_signing_key:
        signer = TokenSigner(settings.inbox_signing_key)
        app.include_router(build_inbox_router(factory.store, factory.decide, signer, SystemClock()))
    else:
        logger.warning("FACTORY_INBOX_SIGNING_KEY is unset; /inbox is not mounted")

    app.state.factory = factory
    return app


__all__ = ["create_app"]
