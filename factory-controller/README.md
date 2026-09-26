# factory-controller

The software factory: autonomous agents do the engineering work, a small
deterministic core enforces the rules, and humans only approve or reject.
Behavior is fully defined in
[`docs/software-factory-final-draft.md`](../docs/software-factory-final-draft.md)
(cited below as `§N`); this README explains how the pieces in this repo
implement it and how to run the result.

## What's here

```text
src/factory/
├── models.py, ports.py, config.py, clock.py, globs.py   shared contracts (§12.1, §12.3)
├── policy/, controller/     the deterministic core: gate table, coverage, approvals,
│                            mission/task state machines, write-ahead intents (§6, §10, §14)
├── store/                   StateStore: in-memory + Postgres (B1)
├── github/                  GitHub App auth, REST client, webhooks, fake (B2)
├── runtime/, sandbox/, budget/   Claude Agent SDK adapter, Docker/local sandboxes,
│                                 LiteLLM per-mission budgets, and their fakes (B3)
├── inbox/                   the approval inbox: signed links, HTML router, Slack (B4)
├── verification/, release/, telemetry/   checks, evidence, holdouts, deploy/flags,
│                                         auto-revert, DORA + autonomy metrics (B5)
├── pipeline/                 orchestrator, product registry, agent I/O, tool guard (B7)
├── wiring.py                 assembles a `Factory` from `Settings` (local -> fakes,
│                              production -> real adapters) (B7)
├── app.py                    FastAPI: /webhooks/github, /inbox, /healthz, /metrics (B7)
├── worker.py                 background loop: webhooks, timeouts, tasks, intents,
│                              observation windows (B7)
├── cli.py                    the `factory` command (B7)
└── demo/                     scripted end-to-end scenarios on fakes (B7)
deploy/                       docker-compose.yml (+ the docker-socket opt-in override),
                               controller.Dockerfile, entrypoint.sh, postgres-init/,
                               .env.example (B7), plus B3's litellm/, egress-proxy/,
                               sandbox/ configs
tests/
├── <module>/                 unit tests per module
├── walkthroughs/             the 13 Phase-1 acceptance scenarios (§19.3) on the bare
│                              controller/policy core
├── pipeline/                 unit tests for products, the tool guard, agent I/O, packets,
│                              and the webhook-driven "auto" HM path
└── e2e/                      every demo scenario, run as a pytest test
```

Every external dependency sits behind a `Protocol` in `ports.py`. Each port has a
real adapter and a `Fake...`/`Recording...` adapter with the same semantics
(§12.2). `factory.pipeline` and `wiring.py` are the only places that assemble a
concrete adapter; the orchestrator itself only imports ports plus the shared
`controller`/`policy` core.

## Quickstart

```bash
cd factory-controller
uv sync
uv run pytest                       # full suite, all fakes/mocks, no network
uv run factory demo --scenario all  # every §3 demo scenario, readable timelines
uv run factory --help
```

`uv run factory demo --serve` starts the FastAPI app plus a background worker,
both wired to fakes, and seeds one feature mission. It prints a signed inbox
link each time an approval opens (H1, then HM, then H2), simulates green CI and
fast-forwards the observation window, so you can take the mission all the way
to DELIVERED in a browser at `http://127.0.0.1:8080/inbox/...`.

## The mission lifecycle (what `pipeline/orchestrator.py` actually does)

`Factory` implements final draft §7/§14.1 end to end:

1. **Intake + coverage.** A `factory:patch`/`factory:feature` label starts a
   mission (idempotent per work item). The intake agent suggests a lane and
   class; the controller alone decides standing-mandate coverage
   (`controller.coverage`, §6.1) — covered work skips straight to admission,
   everything else goes to **discovery** (the architect agent, on a read-only
   sandbox checkout) and an **H1** decision packet.
2. **Decisions.** `Factory.decide` is the one function the inbox and GitHub
   PR-review webhooks call. Quorum met -> the approval is consumed atomically
   (CAS + fencing token, `controller.approvals`) and the mission advances;
   `revise` voids the round (and, at H1, re-runs discovery); `cancel`
   archives/declines.
