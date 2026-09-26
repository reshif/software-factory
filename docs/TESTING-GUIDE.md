# End-to-end testing guide

This is a hands-on guide to testing the software factory in this repository,
stage by stage, from `uv run pytest` up to a real mission running against
your own GitHub org. It is written for the repo owner: technical, but new to
this codebase.

**How this guide was built.** Every command in Stages 1–4 (and the parts of
Stage 5 marked "verified here") was actually run against this repo's `main`
branch on a Linux host with `uv 0.12.5`, Python 3.14.4, and Docker 29.8.0,
and the output below is the real, trimmed output — not a guess. Where a
command didn't behave the way the README implies, that's called out
explicitly in a **Troubleshooting** box, with the exact error. Stages 6 and 7
need a real GitHub org, two GitHub Apps and named human approvers this
environment doesn't have; they're written as precise instructions and marked
**not verified here**.

---

## Stage 0: Orientation

**What this is, in 10 lines.** The software factory is a system where AI
agents (Claude, via the Claude Agent SDK) do the engineering work — planning,
coding, testing, reviewing — and a small deterministic Python service (the
**controller**) is the only thing that ever pushes code, merges a PR, or
deploys. Humans only make four kinds of decision: authorize a mission (H1),
approve a merge (HM), approve a release (H2), or resolve an exception (HX).
Everything else — what's allowed without a human, what needs one, and how
many — comes from one gate table keyed by an **action class** (AC1–AC8), not
from a model's judgment. Agents run in network-off sandboxes with no git or
deploy credentials; the controller captures their diff, classifies it,
checks it against a **mandate** (the approved scope for a piece of work), and
only the controller ever talks to GitHub. The full design is
[`docs/software-factory-final-draft.md`](software-factory-final-draft.md);
this guide tests the implementation in
[`factory-controller/`](../factory-controller/README.md) against it.

**Component map.**

| Component | What it is | Where |
|---|---|---|
| Controller (`factory serve`) | FastAPI app: GitHub webhooks, the approval inbox, `/healthz`, `/metrics` | `factory-controller/src/factory/app.py` |
| Worker (`factory worker`) | Background loop: drains webhooks, runs agent tasks, processes approvals, checks timeouts | `factory-controller/src/factory/worker.py` |
| State store | Postgres in production, in-memory for `demo`/tests | `factory-controller/src/factory/store/` |
| Push bot / merge bot | Two separate GitHub Apps; only identities that ever push/merge | final draft §11, §13.1 |
| LiteLLM gateway | Per-mission budget keys in front of the real Anthropic API | `factory-controller/deploy/litellm/` |
| Egress proxy | Allowlisting forward proxy; the only network path out of a sandboxed task | `factory-controller/deploy/egress-proxy/` |
| Sandbox | One Docker container per task, network off by default, no credentials | `factory-controller/deploy/sandbox/Dockerfile` |
| factory-kit | Gate table, security floor, JSON Schemas, the six agent prompts, golden-path templates | `factory-kit/README.md` |
| Products dir | Your `<product>/factory.yaml` + `mandates/*.yaml` | `FACTORY_PRODUCTS_DIR` |

**The three human gates, plus HX.**

| Gate | Decides | Who (backend-service template) |
|---|---|---|
| **H1** | Authorize a mission (before agents do real work) | PO, TL |
| **HM** | Merge a PR | CODEOWNERS |
| **H2** | Release a digest to production | TL |
| **HX** | An exception: repair budget exhausted, forbidden diff, production regression, kill-switch recovery | PO, TL |

**What "pass" means, per stage.**

| Stage | Passes when |
|---|---|
| 1 | `uv run pytest` reports 0 failed; the Postgres run reports 0 skipped; the Docker run reports 0 skipped (13 passed) |
| 2 | `factory demo --scenario all` prints `14/14 scenarios passed` |
| 3 | You can approve a decision through the browser/curl and see the mission's state advance; a tampered link is refused (403) |
| 4 | Every CLI subcommand you can run locally returns the documented shape, and misuse (bad class, missing config) fails with a clear, non-zero exit |
| 5 | `docker compose config -q` is silent (exit 0); Postgres, LiteLLM and the egress proxy start; `factory migrate` creates real tables; the egress proxy blocks an arbitrary host and allows an allow-listed one |
| 6–7 | Not verified here — see those stages for what "pass" looks like against your own GitHub org |

**Time estimate.**

| Stage | Time |
|---|---|
| 0 Orientation | 10 min read |
| 1 Install + unit tests | 5–10 min (plus ~1 min per Postgres/Docker run) |
| 2 Scripted demo | 5 min |
| 3 Hands-on approval | 15–20 min |
| 4 CLI checks | 15–20 min |
| 5 Local infrastructure | 30–45 min (image builds vary a lot with network speed) |
| 6 Real GitHub setup | 1–2 hours (mostly GitHub App creation) |
| 7 First real mission | 1–3 hours, plus the observation window (24h by default) before DELIVERED |

---

## Stage 1: Install and unit tests

**Goal.** Confirm the controller's own test suite passes on fakes (no
network, no GitHub, no Anthropic key needed), then against a real Postgres
and a real Docker daemon.

**Proves.** The deterministic core (gate resolution, coverage, approvals,
state machines, write-ahead intents) is intact — final draft §6, §10, §14,
and (once Postgres is wired in) the real `StateStore` adapter (§12.2 M1/B1).

**Prerequisites.** `uv` (0.12+), Python 3.11+, Docker (for the Postgres and
sandbox runs).

### 1.1 Install and run the fake-backed suite

```bash
cd factory-controller
uv sync
uv run pytest
```

**Observed:**

```
uv sync
Resolved 56 packages in 1ms
Checked 52 packages in 1ms

uv run pytest
........................................................................ [  7%]
...  (72 lines of dots/skips)  ...
932 passed, 80 skipped in 79.32s (0:01:19)
```

**How to check it.** Last line must say `passed` with `0 failed`/`0 error`.
The 80 skipped tests are the Postgres- and Docker-marked ones (see below) —
skipped, not failed, because no `FACTORY_TEST_DATABASE_URL` or Docker daemon
was available yet at this point.

### 1.2 Run the Postgres-backed tests

```bash
docker run -d --name factory-test-pg \
  -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=factory_test \
  -p 55432:5432 postgres:16
# wait for it to be ready:
until docker exec factory-test-pg pg_isready -U test -d factory_test >/dev/null 2>&1; do sleep 1; done

cd factory-controller
FACTORY_TEST_DATABASE_URL="postgresql://test:test@localhost:55432/factory_test" uv run pytest -m postgres
```

**Observed:**

```
........................................................................ [ 88%]
.........                                                                [100%]
81 passed, 931 deselected in 6.52s
```

