"""The approval inbox: a small, server-rendered FastAPI router (final draft §10).

This router owns exactly one job: show a human everything a decision needs (§10
"what the decision packet contains") and record their decision. It never talks to
GitHub, the runtime, the sandbox or the pipeline directly — it only reads through
`StateStore` and calls the injected `decide` callable, so it can be tested and run
without any of those adapters existing yet.

Security notes (final draft §13.1 #6, #8):
  - Every untrusted string (issue/PR text, agent output, evidence detail) is HTML
    escaped before it reaches a template. Nothing here is ever evaluated as code,
    and no inline JavaScript is used or needed.
  - The decision form is plain HTML, POST-only, with the bearer token carried as a
    hidden form field (never a header or cookie). An attacker's page cannot forge a
    valid submission because it cannot read the token out of a page served from a
    different origin — there is nothing here for a blind cross-site POST to replay.
  - Tokens are verified with `TokenSigner` (constant-time compare, explicit expiry)
    before anything in the store is read or changed.
"""
from __future__ import annotations

import html
import logging
from typing import Callable

from fastapi import APIRouter, Form, HTTPException, Query
from fastapi.responses import HTMLResponse

from ..controller.approvals import (AlreadyConsumed, ApprovalError, Expired, IneligibleApprover,
                                    QuorumNotMet, StaleApproval, Voided)
from ..models import DecisionPacket, EvidenceBundle
from ..ports import Clock, NotFound, StateStore
from .signing import InvalidToken, TokenExpired, TokenPayload, TokenSigner

logger = logging.getLogger(__name__)

DecideCallable = Callable[[str, str, "frozenset | tuple", str, str], object]

DECISIONS = ("approve", "revise", "defer", "cancel")

# ApprovalError subclasses -> HTTP status. Order matters: checked most-specific first
# via isinstance, but these classes don't nest so a dict lookup by exact type is enough.
_ERROR_STATUS: dict[type, int] = {
    Expired: 410,           # the operation had to start before `expires`; it's gone
    AlreadyConsumed: 409,   # someone already executed on this approval
    Voided: 409,            # a revise/cancel already voided this round
    StaleApproval: 409,     # wrong content hash, or state/policy moved since the request
    QuorumNotMet: 409,      # not enough approvals yet to act (shouldn't normally hit /decision)
    IneligibleApprover: 403,  # editor-of-record or requester-under-regulated-profile
}


def _status_for(exc: ApprovalError) -> int:
    for cls, status in _ERROR_STATUS.items():
        if isinstance(exc, cls):
            return status
    return 400


def _escape(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def build_inbox_router(store: StateStore, decide: DecideCallable, signer: TokenSigner, clock: Clock) -> APIRouter:
    """Build the `/inbox` routes.

    `decide` is called as `decide(request_id, approver, roles, decision, content_hash)`
    and is expected to raise an `ApprovalError` subclass on failure (typically a thin
    wrapper around `store.approvals.decide` plus any pipeline side effects — voiding
    notifications, waking dependents, etc. — which live in the orchestrator, not here).
    """
    router = APIRouter()

    def _verify(token: str, request_id: str | None = None) -> TokenPayload:
        try:
            payload = signer.verify(token, now=clock.now())
        except TokenExpired as exc:
            raise HTTPException(status_code=410, detail="token expired") from exc
        except InvalidToken as exc:
            raise HTTPException(status_code=403, detail="invalid token") from exc
        if request_id is not None and payload.request_id is not None and payload.request_id != request_id:
            raise HTTPException(status_code=403, detail="token is not valid for this request")
        return payload

    def _load_packet(request_id: str) -> DecisionPacket:
        try:
            return store.get_packet(request_id)
        except (KeyError, NotFound) as exc:
            raise HTTPException(status_code=404, detail="no such approval request") from exc

    @router.get("/inbox", response_class=HTMLResponse)
    def list_inbox(token: str = Query(...)) -> str:
        payload = _verify(token)
        open_requests = store.approvals.list_open()
        if payload.request_id is not None:
            open_requests = [r for r in open_requests if r.request_id == payload.request_id]
        packets = []
        for req in open_requests:
            try:
                packets.append(store.get_packet(req.request_id))
            except (KeyError, NotFound):
                continue  # no packet built yet for this request; nothing to show
        return _render_list(packets, token=token, approver=payload.approver)

    @router.get("/inbox/{request_id}", response_class=HTMLResponse)
    def get_packet_page(request_id: str, token: str = Query(...)) -> str:
        payload = _verify(token, request_id)
        packet = _load_packet(request_id)
        return _render_packet(packet, token=token, approver=payload.approver)

    @router.post("/inbox/{request_id}/decision", response_class=HTMLResponse)
    def post_decision(request_id: str, token: str = Form(...), decision: str = Form(...),
                      content_hash: str = Form(...)) -> HTMLResponse:
        payload = _verify(token, request_id)
        if decision not in DECISIONS:
            raise HTTPException(status_code=400, detail=f"unknown decision {decision!r}")
        try:
            decide(request_id, payload.approver, payload.roles, decision, content_hash)
        except ApprovalError as exc:
            raise HTTPException(status_code=_status_for(exc), detail=str(exc)) from exc
        except (KeyError, NotFound) as exc:
            raise HTTPException(status_code=404, detail="no such approval request") from exc
        logger.info("approval %s: %s recorded %s", request_id, payload.approver, decision)
        return HTMLResponse(_render_ack(request_id, payload.approver, decision))

    return router


# ── Rendering (server-side only; no client-side script) ─────────────────────────────────

_STYLE = """
body { font-family: -apple-system, Helvetica, Arial, sans-serif; margin: 2rem; color: #1a1a1a; }
pre { background: #f4f4f4; padding: 1rem; overflow-x: auto; white-space: pre-wrap; word-break: break-word; }
table { border-collapse: collapse; margin: 0.5rem 0; }
td, th { border: 1px solid #ccc; padding: 0.25rem 0.5rem; text-align: left; }
.decision-form button { margin-right: 0.5rem; padding: 0.4rem 1rem; }
.warn { color: #946200; }
"""


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<title>{_escape(title)}</title><style>{_STYLE}</style></head>"
            f"<body>{body}</body></html>")