3. **Admission.** A per-mission LiteLLM budget key, and one schema-valid
   task contract per architect task (validated against
   `factory-kit/schemas/task-contract.schema.json`).
4. **Tasks.** Each task gets its own sandbox at the mission's base commit, runs
   the implementer agent, captures and classifies the diff, checks standing
   coverage again (exceeding it re-routes to discovery), runs the product's
   sandboxed checks, and repairs (resume with the trimmed failure) up to the
   lane's `repair_attempts`. An AC8 (forbidden-path) diff or an exhausted
   repair budget escalates to **HX**, never gets pushed.
5. **Integrate.** Once every task is DONE, their diffs are applied onto a
   fresh sandbox at the base commit, checks + the reviewer agent run on the
   *combined* diff, evidence is built, and the push bot pushes the branch and
   opens a controller-generated PR.
6. **HM.** `auto` waits for CI on the pushed commit (a `check_suite`
   webhook) and merges with no human; otherwise an HM packet is created and a
   `pull_request_review` APPROVED event (or the inbox) satisfies it.
7. **Post-merge.** A failing check run on the merge commit triggers an
   immediate auto-revert and a repair task; passing evidence moves to
   **RELEASE_READY**.
8. **Release candidate.** Build once, deploy to staging, run the holdout
   runner, complete the evidence, and resolve **H2** (one mission per release
   in v1) — `standing` deploys immediately, otherwise an H2 packet.
9. **Deploy + observe.** H2 consumed -> deploy to production behind the
   mission's flag at 100%, then **OBSERVING**. Healthy through the
   observation window -> **DELIVERED**. A regression kills the flag, rolls
   back, invalidates open approvals for that artifact, and — v1 has no
   automated diagnosis/fix path yet — always escalates to **HX**.
10. **Telemetry.** Every transition, decision, push, deploy and cost is an
    `append_event` in the exact kinds/payloads `telemetry/metrics.py`
    documents, so `/metrics` and `factory.telemetry.metrics.from_store` work
    against real mission history.

### The tool guard (containment)

Agents always run with `allowed_tools=()`, so every tool call goes through
`pipeline.tool_guard.ToolGuard` as `on_tool_approval`. It allows
`Read`/`Glob`/`Grep`/`Edit`/`Write` only when the resolved path (symlinks and
`..` included) stays inside the task's sandbox workdir; `Edit`/`Write`
additionally need the path inside the task's `owned_paths` and outside the
floor/product protected or forbidden paths. Everything else — **Bash above
all** — is denied outright: the controller runs code, never the agent, via
`SandboxPort.exec(network=False)`.