Running the **whole** suite with the variable set (not just `-m postgres`)
gives:

```
1012 passed in 83.75s (0:01:23)
```

**How to check it.** `0 skipped` (matches the task's expectation) — every
test that was skipped in 1.1 for lack of Postgres now runs and passes.

**Cleanup:**

```bash
docker stop factory-test-pg && docker rm factory-test-pg
```

### 1.3 Run the Docker sandbox tests

```bash
cd factory-controller
uv run pytest -m docker tests/sandbox
```

**Observed** (with a working local Docker daemon):

```
.............                                                            [100%]
13 passed, 26 deselected in 4.81s
```

**What the skip means.** `tests/sandbox/test_docker_sandbox.py` gates every
test in that file with:

```python
pytest.mark.docker,
pytest.mark.skipif(not _docker_available(), reason="docker is not available"),
```

so on a host with no Docker daemon reachable, these 13 tests report
`skipped (docker is not available)` rather than failing — the suite as a
whole still passes, just with fewer tests exercised. This is also why the
1.1 run (before Docker was used for anything) still showed 80 skipped
overall: some of those are the Docker-marked tests, independent of Postgres.

**Troubleshooting.**
- If `docker run` for the test Postgres fails with a port conflict, change
  `-p 55432:5432` to a free host port and update the connection URL to match.
- `uv sync` produced no visible output here because the environment was
  already resolved from an earlier session; a completely fresh clone will
  show real download/build lines instead of `Checked 52 packages in 1ms`.

---

## Stage 2: Scripted demo

**Goal.** Run every built-in end-to-end scenario against fakes, in one shot,
and read one scenario's timeline in detail.

**Proves.** The full mission lifecycle (final draft §7), the gate table
(§6.2), and specific hardening fixes (stale approvals, replay rejection,
human intervention recording) all work together, end to end, without any
external dependency.

**Prerequisites.** Stage 1's `uv sync`.

### 2.1 Run every scenario

```bash
cd factory-controller
uv run factory demo --scenario all
```

**Observed** (trimmed; full run took ~29s):

```
########## scenario: happy_path ##########
=== happy_path (MIS-6a5b3c0787) ===
  ... mission_transition NEW -> DISCOVERING -> AWAITING_H1 ...
  approval_requested        {'gate': 'H1', ...}
  approval_decided          {'decision': 'approve', 'approver': '@po'}
  ... ADMITTED -> ACTIVE -> INTEGRATING ...
  revision_pushed           {'sha': '1520aab3...'}
  ... AWAITING_HM -> MERGED -> RELEASE_READY ...
  deployed                  {'environment': 'staging', ...}
  ... AWAITING_H2 -> DEPLOYING ...
  deployed                  {'environment': 'production', ...}
  ... OBSERVING -> DELIVERED ...
  final state: DELIVERED
result: PASS

  [... 13 more scenarios ...]

===== summary =====
  PASS  happy_path
  PASS  patch_standing
  PASS  coverage_exceeded
  PASS  h1_reject
  PASS  repair_then_pass
  PASS  repair_exhausted_hx
  PASS  forbidden_path_blocked
  PASS  post_merge_revert
  PASS  prod_regression_rollback
  PASS  stale_approval_rejected
  PASS  replay_after_rollback
  PASS  human_push_recorded
  PASS  second_release_standing
  PASS  kill_switch_resume
14/14 scenarios passed
```

**Troubleshooting / doc mismatch.** `factory-controller/README.md`'s
scenario table lists only **12** scenarios. The shipped code
(`src/factory/demo/scenarios.py`, the `SCENARIOS` dict) has **14** —
`second_release_standing` and `kill_switch_resume` exist and pass but aren't
documented in that table. Both are described below from the source, and both
ran successfully in this environment. Also note the startup line printed
before any scenario output: `LocalSandbox is NOT a security boundary:
commands run as the controller's own user with no container isolation and no
network control.` — this is expected; the demo uses `LocalSandbox`/fakes
throughout, not `DockerSandbox`, and the message is the module telling you
so, not an error.

### 2.2 What every scenario proves

| Scenario | What it simulates | Expected end state | Proves (§) |
|---|---|---|---|
| `happy_path` | Feature lane, everything green | `DELIVERED` | §7 (whole flow) |
| `patch_standing` | A patch covered by the standing mandate | `DELIVERED`, no H1 | §6.1, §6.2 (`standing`/`auto`) |
| `coverage_exceeded` | A "small" patch whose real diff is 200 lines | `AWAITING_H1` | §6.1 (deterministic coverage, checked on the diff) |
| `h1_reject` | H1 revise (fresh discovery), then decline | `ARCHIVED` | §6.3 (revise voids the round) |
| `repair_then_pass` | First implementation fails its own test, repair fixes it | `DELIVERED`, ≥1 repair attempt used | §9.2 (bounded repair loop) |
| `repair_exhausted_hx` | Implementation never passes | `AWAITING_HX`, a `FAILED` task | §7 (repair budget exhausted → boundary → HX) |
| `forbidden_path_blocked` | Architect routes a task at `.github/workflows/**` | `AWAITING_HX`, **no PR ever opened** | §13 (AC8, blocked before push) |
| `post_merge_revert` | CI fails on `main` after merge | `ACTIVE` again, `reverted`+`recovered` events, a `repair-*` task | §7 step 9 (auto-revert → REPAIRING) |
| `prod_regression_rollback` | Deploy target reports unhealthy in production | `AWAITING_HX`, a `rolled_back` receipt, flag rollout `0` | §7 step 10, §13.1 (kill flag, always escalates) |
| `stale_approval_rejected` | Mission's state version moves after the H1 request opened | Approval stays `open`, mission stays `AWAITING_H1` | §10 (CAS on state version) |
| `replay_after_rollback` | Reusing an H2 approval already consumed once | `AlreadyConsumed` raised | §13.1 #8 (single-use approvals) |
| `human_push_recorded` | A human pushes to the mission's `factory/*` branch | `human_intervention` event; pusher added to `editors` | §7 step 13 (human intervention, no-self-approval feed) |
| `second_release_standing` | Two back-to-back standing (auto-H1/auto-H2) releases | Both `DELIVERED`; 2 distinct production artifacts/fencing tokens | §6.4 (nothing collides across releases) |
| `kill_switch_resume` | Kill switch mid-mission, resume, defer, unblock | Ends `MERGED` after HELD→AWAITING_HX→BLOCKED→ACTIVE | operator entry points (kill-switch/resume/unblock) |

### 2.3 Run one scenario and read its timeline

```bash
uv run factory demo --scenario happy_path
```

Each line is one `append_event` call from the real orchestrator, in order:
`mission_transition` lines show `{'from', 'event', 'to'}`; `approval_requested`
/`approval_decided` show the gate and who decided; `revision_pushed` and
`deployed` show the sha/artifact. Read top to bottom — it's the exact
sequence `/metrics` and the Postgres `events` table would show for a real
mission (Stage 7 shows the SQL).

---

## Stage 3: Hands-on local approval

**Goal.** Approve a real decision through the browser/HTTP path the way a
human approver would, and verify the inbox's security properties (signed,
single-use, request-scoped tokens).