def _render_list(packets: list[DecisionPacket], *, token: str, approver: str) -> str:
    if not packets:
        body = f"<h1>Inbox</h1><p>No open approvals for {_escape(approver)}.</p>"
        return _page("Inbox", body)
    rows = "".join(
        f"<tr><td>{_escape(p.gate)}</td>"
        f"<td><a href=\"/inbox/{_escape(p.request_id)}?token={_escape(token)}\">{_escape(p.title)}</a></td>"
        f"<td>{_escape(p.required)}</td><td>{_escape(p.expires.isoformat())}</td></tr>"
        for p in packets
    )
    body = (f"<h1>Inbox</h1><p>Open approvals for {_escape(approver)}.</p>"
            f"<table><tr><th>Gate</th><th>Title</th><th>Required</th><th>Expires</th></tr>{rows}</table>")
    return _page("Inbox", body)


def _render_checks(evidence: EvidenceBundle | None) -> str:
    if evidence is None:
        return "<p>No evidence attached.</p>"
    rows = "".join(
        f"<tr><td>{_escape(name)}</td><td>{_escape(result.conclusion)}</td>"
        f"<td>{_escape(result.detail)}</td></tr>"
        for name, result in evidence.checks.items()
    )
    holdout = ""
    if evidence.holdout is not None:
        passed, total = evidence.holdout
        holdout = f"<p><strong>Holdout:</strong> {_escape(passed)}/{_escape(total)} passed</p>"
    return (f"<p><strong>Rule fired:</strong> {_escape(evidence.rule_fired)} "
            f"(action class {_escape(evidence.action_class)})</p>"
            f"<table><tr><th>Check</th><th>Conclusion</th><th>Detail</th></tr>{rows}</table>"
            f"{holdout}")


def _render_packet(packet: DecisionPacket, *, token: str, approver: str) -> str:
    alternatives = "".join(f"<li>{_escape(a)}</li>" for a in packet.alternatives) or "<li>(none offered)</li>"
    untrusted = "".join(f"<li>{_escape(u)}</li>" for u in packet.untrusted_inputs) or "<li>(none recorded)</li>"
    buttons = "".join(
        f"<button type=\"submit\" name=\"decision\" value=\"{d}\">{d.capitalize()}</button>"
        for d in DECISIONS
    )
    body = f"""
<h1>{_escape(packet.title)}</h1>
<p class="warn">Approving as {_escape(approver)}. Expires {_escape(packet.expires.isoformat())}.
Required: {_escape(packet.required)}.</p>
<h2>Recommendation</h2>
<p>{_escape(packet.recommendation)}</p>
<p>{_escape(packet.summary)}</p>
<h2>Alternatives considered</h2>
<ul>{alternatives}</ul>
<h2>Raw diff</h2>
<pre>{_escape(packet.raw_diff)}</pre>
<h2>Evidence</h2>
{_render_checks(packet.evidence)}
<h2>Recovery plan</h2>
<p>{_escape(packet.recovery_plan) if packet.recovery_plan else '(none provided)'}</p>
<h2>Cost</h2>
<p>${_escape(f"{packet.cost_usd:.2f}")}</p>
<h2>Untrusted inputs the agents read</h2>
<ul>{untrusted}</ul>
<h2>Decide</h2>
<form class="decision-form" method="post" action="/inbox/{_escape(packet.request_id)}/decision">
  <input type="hidden" name="token" value="{_escape(token)}">
  <input type="hidden" name="content_hash" value="{_escape(packet.content_hash)}">
  {buttons}
</form>
"""
    return _page(packet.title, body)


def _render_ack(request_id: str, approver: str, decision: str) -> str:
    body = (f"<h1>Recorded</h1><p>{_escape(approver)} chose <strong>{_escape(decision)}</strong> "
            f"for {_escape(request_id)}.</p>")
    return _page("Recorded", body)


__all__ = ["build_inbox_router"]