**v1 containment limitation.** The guard filters tool arguments inside the
*same OS process* as the controller. It is not a security boundary against a
hostile runtime process itself — that boundary is `DockerSandbox` (network
off, no credentials, `.git` kept outside the mounted workdir so nothing an
agent writes can redirect a host git command). Phase 3 moves the runtime
process itself into that container, at which point the guard becomes defense
in depth rather than the only check. `FakeRuntime` (used by the demo and
`tests/e2e`) doesn't simulate tool calls at all — it just writes files
directly — so the guard's own behavior is exercised by `tests/pipeline/
test_tool_guard.py`, not by the demo.

## Demo scenarios (`src/factory/demo/`)

`demo/harness.py` copies `factory-kit/templates/backend-service` into a real
temporary git repo (registered as `org/backend-service`), extends its own
copy of `factory.yaml` to also require `review_agent`/`holdout_blackbox`, and
wires a `Factory` with every port on its fake (`wiring.build_factory_and_adapters`,
`local` mode). `demo/scenarios.py` scripts `FakeRuntime` per role and drives
missions through the same calls a webhook/inbox would make. Every scenario
asserts its own end state; `tests/e2e/test_demo_scenarios.py` runs all of
them as pytest tests.

| Scenario | What it proves |
|---|---|
| `happy_path` | feature lane: H1 -> HM -> H2 -> DELIVERED |
| `patch_standing` | a covered patch skips H1; HM/H2 per the gate table |
| `coverage_exceeded` | a patch's actual diff exceeds its mandate -> re-routed to H1 |
| `h1_reject` | H1 revise (fresh discovery), then decline -> ARCHIVED |
| `repair_then_pass` | a failing check is repaired via `resume` and then passes |
| `repair_exhausted_hx` | repair attempts exhausted -> boundary -> AWAITING_HX |
| `forbidden_path_blocked` | an AC8 diff (`.github/workflows/**`) is blocked before push |
| `post_merge_revert` | CI fails on `main` after merge -> auto-revert -> repair task, ACTIVE |
| `prod_regression_rollback` | a production regression kills the flag, rolls back, escalates to HX |
| `stale_approval_rejected` | the mission's state moves after H1 is requested -> consume rejects it |
| `replay_after_rollback` | replaying an already-consumed H2 approval is rejected |
| `human_push_recorded` | a human pushing to a mission's `factory/*` branch is recorded and becomes an editor |
| `second_release_standing` | two consecutive standing-mandate releases each deploy their own artifact (fresh fencing tokens) |
| `kill_switch_resume` | kill switch -> HELD, resume -> HX, defer -> BLOCKED, unblock -> HX approve -> work continues |

`tests/pipeline/test_orchestrator_auto_merge.py` additionally exercises the
webhook-driven `auto` HM path directly (at `experimental` risk, since none of
the required scenarios reach it at `standard`), and
`tests/pipeline/test_red_team_2.py` exercises the fixes from a second
integration-layer red-team pass (discovery budget keys, out-of-scope/symlink
diff rejection, GitHub check-name completeness, HM's binding to the exact
pushed/reviewed sha, `decide()` only enqueueing, and a repaired revision after
a post-merge revert being genuinely rebuilt/re-deployed rather than replaying
a stale receipt) directly.

## CLI

```text
factory serve                  run the FastAPI app (uvicorn with access_log=False --
                                inbox links carry a signed token in the query string)
factory worker                 run the background worker loop
factory migrate                apply Postgres migrations (no-op in local/in-memory mode)
factory validate-kit           validate the factory-kit policies
factory products validate      validate every <product>/factory.yaml + mandates
factory classify --git BASE..HEAD [--repo PATH]
factory gates --class AC4 --profile standard [--level L3] [--protected]
factory inbox-link --request REQ --approver @x --roles tech_lead
factory kill-switch            emergency stop: every non-terminal mission -> HELD
factory resume --mission ID    resume one HELD mission
factory unblock --mission ID   clear one BLOCKED mission
factory github setup --repo OWNER/NAME [--dry-run]
                                apply (or print) the recommended branch-protection
                                ruleset for one product repo
factory demo [--scenario NAME|all] [--serve]
```

Without a Slack bot token configured (`FACTORY_SLACK_BOT_TOKEN`), the
controller falls back to a logging notifier — nothing DMs approvers a link.
In that mode (and for a one-off, e.g. handing someone a link outside their
normal Slack cadence), mint a signed, request-bound link yourself with
`factory inbox-link --request REQ --approver @approver --roles tech_lead` and
send it to them directly.

## Production mode

```bash
cp factory-controller/deploy/.env.example factory-controller/deploy/.env
# fill in .env (FACTORY_* Settings values only -- see below for secrets), then
# create the SIX secret files docker-compose.yml references:
mkdir -p factory-controller/deploy/secrets
echo "<a strong random password>"       > factory-controller/deploy/secrets/postgres_password.txt
echo "<a different strong password>"    > factory-controller/deploy/secrets/litellm_password.txt
echo "<your Anthropic API key>"         > factory-controller/deploy/secrets/anthropic_api_key.txt
cp /path/to/push-app.pem  factory-controller/deploy/secrets/push-app.pem
cp /path/to/merge-app.pem factory-controller/deploy/secrets/merge-app.pem
python -c "import secrets; print(secrets.token_urlsafe(32))" \
                                        > factory-controller/deploy/secrets/llm_gateway_master_key.txt
# from the repo root:
docker compose -f factory-controller/deploy/docker-compose.yml --env-file factory-controller/deploy/.env up
```

All six secrets, in one place:

| # | File | What it is |
|---|---|---|
| 1 | `secrets/postgres_password.txt` | the `factory` Postgres role's password |
| 2 | `secrets/litellm_password.txt` | the separate `litellm` Postgres role's password (its own database, never the controller's tables) |
| 3 | `secrets/anthropic_api_key.txt` | the real Anthropic API key, held only by the LiteLLM gateway |
| 4 | `secrets/push-app.pem` | the push-bot GitHub App's private key |
| 5 | `secrets/merge-app.pem` | the merge-bot GitHub App's private key (a SEPARATE App from the push bot, final draft §11) |
| 6 | `secrets/llm_gateway_master_key.txt` | LiteLLM's admin `master_key`, shared by `litellm` (as `LITELLM_MASTER_KEY`) and the controller/worker (as `FACTORY_LLM_GATEWAY_MASTER_KEY`, exported by `entrypoint.sh`) for minting per-mission budget keys |

### The two GitHub Apps

Two separate App identities, each with only what it needs (final draft §11,
§13.1 #9, §13.2) — **neither one is an admin on the repo**; that's a human,
using `factory github setup` once to install the branch-protection ruleset:

| App | Permissions | Never |
|---|---|---|
| push bot | Contents: Read & write · Pull requests: Read & write · Issues: Read · Checks: Read · Metadata: Read | **Workflows** (can't touch `.github/workflows/**` — that's AC8, refused before it ever reaches GitHub) |
| merge bot | Contents: Read & write · Pull requests: Read & write · Metadata: Read | Issues, Checks — it only ever calls `PUT /pulls/{n}/merge` |

Subscribe the App(s) to these webhook events (`POST /webhooks/github`,
verified via `X-Hub-Signature-256`): **Issues**, **Pull request review**,
**Check suite**, **Push**. Everything else is explicitly ignored by
`github/webhooks.py`.

### Populating the kit/products/sandbox-image volumes

`docker-compose.yml` mounts `kit-data`/`products-data` read-only into
`controller`/`worker` at `/var/lib/factory/{kit,products}`
(`FACTORY_KIT_DIR`/`FACTORY_PRODUCTS_DIR`) — Compose named volumes, not baked
into the image, so a kit or product-config change never needs a rebuild. Get
your content into them once (a running container, or any way you like to
populate a named volume) before `up`, e.g.:

```bash
docker compose -f factory-controller/deploy/docker-compose.yml run --rm \
  -v "$(pwd)/factory-kit:/src/kit:ro" -v "$(pwd)/products:/src/products:ro" \
  --entrypoint sh controller -c "cp -r /src/kit/. /var/lib/factory/kit/ && \
                                 cp -r /src/products/. /var/lib/factory/products/"
```

Re-run this (or your own sync) whenever `factory-kit/` or a product's
`factory.yaml`/`mandates/` changes — the kit gate (final draft §13.3) governs
what's allowed to change, not this file.

**The sandbox image** (`factory-sandbox:latest`, `FACTORY_SANDBOX_IMAGE`) is
built separately — `DockerSandbox` shells out to `docker run <image>` at
runtime, so it's never a compose service:

```bash
docker build -t factory-sandbox:latest -f factory-controller/deploy/sandbox/Dockerfile factory-controller/deploy/sandbox
```

### `factory github setup`

Once the two Apps are installed on a product repo, apply the recommended
branch-protection ruleset (required checks, required CODEOWNERS review, a
merge restriction to the merge-bot App, and turning off "Allow GitHub Actions
to create and approve pull requests") with:

```bash
factory github setup --repo org/backend-service --dry-run   # print the plan first
factory github setup --repo org/backend-service             # apply it
```

It reads `FACTORY_MERGE_APP_ID` plus the push bot's App credentials from
`Settings` (the push bot needs admin on the repo to apply a ruleset) — see
`src/factory/github/setup.py`.

### Emergency stop and recovery

`factory kill-switch` moves every non-terminal mission to `HELD` immediately
— no worker acts on a `HELD` mission. `factory resume --mission ID` brings
one back; `factory unblock --mission ID` clears one stuck `BLOCKED` (both
target a specific mission, not a fleet-wide switch).

`deploy/docker-compose.yml` runs Postgres, the LLM gateway (B3's
`deploy/litellm/config.yaml`), the egress allowlist proxy (B3's
`deploy/egress-proxy/`), and one `controller` (`factory serve`) plus one
`worker` (`factory worker`) container built from `deploy/controller.Dockerfile`.
`.env.example` documents every `FACTORY_*` variable `config.py` reads.
`FACTORY_KIT_DIR`/`FACTORY_PRODUCTS_DIR` mount the kit and your product
configs read-only, separate from the controller image, so a kit or product
change never needs a rebuild.

**Secrets never live in `.env`.** The Postgres and LiteLLM database passwords,
the Anthropic API key, and the two GitHub App private keys are Compose
`secrets:` — plain files under `deploy/secrets/` (gitignore this directory in
your own deployment fork; nothing under it should ever be committed) that
Compose mounts read-only into exactly the containers that need them.
`deploy/entrypoint.sh` builds `FACTORY_DATABASE_URL` from the mounted
Postgres-password file at container startup; `litellm`'s command does the same
for its own Anthropic key and (separate, red-team-hardened) database
credential. None of this shows up in `docker inspect`/`compose config`'s
plain-text environment listing the way a `${VAR}`-interpolated secret would.
The LiteLLM admin master key is a Compose secret too
(`secrets/llm_gateway_master_key.txt`): litellm reads it as `LITELLM_MASTER_KEY`,
and `entrypoint.sh` exports it as `FACTORY_LLM_GATEWAY_MASTER_KEY` for the
controller and worker. `deploy/secrets/` and `deploy/.env` are gitignored.

**Running sandboxed tasks needs `DockerSandbox` to launch containers**, which
this compose file does *not* wire up by default (see its own "Running
sandboxed tasks" comment): the recommended production path is a separate,
rootless Docker-in-Docker (or gVisor/Sysbox) daemon reached over TCP+TLS,
never a mounted socket. For a quick/local production trial only, `docker
compose -f docker-compose.yml -f docker-compose.docker-socket.yml up` opts
into mounting the host's real Docker socket — holding that socket is
equivalent to root on the host, so only do this on a host you're prepared to
treat as fully accessible to whatever `controller`/`worker` run.

`wiring.py` is the only place that assembles concrete adapters. `DeployTarget`
fails FAST at startup if any of its 5 hook commands is missing, and
`HoldoutRunner` does the same for any product whose `verification.required`
names `holdout_blackbox` with no `FACTORY_HOLDOUT_TOKEN` configured — a
mis-configured release or holdout path is a deploy-time bug you want to catch
at startup, not the first time a mission reaches it.

## v1 scope notes (deliberate, not oversights)

- **The coordinator agent is bypassed.** Tasks are created directly from the
  architect's `tasks[]`, as the build spec explicitly allows for v1.
- **The QA agent is not invoked automatically.** The build spec's numbered
  lifecycle (§3 step 5) names the sandboxed checks and the reviewer agent as
  the two wired independent-verification layers; QA isn't among them.
- **Fan-out is sequential, not concurrent.** Up to `max_parallel_tasks` READY
  tasks are released, but run one at a time in-process. The AND join-barrier
  semantics are identical to true concurrency; only wall-clock parallelism
  differs.
- **A production regression always escalates to HX.** There's no automated
  diagnosis/fix path before the Phase-4 ops agent, matching final draft §7's
  "with no automated fix path -> AWAITING_HX" literally.
- **Side-state timeouts fire in one step.** `mission_fsm.TIMEOUTS` gives one
  deadline per side state ("escalate to the backup approver, then fire the
  event"); the worker notifies the backup and fires the timeout event at that
  same deadline rather than inventing a second, unspecified interval.
- **The webhook queue and the approval-decision queue are both process-local.**
  `Factory.enqueue_webhook`/`drain_webhooks` decouple the HTTP handler (fast:
  verify + parse + enqueue) from the worker loop, which does the actual
  agent/GitHub/deploy work via `dispatch_webhooks`. `decide()` follows the
  exact same pattern: it only records the decision (`ApprovalStore.decide`,
  fast and local) and enqueues the request id; `process_approvals` (also
  worker-driven) does the atomic consume and the follow-up action, so a human
  clicking "approve" in the inbox never blocks on an agent run or a GitHub
  call either. Neither queue is persisted — a crash between "enqueued" and
  "drained" loses that one delivery (GitHub's own webhook retry may or may not
  resend it; a lost decision just means the approver's click didn't take and
  they see the packet still open). Every actual side effect downstream (push,
  merge, deploy) is still write-ahead-intent protected and, for gated ones,
  single-use-approval protected regardless.
- **Operation ids are keyed by revision, not just by mission**, e.g.
  `push:{mission}:{content_hash}`, `merge:{mission}:{pushed_sha}`,
  `build:{mission}:{merge_sha}`, `deploy_prod:{mission}:{artifact}`: a repaired
  revision after a revert/repair cycle gets its OWN operation id, so
  `execute_once` builds/pushes/merges/deploys the new content instead of
  replaying a receipt from a previous revision. Real probes are used where the
  port makes one possible (e.g. a deploy retry that hits the target's own
  fencing-token rejection is treated as "already applied", not a failure);
  where the port has no way to positively confirm an effect already happened
  (there is no "is this PR already merged with this head" read, for instance),
  the probe is `lambda: None` and a retry is idempotent enough on the fake/real
  adapters in use not to double-apply.
- **A follow-up action that fails after its approval is consumed lands the
  mission in AWAITING_HX**, not stuck: `_on_quorum` wraps the post-consume
  action and, on any unexpected exception, escalates with the failure as the
  HX packet's reason. A stale PR head at merge time is handled more
  specifically — it's refused, logged as a `human_intervention` event, and the
  mission goes back to ACTIVE with a repair task, since that specifically
  means new work landed and needs re-integrating, not a human exception.
- **Mission-scoped scratch state (the discovery and mission gateway keys, the
  architect's task plan, per-task diffs, the pushed/merge sha, the PR number,
  the requester login) is cached in the `Factory` instance**, with every field
  that matters for restart recovery or a security rule (no-self-approval
  needs the requester to survive a restart) also durably recorded as
  `_cache.*` store events (ignored by `telemetry.metrics`, which only reads
  its documented kinds). A cleaner home would be a small structured field on
  `MissionRecord`/`TaskRecord`, but those are shared, orchestrator-only files
  this build didn't touch.
- **`SandboxPort.exec` has no stdin.** Applying a task's diff during
  integration therefore still has to write the patch somewhere inside the
  sandbox workdir (an agent-writable directory) rather than truly outside it;
  it goes into a dedicated `.factory-controller-tmp/` directory that's always
  scrubbed before the next `capture_diff` (`_scrub_controller_artifacts`), so
  the patch itself can never appear as a change in a pushed diff, but a stdin
  parameter on `SandboxPort.exec` would let this avoid the workdir entirely.

## Requests to orchestrator

The first integration pass's gaps (`FakeGitHub` head-seeding, the missing
`RepoMirror` port, `Settings` fields for the holdout runner/deploy
commands/Slack ids, and the webhook parser's default-branch-only push
handling) were all resolved by B2's and this build's gap wave: `wiring.py`
now builds a real `GitMirror`/`FakeMirror`, `CommandDeployTarget`/
`WorkflowHoldoutRunner` from real `Settings` fields (failing fast at startup
when required config is missing), `_build_roster` reads `factory.yaml`
`slack_ids`, and `on_push_to_branch` handles `webhooks.PushToBranch` for a
human pushing to a `factory/*` mission branch. What's left:

1. **`SandboxPort.exec` has no stdin.** See "v1 scope notes" above --
   `_scrub_controller_artifacts` covers the same safety property a different
   way, but a stdin parameter would let integration hand the sandbox its patch
   without writing anything into the agent-writable workdir at all.
2. **`DockerSandbox` needs to launch a container per task**, which holding the
   host's Docker socket (the standard way to run containers from a container)
   grants effectively host-root access to do. `docker-compose.yml` does NOT
   wire this up by default; `docker-compose.docker-socket.yml` is an explicit,
   loudly-commented opt-in override for it. The better production answer --
   a separate, rootless Docker-in-Docker (or gVisor/Sysbox) daemon reached over
   TCP+TLS, never a mounted socket -- is documented in both files but not
   built here, since it needs infrastructure (a second host/node, or a
   sandboxing service) outside a docker-compose file's reach.