**Proves.** Final draft §10 (the durable approval workflow) and §13.1 #8
(single-use approvals) against the real FastAPI app and real `TokenSigner` —
not a scripted call into the orchestrator.

**Prerequisites.** Stage 1's `uv sync`. Nothing else — `--serve` wires every
port to a fake, like `--scenario`.

### 3.1 Start it and get the printed link

```bash
cd factory-controller
uv run factory demo --serve &
```

**Observed** (tokens shortened):

```
LocalSandbox is NOT a security boundary: ...
Seeded mission MIS-7095706ab8. Approve each gate by opening its link.
New links are printed here as gates open (H1 -> HM -> H2), then the mission is DELIVERED.
  [H1] open as @po: http://127.0.0.1:8080/inbox/REQ-H1-215c6196f7?token=eyJhcHByb3Zlci...

Serving on http://127.0.0.1:8080 (Ctrl+C to stop)
```

The demo keeps printing: every state change (`mission MIS-… -> AWAITING_HM`)
and a **new link each time a new approval opens**. Keep this terminal visible.

### 3.2 Check `/healthz` and `/metrics`

```bash
curl -s http://127.0.0.1:8080/healthz
curl -s http://127.0.0.1:8080/metrics
```

**Observed:**

```
{"status":"ok"}

# HELP factory_deployment_frequency_per_day Production deployments per day.
# TYPE factory_deployment_frequency_per_day gauge
factory_deployment_frequency_per_day 0.0
# HELP factory_deployment_count Number of production deployments observed.
factory_deployment_count 0
# HELP factory_human_interventions_outside_approvals ...
factory_human_interventions_outside_approvals 0
# HELP factory_delivered_count Number of delivered (accepted) changes observed.
factory_delivered_count 0
```

### 3.3 Open the inbox page and read it

```bash
curl -s "http://127.0.0.1:8080/inbox/REQ-H1-3f7f907e9a?token=<the printed token>"
```

The page (also viewable in a real browser) shows, in order: the mission
title as `<h1>`; who you're approving as and the expiry (`Approving as @po.
Expires ...`); the architect's recommendation; alternatives considered; the
raw diff; evidence; the recovery plan; cost; the list of untrusted inputs the
agents read (e.g. `issue body of org/backend-service#1` — literally
labeled "untrusted"); and a plain HTML `<form method="post">` with four
buttons — **Approve / Revise / Defer / Cancel** — carrying two hidden
fields, `token` and `content_hash`.

### 3.4 POST a decision the way the form does

```bash
TOKEN="<the printed token>"
CH=$(curl -s "http://127.0.0.1:8080/inbox/REQ-H1-3f7f907e9a?token=$TOKEN" \
     | grep -oP 'content_hash" value="\K[^"]+')
curl -s -X POST "http://127.0.0.1:8080/inbox/REQ-H1-3f7f907e9a/decision" \
  --data-urlencode "token=$TOKEN" \
  --data-urlencode "decision=approve" \
  --data-urlencode "content_hash=$CH"
```

**Observed:**

```html
<h1>Recorded</h1><p>@po chose <strong>approve</strong> for REQ-H1-3f7f907e9a.</p>
```

**Replaying the identical token** (same request, same decision) is refused:

```bash
curl -s -i -X POST "http://127.0.0.1:8080/inbox/REQ-H1-3f7f907e9a/decision" \
  --data-urlencode "token=$TOKEN" --data-urlencode "decision=approve" --data-urlencode "content_hash=$CH"
```

```
HTTP/1.1 409 Conflict
{"detail":"token CX9NNF9AuHwLz5Xi was already used to record a decision"}
```

### 3.5 A tampered link is refused

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  "http://127.0.0.1:8080/inbox/REQ-H1-3f7f907e9a?token=tampered-invalid-token"
```

**Observed:** `403`, body `{"detail":"invalid token"}`.

### 3.6 Take the mission all the way to DELIVERED

In `--serve` mode, fakes play the parts GitHub and the agents play in
production: the agents are scripted (planner, implementer, reviewer), **CI
"passes" automatically** on every pushed and merged commit (a simulated
`check_suite` webhook), and once the mission has been in `OBSERVING` for about
20 seconds the clock is fast-forwarded past the 24-hour observation window.
You only do what a human approver does: open each link and click **Approve**.

1. Open the `[H1]` link and click **Approve** (or POST as in 3.4).
2. Within a few seconds the terminal shows the task running and a new `[HM]`
   link (the merge approval, as `@tl`). Open it and **Approve**.
3. CI "passes" on the merge commit, the build deploys to staging, the holdouts
   run, and a new `[H2]` link (the release approval) appears. **Approve** it.
4. The mission moves to `OBSERVING`, and about 20 seconds later to `DELIVERED`.

**Observed** (verified end to end over plain HTTP, every POST returned 200):

```
  [H1] open as @po: http://127.0.0.1:8080/inbox/REQ-H1-215c6196f7?token=...
  mission MIS-7095706ab8 -> AWAITING_H1
  mission MIS-7095706ab8 -> AWAITING_HM
  [HM] open as @tl: http://127.0.0.1:8080/inbox/REQ-HM-bf88a81163?token=...
  mission MIS-7095706ab8 -> MERGED
  [H2] open as @tl: http://127.0.0.1:8080/inbox/REQ-H2-9047171c7c?token=...
  mission MIS-7095706ab8 -> OBSERVING
  mission MIS-7095706ab8 -> DELIVERED
```

Each link is signed for **one approver and one request**: the H1 token can't
open or decide the HM request (`403 token is not valid for this request`),
which is why the demo prints a fresh link per gate. In production those links
arrive as Slack DMs, or you mint them with `factory inbox-link` (Stage 4.5).

Afterwards, `curl -s http://127.0.0.1:8080/metrics | grep -v '^#'` shows it
counted (observed):

```
factory_deployment_count 1
factory_interventions_per_delivered_change 0.0
factory_approvals_per_delivered_change 3.0
factory_delivered_count 1
```

### 3.7 Reject/revise

Using the packet's `content_hash` and a fresh token the same way as 3.4, but
`decision=revise`:

```bash
curl -s -X POST "http://127.0.0.1:8091/inbox/REQ-H1-7ac828a234/decision" \
  --data-urlencode "token=$TOKEN" --data-urlencode "decision=revise" --data-urlencode "content_hash=$CH"
```

