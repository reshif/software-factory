"""The FastAPI app: `/webhooks/github`, `/inbox`, `/healthz`, `/metrics` (build spec §3 B7).

The webhook handler is intentionally thin: verify the signature, parse the
event, and enqueue it (`Factory.enqueue_webhook`) -- a fast, synchronous,
in-memory operation. It never runs an agent, touches the sandbox or calls
GitHub itself; the worker loop (`worker.py`) drains the queue and does that
work off the request path (see the fix-wave note this build incorporated:
`ClaudeRuntime` will refuse to run synchronously inside a running event loop,
and even before that change, an agent run has no place blocking an inbound
webhook request).

The inbox router (`factory.inbox.router.build_inbox_router`) is B4's; its
POST handler is a plain `def`, so FastAPI already runs it in a worker thread,
off the event loop -- safe for `Factory.decide`, which never itself calls the
agent runtime (only GitHub/deploy calls, which are fine there).
"""
from __future__ import annotations

import json
import logging

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse

from .clock import SystemClock
from .config import Settings
from .github.webhooks import (CheckSuiteCompleted, IssueCommentCreated, IssueLabeled, PullRequestReview,
                              PushToDefault, verify_signature)
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
}


def create_app(settings: Settings, *, factory: Factory | None = None) -> FastAPI:
    factory = factory or build_factory(settings)
    app = FastAPI(title="factory-controller")

    @app.post("/webhooks/github")
    async def webhook_github(request: Request, x_github_event: str = Header(default=""),
                             x_hub_signature_256: str = Header(default="", alias="X-Hub-Signature-256")) -> Response:
        body = await request.body()
        if not verify_signature(body, x_hub_signature_256, settings.github_webhook_secret or ""):
            return JSONResponse(status_code=401, content={"detail": "invalid signature"})
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"detail": "malformed JSON body"})
        event = gh_webhooks.parse_event(x_github_event, payload)
        if event is None:
            return Response(status_code=204)
        kind = _EVENT_KIND.get(type(event))
        if kind is None:  # e.g. IssueCommentCreated: parsed, but nothing to act on in v1
            return Response(status_code=204)
        factory.enqueue_webhook(kind, event)
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
