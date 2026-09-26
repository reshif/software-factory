# `factory.inbox` — the approval inbox

Signed links, a small server-rendered FastAPI router, and a Slack notifier for the
human approval gates (final draft §10). See the module docstrings for the details:

- `signing.py` — HMAC-SHA256 tokens (`{request_id, approver, roles, exp, jti}`),
  constant-time verified, with explicit expiry.
- `router.py` — `build_inbox_router(store, decide, signer, clock)`: the list page,
  the packet page, and the decision POST. Depends only on `StateStore` and an
  injected `decide` callable — never on the pipeline, GitHub, the runtime or Slack.
- `slack.py` — `SlackNotifier`: DMs each approver their own signed link; the
  optional channel webhook gets an FYI with no token in it.
- `fake.py` — `FakeNotifier` (alias: `RecordingNotifier`) for tests and the demo.

## Operational requirement: don't log inbox URLs

`GET /inbox` and `GET /inbox/{request_id}` take the signed token as a query
parameter (`?token=...`), because that's the only way to hand a bearer credential to
a plain `<a href>` link — there's no header or cookie a browser will send just from
clicking a link. **Anything that logs full request URLs will capture live,
usable approval tokens** — the ASGI server's own access log, a reverse proxy's
access log, a CDN, browser history sync, etc.

Whoever runs this router in production **must** do at least one of:

1. Disable URL-logging access logs on the server in front of it. For uvicorn,
   pass `access_log=False` (this is what `factory.cli` does).
2. If an access log is required for other reasons (e.g. a reverse proxy you don't
   control), configure it to redact or drop the `token` query parameter before
   it's written or shipped anywhere.

The decision `POST /inbox/{request_id}/decision` does **not** have this problem —
its token is a hidden form field, never part of a URL — so this only applies to the
two `GET` routes above.

We evaluated exchanging the query-string token for a short-lived cookie (a
`POST /inbox/session` endpoint that sets an `HttpOnly`/`SameSite=Strict` cookie and
redirects to a clean URL) and decided against it for now: the very first hit still
has to be a plain `<a href>` GET with the token in the query string (a browser can't
follow a link with a POST), so a cookie exchange doesn't remove the exposure that
matters most — it only helps on repeat visits — while adding real session-management
surface (redirect-target validation, cookie scope, an extra invalidation path) to a
small, otherwise stateless router. Disabling access logs closes the actual gap
outright. If a later requirement makes repeat-visit exposure matter (e.g. a proxy
that can't be reconfigured), revisit this trade-off then.