**Observed:** `<h1>Recorded</h1><p>@po chose <strong>revise</strong> for REQ-H1-7ac828a234.</p>`,
and (as with approve) the same token replayed a second time is refused with
`409`. The full revise → fresh discovery round → decline → `ARCHIVED`
transition is asserted end to end by the scripted `h1_reject` scenario in
Stage 2 — that's the more useful place to see the complete state sequence;
this HTTP-level test confirms the wire mechanics (200 on the first POST, 409
on token reuse) against the real router.

### 3.8 Cleanup

```bash
kill %1   # or: pkill -f "factory demo --serve"
```

**Troubleshooting.**
- If curl shows `Connection refused`, the server likely hasn't finished
  starting — wait 2–3s after launching before the first request.
- The `content_hash` in the decision POST must be the exact value from the
  packet page's hidden field for that specific open request — a stale or
  hand-typed hash gets a `409 StaleApproval`.
- A new link appears only after the worker's next tick (every ~2s). If
  nothing appears, check the terminal for `demo tick failed` and its traceback.

---

## Stage 4: CLI and policy checks

**Goal.** Exercise every `factory` subcommand that runs locally, including
its failure modes.

**Proves.** The gate table, action-class classifier, and inbox link signing
work as documented (`factory-kit/policies/gate-table.yaml`,
`policy/action_classes.py`), and that CLI misuse fails clearly.

All commands below were run from `factory-controller/`.

### 4.1 `validate-kit`

```bash
uv run factory validate-kit
```
```
policy_version=p-2026.10.3: OK
```

### 4.2 `products validate`

Copy the golden-path template to a scratch products dir first (never point
this at the repo's own `factory-kit/templates/` in place):

```bash
mkdir -p /tmp/products-scratch
cp -r ../factory-kit/templates/backend-service /tmp/products-scratch/
uv run factory products validate --products-dir /tmp/products-scratch
```
```
backend-service: OK (repo=org/backend-service, 1 standing mandate(s))
```

### 4.3 `gates` — several classes and profiles

```bash
uv run factory gates --class AC4 --profile standard
```
```json
{"h1": "1", "hm": "1", "h2": "1", "rules": ["policy p-2026.10.3", "base.AC4"]}
```

```bash
uv run factory gates --class AC8 --profile regulated
```
```json
{"h1": "blocked", "hm": "blocked", "h2": "blocked", "rules": ["policy p-2026.10.3", "base.AC8"]}
```

**The `--protected` flag, shown against AC3** (a normally-light class):

```bash
uv run factory gates --class AC3 --profile standard              # no --protected
```
```json
{"h1": "1", "hm": "1", "h2": "1", "rules": ["policy p-2026.10.3", "base.AC3", "not_covered->mission_h1"]}
```
```bash
uv run factory gates --class AC3 --profile standard --protected  # touches a protected path
```
```json
{"h1": "1+sec", "hm": "1+sec", "h2": "1",
 "rules": ["policy p-2026.10.3", "base.AC3", "protected_paths->AC6"]}
```

This is the table-effect the task asks to see: `--protected` promotes
*any* class's H1/HM requirement to the AC6 row (`1+sec`) — confirmed here by
diffing AC3 with and without the flag; AC6 itself needs security either way
since it's already the strictest applicable row.

**Autonomy relaxation** (L5, experimental, AC1):

```bash
uv run factory gates --class AC1 --profile experimental --level L5
```
```json
{"h1": "1", "hm": "auto_sampled", "h2": "standing",
 "rules": ["policy p-2026.10.3", "base.AC1", "L5.hm->auto_sampled", "L5.h2->standing", "not_covered->mission_h1"]}
```

**Bad input, clean failure:**

```bash
uv run factory gates --class AC99 --profile standard; echo "exit=$?"
```
```
factory gates: unknown action class 'AC99'
exit=2
```

### 4.4 `classify` on a sample git range

```bash
mkdir -p /tmp/classify-repo/.github/workflows && cd /tmp/classify-repo
git init -q && git config user.email t@e.com && git config user.name t
mkdir -p app && echo "print('hello')" > app/main.py
echo "name: ci" > .github/workflows/ci.yml
git add -A && git commit -qm base
BASE=$(git rev-parse HEAD)
echo "print('world')" >> .github/workflows/ci.yml
git add -A && git commit -qm "touch workflow"
HEAD=$(git rev-parse HEAD)
cd -   # back to factory-controller
uv run factory classify --git "$BASE..$HEAD" --repo /tmp/classify-repo
```
```json
{
  "action_class": "AC8",
  "reasons": ["forbidden paths: ['.github/workflows/ci.yml']", "small change: 1 lines <= 150"],
  "protected_touched": [],
  "forbidden_touched": [".github/workflows/ci.yml"],
  "diff_lines": 1
}
```

A one-line change to a workflow file is still `AC8` — line count never
overrides a forbidden path.

### 4.5 `inbox-link`

```bash
FACTORY_INBOX_SIGNING_KEY="test-signing-key" \
  uv run factory inbox-link --request REQ-H1-demo --approver @tl --roles tech_lead
```
```
http://localhost:8080/inbox/REQ-H1-demo?token=eyJhcHByb3ZlciI6IkB0bCIs...
```

Without the signing key set:
```
uv run factory inbox-link --request REQ-H1-demo --approver @tl --roles tech_lead
FACTORY_INBOX_SIGNING_KEY is not set
```
(exit 1)

### 4.6 `kill-switch`, `resume`, `unblock`

These operate on the missions in the factory's store, so for real use point
`FACTORY_DATABASE_URL` at the factory's Postgres. Without it, each CLI run uses
a fresh, empty in-memory store; the command warns you about that. They also
need a products directory (`FACTORY_PRODUCTS_DIR`, e.g. the scratch copy from 4.2).

```bash
FACTORY_PRODUCTS_DIR=/tmp/products-scratch uv run factory kill-switch
```
```
warning: FACTORY_DATABASE_URL is not set, so this acts on a fresh, empty in-memory store; point it at the factory's Postgres to act on real missions
kill switch engaged: every non-terminal mission moved to HELD
```

Configuration and input problems are one-line errors with exit code 2 (verified):

```bash
FACTORY_PRODUCTS_DIR=/tmp/products-scratch uv run factory resume --mission MIS-doesnotexist; echo "exit=$?"
```
```
warning: FACTORY_DATABASE_URL is not set, ...
error: not found: MIS-doesnotexist
exit=2
```

Without a products directory: `error: products_dir /…/products does not exist` (exit 2).

To see `resume`/`unblock` act on a real mission, run the Stage 5 Postgres, set
`FACTORY_DATABASE_URL`, and use a mission id from `SELECT mission_id, state FROM missions;`.
The full kill-switch → resume → HX → unblock sequence is also asserted by the
`kill_switch_resume` scenario in Stage 2.

### 4.7 `github setup --dry-run`

```bash
uv run factory github setup --repo org/backend-service --dry-run
```
```
factory github setup: FACTORY_MERGE_APP_ID is not set
```
(exit 1 — needs at least the merge bot's numeric App id, even for `--dry-run`)

```bash
FACTORY_MERGE_APP_ID=123456 uv run factory github setup --repo org/backend-service --dry-run
```
```json
{
  "actions_permissions": {"can_approve_pull_request_reviews": false, "default_workflow_permissions": "read"},
  "ruleset": {
    "bypass_actors": [{"actor_id": 123456, "actor_type": "Integration", "bypass_mode": "always"}],
    "conditions": {"ref_name": {"exclude": [], "include": ["refs/heads/main"]}},
    "enforcement": "active",
    "name": "factory-protect-main",
    "rules": [
      {"type": "deletion"},
      {"type": "non_fast_forward"},
      {"type": "pull_request", "parameters": {"dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": true, "required_approving_review_count": 1, ...}},
      {"type": "required_status_checks", "parameters": {"required_status_checks":
        [{"context": "lint"}, {"context": "types"}, {"context": "secret_scan"},
         {"context": "dep_audit"}, {"context": "unit"}], "strict_required_status_checks_policy": true}}
    ],
    "target": "branch"
  }
}
```

No GitHub API call is made in `--dry-run` mode — confirmed by the fact this
ran successfully with a made-up `FACTORY_MERGE_APP_ID` and no network at all.

---

## Stage 5: Local infrastructure (docker-compose)

**Goal.** Validate the compose file and bring up the pieces that don't need
GitHub: Postgres (with the separate `litellm` role/database), and the
egress-allowlist proxy.

**Proves.** §12.2/§12.4 (the real deployment topology), and concretely, the
egress control in §13.1 #2/#5 (a sandboxed task can reach only the LLM
gateway and a package mirror — nothing else).

**Important:** do this in a scratch copy, not the repo. `docker-compose.yml`
builds with `context: ../..` (the repo root, so the Dockerfile can `COPY
factory-controller/...`), so the scratch copy needs to mirror that one
relative layout, not just the `deploy/` folder in isolation.

### 5.1 Set up the scratch copy

```bash
mkdir -p /tmp/factory-deploy-scratch
rsync -a --exclude='.venv' --exclude='__pycache__' --exclude='.pytest_cache' \
  factory-controller/ /tmp/factory-deploy-scratch/factory-controller/
cd /tmp/factory-deploy-scratch
```

### 5.2 Build the sandbox image (verified here)

```bash
docker build -t factory-sandbox:latest \
  -f factory-controller/deploy/sandbox/Dockerfile factory-controller/deploy/sandbox
```

**Observed:** builds cleanly from `debian:12-slim` (apt-only: git, python3,
pytest, node, npm) — succeeded on the second attempt after a transient DNS
failure resolving `registry-1.docker.io` on the first try (see
Troubleshooting).

### 5.3 Build the controller image (verified here)

```bash
docker build -t factory-controller:latest \
  -f factory-controller/deploy/controller.Dockerfile .    # context is the repo root
```

It needs network access to `registry.npmjs.org` (the pinned Claude CLI) and
PyPI (Python dependencies). On a slow link the first build can take several
minutes; the Dockerfile sets a generous `UV_HTTP_TIMEOUT`.

**Check it** (observed):

```bash
docker run --rm --entrypoint sh factory-controller:latest -c \
  'id -u; node --version; $FACTORY_CLAUDE_CLI_PATH --version; factory --help | head -1'
```
```
1000
v22.23.3
2.1.283 (Claude Code)
usage: factory [-h]
```

It runs as a non-root user (uid 1000), with Node 22 (required by the Claude
CLI) and the CLI at `FACTORY_CLAUDE_CLI_PATH=/usr/local/bin/claude`.

### 5.4 The six secrets

```bash
mkdir -p factory-controller/deploy/secrets
cd factory-controller/deploy/secrets
python3 -c "import secrets; print(secrets.token_urlsafe(24))" > postgres_password.txt
python3 -c "import secrets; print(secrets.token_urlsafe(24))" > litellm_password.txt
echo "<your real Anthropic API key>"                          > anthropic_api_key.txt
# push-app.pem / merge-app.pem: downloaded from each GitHub App's settings page
# (Stage 6) — there is no way to generate these locally. For a LOCAL, GitHub-less
# `docker compose config -q`/`up` test, any well-formed PEM file satisfies the
# Compose secret (the file only needs to exist; nothing here parses its content
# until a real GitHub call is made) -- e.g.:
openssl genrsa -out push-app.pem 2048
openssl genrsa -out merge-app.pem 2048
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > llm_gateway_master_key.txt
cd ../../..
```

| # | File | Real production value |
|---|---|---|
| 1 | `postgres_password.txt` | the `factory` Postgres role's password |
| 2 | `litellm_password.txt` | the separate `litellm` role's password (its own DB) |
| 3 | `anthropic_api_key.txt` | your real Anthropic API key (LiteLLM only) |
| 4 | `push-app.pem` | the push-bot GitHub App's private key (from GitHub) |
| 5 | `merge-app.pem` | the merge-bot GitHub App's private key (from GitHub, separate App) |
| 6 | `llm_gateway_master_key.txt` | LiteLLM's admin master key |

### 5.5 `.env` and `docker compose config -q`

```bash
cp factory-controller/deploy/.env.example factory-controller/deploy/.env
docker compose -f factory-controller/deploy/docker-compose.yml \
  --env-file factory-controller/deploy/.env config -q
echo "exit=$?"
```

**Observed:** `exit=0`, no output — confirmed **even with every `FACTORY_*`
value in `.env` left blank** (a copy of `.env.example` as-is). `config -q`
only validates Compose YAML syntax and variable interpolation; it does not
check that the six secret files exist (also confirmed: temporarily renaming
`secrets/` away still passed `config -q`) or that any `FACTORY_*` value is
non-empty — those only matter once you `up` the `controller`/`worker`
services (`Settings.validate()` enforces the required production fields at
container startup, and Compose's `secrets:` block needs the files to exist
at `up` time, not at `config` time).

### 5.6 Bring up Postgres and the egress proxy (verified here)

```bash
docker compose -f factory-controller/deploy/docker-compose.yml \
  --env-file factory-controller/deploy/.env -p factory-test up -d postgres egress-proxy
```

**Observed:** both start; Postgres reports `healthy` within ~10s.

**Verify the separate `litellm` role/database exists** (`postgres-init/001-litellm.sh`):

```bash
docker exec factory-test-postgres-1 psql -U factory -d factory -c "\du"
docker exec factory-test-postgres-1 psql -U factory -d factory -c "\l"
```
```
 Role name |                         Attributes
-----------+------------------------------------------------------------
 factory   | Superuser, Create role, Create DB, Replication, Bypass RLS
 litellm   |
...
   Name    |  Owner
-----------+---------
 factory   | factory
 litellm   | litellm
```

**Run `factory migrate` against it** (publish the port for a host-side test —
add a `docker-compose.override.yml` in the scratch dir only, with
`services: {postgres: {ports: ["55433:5432"]}}`, then `up -d postgres`
again to pick it up):

```bash
PGPASS=$(cat factory-controller/deploy/secrets/postgres_password.txt)
cd factory-controller
FACTORY_DATABASE_URL="postgresql://factory:${PGPASS}@localhost:55433/factory" uv run factory migrate
```
```
migrations applied
```

```bash
docker exec factory-test-postgres-1 psql -U factory -d factory -c "\dt"
```
```
 Schema |        Name        | Type  |  Owner
--------+--------------------+-------+---------
 public | approval_decisions | table | factory
 public | approvals          | table | factory
 public | events             | table | factory
 public | evidence           | table | factory
 public | intents            | table | factory
 public | missions           | table | factory
 public | packets            | table | factory
 public | schema_migrations  | table | factory
 public | tasks              | table | factory
 public | used_keys          | table | factory
 public | webhook_inbox      | table | factory
(11 rows)
```

### 5.7 Verify the egress allowlist (verified here)

```bash
docker run --rm --network factory-egress curlimages/curl:8.10.1 \
  -s -o /dev/null -w "arbitrary host: %{http_code}\n" -x http://egress-proxy:8888 http://example.com
docker run --rm --network factory-egress curlimages/curl:8.10.1 \
  -s -o /dev/null -w "allow-listed:  %{http_code}\n" -x http://egress-proxy:8888 https://pypi.org
```
```
arbitrary host: 403
allow-listed:  200
```

This is `filter.allowlist`'s `FilterDefaultDeny Yes` doing exactly what
§13.1 #2/#6 says: a sandboxed task (or anything else on the `factory-egress`
network) can reach `litellm` and the package mirrors listed, and gets a flat
403 for anything else — including a plausible-looking arbitrary domain.

### 5.8 LiteLLM (attempted, not completed here)

```bash
docker compose -f factory-controller/deploy/docker-compose.yml \
  --env-file factory-controller/deploy/.env -p factory-test up -d litellm
```

**Observed:** the initial `up` (attempting postgres+litellm+egress-proxy
together) failed pulling `ghcr.io/berriai/litellm:main-stable` — a large
image — with the same class of transient network failure as 5.3
(`httpReadSeeker: ... net/http: timeout awaiting response headers`). This is
almost certainly this sandbox's network conditions, not the image or the
compose file; the reference to `ghcr.io/berriai/litellm:main-stable` in
`docker-compose.yml` is otherwise unremarkable. Not verified here — retry on
a host with a stable connection to `ghcr.io`.

### 5.9 Cleanup

```bash
docker compose -f factory-controller/deploy/docker-compose.yml \
  --env-file factory-controller/deploy/.env -p factory-test down -v
docker network rm factory-egress 2>/dev/null || true   # only if `down -v` left it (it shouldn't)
docker rmi factory-sandbox:latest 2>/dev/null || true
rm -rf /tmp/factory-deploy-scratch /tmp/products-scratch /tmp/classify-repo
```

Confirm nothing of yours is left, and `agentic-bots-pg` (pre-existing,
unrelated to this guide) is untouched:

```bash
docker ps -a
```

**Troubleshooting.**
- Any `ETIMEDOUT`/`httpReadSeeker` error pulling from `ghcr.io` or
  `registry.npmjs.org` in your own run is a network/proxy issue on your
  build host, not a bug in this repo's Dockerfiles — retry, or point Docker
  at a mirror.
- `docker compose config -q` succeeding is **not** proof the stack will
  actually start — it only validates syntax/interpolation; `up` is the real
  test, and it needs the secrets and a working image build first.

---

## Stage 6: Real GitHub setup

**Not verified here: requires your accounts.** This stage needs a GitHub
org you administer, permission to create GitHub Apps, and (for Stage 7)
named human approvers. Everything below is precise, but untested in this
environment.

### 6.1 Create the two repos

1. Create a new repo from the template at `factory-kit/templates/backend-service/`
   (copy its contents into a fresh repo, e.g. `org/backend-service`; it's a
   plain directory, not a GitHub "template repository" in this codebase, so
   a straightforward `git init && git add -A && git commit` + push works).
2. Create a second repo from `factory-kit/templates/holdouts-repo/`, e.g.
   `org/backend-service-holdouts` (matching `factory.yaml`'s `holdout_repo`
   field). Its `CODEOWNERS` (`@po @sec`) should list real GitHub usernames.

### 6.2 Create the two GitHub Apps

Create **two separate** GitHub Apps (Settings → Developer settings → GitHub
Apps → New GitHub App) — this separation is load-bearing (final draft §11,
§13.1 #9): a compromised push bot can never merge, and the merge bot can
never touch `.github/workflows/**`.

| App | Repository permissions | Webhook events | Never |
|---|---|---|---|
| **push bot** | Contents: Read & write · Pull requests: Read & write · Issues: Read · Checks: Read · Metadata: Read | Issues, Pull request review, Check suite, Push | Workflows |
| **merge bot** | Contents: Read & write · Pull requests: Read & write · Metadata: Read | Issues, Pull request review, Check suite, Push | Issues, Checks |

For each App: set the webhook URL to your controller's public
`/webhooks/github` endpoint (Stage 6.5 covers a local tunnel), set the
webhook secret (this becomes `FACTORY_GITHUB_WEBHOOK_SECRET`), generate a
private key (download the `.pem` — this becomes `secrets/push-app.pem` /
`secrets/merge-app.pem`), and install the App on `org/backend-service`
(note the numeric installation id from the install URL or the API — this
becomes `FACTORY_PUSH_APP_INSTALLATION_ID` / `FACTORY_MERGE_APP_INSTALLATION_ID`).

Additionally, create a **third**, read-only credential for the holdout
runner (a fine-grained PAT or its own App installation) scoped to
`org/backend-service-holdouts` only, with **Actions: Read and write** (per
`factory-kit/templates/holdouts-repo/README.md` — this single permission
covers both dispatching `workflow_dispatch` and reading the run result) and
nothing else — this becomes `FACTORY_HOLDOUT_TOKEN`.

### 6.3 `factory github setup`

```bash
cd factory-controller
FACTORY_MERGE_APP_ID=<merge bot's numeric App id> \
  uv run factory github setup --repo org/backend-service --dry-run
```

Review the printed ruleset plan (same shape as Stage 4.7's output), then, with
the push bot's full credentials set (it needs admin on the repo to apply a
ruleset):

```bash
FACTORY_MERGE_APP_ID=... FACTORY_PUSH_APP_ID=... \
  FACTORY_PUSH_APP_PRIVATE_KEY_PATH=... FACTORY_PUSH_APP_INSTALLATION_ID=... \
  uv run factory github setup --repo org/backend-service
```

This applies branch protection to `main`: required status checks, required
CODEOWNERS review, and a merge restriction to the merge-bot App — and turns
off "Allow GitHub Actions to create and approve pull requests" (§13.2).

### 6.4 Products dir, `factory.yaml`, and deploy hooks

Point `FACTORY_PRODUCTS_DIR` at a directory containing one subdirectory per
product, e.g. `products/backend-service/factory.yaml` (copy the template's
`factory.yaml` and `mandates/SM-patch.yaml`), and edit:

- `repo`/`holdout_repo` to your real `org/...` names.
- `owners`/`approvers` to real GitHub logins.
- `slack_ids` (add this section) mapping each approver login to their Slack
  member id, if you're wiring up DMs (`FACTORY_SLACK_BOT_TOKEN`); without
  it, the controller falls back to the logging notifier and you mint links
  by hand with `factory inbox-link`.
- `checks` if your product's lint/test commands differ from the template's.

**A minimal deploy target for testing**, using one local Docker container
per environment instead of a real cloud target — five shell one-liners are
enough to satisfy `CommandDeployTarget`'s contract:

```bash
FACTORY_DEPLOY_BUILD_CMD='docker build -q -t backend-service:$FACTORY_DEPLOY_ARTIFACT .'
FACTORY_DEPLOY_CMD='docker run -d --name backend-service-$FACTORY_DEPLOY_ENVIRONMENT -p 0:8080 backend-service:$FACTORY_DEPLOY_ARTIFACT'
FACTORY_DEPLOY_HEALTH_CMD='curl -sf http://localhost:$(docker port backend-service-$FACTORY_DEPLOY_ENVIRONMENT 8080 | cut -d: -f2)/health'
FACTORY_DEPLOY_ROLLBACK_CMD='docker stop backend-service-$FACTORY_DEPLOY_ENVIRONMENT'
FACTORY_DEPLOY_URL_CMD='echo http://localhost:$(docker port backend-service-$FACTORY_DEPLOY_ENVIRONMENT 8080 | cut -d: -f2)'
```

(Treat this as a starting point to adapt, not a copy-paste-verified script —
it wasn't run in this environment; see `release/deploy.py` for the exact env
vars each hook receives.)

### 6.5 Point the webhook at your controller

For local testing, run `factory serve` and expose it with a tunnel so
GitHub's webhook can reach it:

```bash
# cloudflared (no account needed for a quick tunnel):
cloudflared tunnel --url http://localhost:8080
# or smee.io:
npx smee-client --url https://smee.io/<your-channel> --target http://localhost:8080/webhooks/github
```

Set each GitHub App's webhook URL to the tunnel's `https://.../webhooks/github`.

---

## Stage 7: The first real mission

**Not verified here: requires your accounts.**

### 7.1 Open the mission

Open an issue on `org/backend-service` describing a small, real change, and
add the label `factory:feature` (or `factory:patch` if it's covered by
`mandates/SM-patch.yaml`).

### 7.2 Walk the stages

| Stage | What the approver sees | Where to look |
|---|---|---|
| Discovery → H1 | A Slack DM with a signed link (if `FACTORY_SLACK_BOT_TOKEN`+`slack_ids` configured), or run `factory inbox-link --request <id> --approver @you --roles product` yourself | `/inbox/{request_id}` |
| Admission → tasks | Nothing to approve; watch progress | `/metrics`, the `events` table |
| Integration → HM | A GitHub PR opens (controller-authored title/body from evidence); either a `pull_request_review` APPROVED, or the inbox | The PR page; `/inbox` |
| Post-merge → RELEASE_READY | CI runs on the merge commit | The PR's checks tab |
| Release candidate → H2 | One packet per release digest, listing every mission in it | `/inbox/{request_id}` |
| Deploy → observe → DELIVERED | Nothing to approve; the flag ramps to 100%, then the observation window (`factory.yaml`'s `observation_window`, 24h in the template) | `/metrics` |

**Query mission/event history directly in Postgres:**

```sql
select mission_id, state, product, created_at from missions order by created_at desc limit 10;
select kind, payload, created_at from events where mission_id = 'MIS-...' order by created_at;
select request_id, gate, status, expires from approvals where mission_id = 'MIS-...';
```

**Expected state transitions** (feature lane, everything green): `NEW` →
`DISCOVERING` → `AWAITING_H1` → `ADMITTED` → `ACTIVE` → `INTEGRATING` →
`AWAITING_HM` → `MERGED` → `RELEASE_READY` → `AWAITING_H2` → `DEPLOYING` →
`OBSERVING` → `DELIVERED` (identical to the `happy_path` timeline in Stage
2.3 — that's exactly what to expect here, just driven by real GitHub
webhooks instead of scripted calls).

### 7.3 Negative tests to run for real

| Test | How | Expected |
|---|---|---|
| Patch under a standing mandate | Open an issue labeled `factory:patch` whose real diff stays inside `mandates/SM-patch.yaml`'s paths/line limit | No H1; goes straight to `ACTIVE` |
| Issue asking to modify `.github/workflows` | Ask for a change that would require editing a workflow file | Task/mission ends in `AWAITING_HX`; no PR is ever opened (same as `forbidden_path_blocked`, Stage 2.2) |
| Approval on a stale sha | Approve an HM packet after new commits landed on the mission branch (e.g. after a human push) | The decision is silently ignored (`StaleApproval`); mission stays at the same gate, no state advance |
| Human pushing to `factory/*` | `git push` directly to the mission's branch as a human | Recorded as a `human_intervention` event; that human is added to `editors` (so the no-self-approval rule excludes them from that mission's HM) |
| Production regression | Point `FACTORY_DEPLOY_HEALTH_CMD` at a check that fails after deploy | Flag killed, rollback executed, mission → `AWAITING_HX` (always — v1 has no automated fix path) |
| `kill-switch` | `factory kill-switch` while a mission is `ACTIVE` | Mission → `HELD`; `factory resume --mission <id>` brings it to `AWAITING_HX` for a fresh decision (Stage 2.2's `kill_switch_resume`) |

---

## Stage 8: Sign-off checklist

| §19.3 walkthrough / §13 floor item | Proven by | ✅ |
|---|---|---|
| 1. Happy path (H1→HM→H2→DELIVERED) | Stage 2 `happy_path`; Stage 7 real mission | ☐ |
| 2. Standing-mandate diff exceeds coverage → H1 | Stage 2 `coverage_exceeded` | ☐ |
| 3. H1 revise then decline → ARCHIVED | Stage 2 `h1_reject`; Stage 3.7 | ☐ |
| 4. Stale approval → consume fails, new round | Stage 2 `stale_approval_rejected`; Stage 7.3 | ☐ |
| 5. Replay an H2 after rollback → rejected | Stage 2 `replay_after_rollback` | ☐ |
| 6. Repair budget exhausted → HX | Stage 2 `repair_exhausted_hx` | ☐ |
| 7. Worker/controller crash, no duplicate effect | `tests/walkthroughs/test_walkthroughs.py::test_07_...` (Stage 1) | ☐ |
| 8. Approval timeout → backup → HELD, never approved | `test_08_approval_timeout_never_approves` (Stage 1) | ☐ |
| 9. Post-merge failure → auto-revert → REPAIRING | Stage 2 `post_merge_revert` | ☐ |
| 10. Production regression → rollback, HX | Stage 2 `prod_regression_rollback`; Stage 7.3 | ☐ |
| 11. Injection in issue body contained | `test_11_injection_in_issue_body_is_contained` (Stage 1); Stage 5.7 (egress) | ☐ |
| 12. `.github/workflows` edit → AC8 blocked; conftest edit → AC6 | Stage 2 `forbidden_path_blocked`; Stage 4.4; Stage 7.3 | ☐ |
| 13. Holdout exfiltration attempt → unreachable | `test_13_holdouts_unreachable_from_sandbox` (Stage 1) | ☐ |
| §13.1 #1 Prod credentials only in the deploy controller | Stage 6.4 (deploy hooks, not agent-reachable) | ☐ |
| §13.1 #2 Network-off execution; egress allowlist | Stage 1.3 (Docker sandbox tests); Stage 5.7 | ☐ |
| §13.1 #3 Protected records (AC6/AC8) | Stage 4.3/4.4; Stage 2 `forbidden_path_blocked` | ☐ |
| §13.1 #4 Holdouts in a separate repo/runner | Stage 6.1/6.2; `test_13_...` | ☐ |
| §13.1 #5 No agent outbound write path | Stage 3.3 (PR text generated by controller) | ☐ |
| §13.1 #6 Untrusted text is data | `test_11_...`; Stage 3.3 (packet lists untrusted inputs) | ☐ |
| §13.1 #7 Budgets enforced externally; kill switch | Stage 5.8 (LiteLLM); Stage 7.3 (`kill-switch`) | ☐ |
| §13.1 #8 Single-use approvals | Stage 3.4 (409 on token replay); Stage 2 `replay_after_rollback` | ☐ |
| §13.1 #9 One identity per bot | Stage 6.2 (two separate GitHub Apps) | ☐ |
| §13.1 #10 Supply chain (pinned versions) | Stage 5.3 (pinned `CLAUDE_CODE_VERSION`) | ☐ |

---

## Appendix

### Known v1 limitations (from `factory-controller/README.md`, verbatim in substance)

- The coordinator agent is bypassed — tasks come directly from the
  architect's `tasks[]`.
- The QA agent is not invoked automatically (only sandboxed checks + the
  reviewer agent are wired as independent verification).
- Fan-out is sequential, not concurrent (same join-barrier semantics, no
  wall-clock parallelism).
- A production regression **always** escalates to HX — no automated
  diagnosis/fix path in v1.
- Side-state timeouts fire in one step (no separate "warn, then act" delay
  beyond notifying the backup approver).
- The webhook queue and the approval-decision queue are both process-local,
  not persisted — a crash between enqueue and drain loses that one delivery
  (downstream side effects stay write-ahead-intent and single-use-approval
  protected regardless).
- Operation ids are keyed by revision (e.g.
  `push:{mission}:{content_hash}`), so a repaired revision after a
  revert/repair cycle is genuinely rebuilt/redeployed, not replayed from a
  stale receipt.
- A follow-up action that fails after its approval is consumed lands the
  mission in `AWAITING_HX`, not stuck (except a stale PR head at merge time,
  which goes back to `ACTIVE` with a repair task instead).
- Mission-scoped scratch state lives on the `Factory` instance plus
  `_cache.*` store events, not a dedicated field on `MissionRecord`.
- `SandboxPort.exec` has no stdin, so applying a diff during integration
  still writes into the sandbox's agent-writable workdir (into a
  `.factory-controller-tmp/` directory that's scrubbed before the next diff
  capture).
- `DockerSandbox` needs the host Docker socket to launch task containers,
  which `docker-compose.yml` does **not** wire up by default — see Stage 5
  and `docker-compose.docker-socket.yml`'s loud warning about what holding
  that socket grants.

### Cleanup commands

```bash
# Stage 1
docker stop factory-test-pg && docker rm factory-test-pg

# Stage 3
pkill -f "factory demo --serve"

# Stage 5
docker compose -f factory-controller/deploy/docker-compose.yml \
  --env-file factory-controller/deploy/.env -p factory-test down -v
docker rmi factory-sandbox:latest factory-controller:latest 2>/dev/null || true
rm -rf /tmp/factory-deploy-scratch /tmp/products-scratch /tmp/classify-repo /tmp/products-scratch

# General: never touch a container you didn't start for this guide
# (e.g. leave `agentic-bots-pg` alone).
docker ps -a
```

### Glossary

| Term | Meaning |
|---|---|
| **H1** | Human gate: authorize a mission before agents do real work |
| **HM** | Human gate: approve a merge |
| **H2** | Human gate: approve a release (digest) to production |
| **HX** | Human gate: resolve an exception (repair exhausted, forbidden diff, regression, kill-switch recovery) |
| **AC1–AC8** | Action classes: the category of a change that determines its gates (AC1 docs/tests, ... AC7 authority change, AC8 forbidden) |
| **Mandate** | Approved authority for work: outcome, scope, budget, merge/release policy, recovery plan, expiry, pinned kit/policy versions |
| **Standing mandate** | A mandate covering recurring work, whose coverage is checked deterministically by the controller (labels, action classes, paths, diff size) |
| **Holdout** | A black-box acceptance scenario, in a separate repo, run by a separate identity against the deployed staging digest — agents never see it |
| **Fencing (token)** | A monotonically-increasing value used at CAS-consume time so a stale or replayed approval/operation can never be applied twice |
| **Join barrier** | Every required parallel task output must pass before integration proceeds |
| **Write-ahead intent** | A side effect recorded before it runs, so a crash can be reconciled without a duplicate effect |
