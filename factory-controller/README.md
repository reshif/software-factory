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
deploy/                       docker-compose.yml, controller.Dockerfile, .env.example (B7),
                               plus B3's litellm/, egress-proxy/, sandbox/ configs
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
both wired to fakes, seeds one feature mission awaiting H1, and prints a
signed inbox link for each open approval so you can approve it in a browser
at `http://127.0.0.1:8080/inbox/...`.

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

`tests/pipeline/test_orchestrator_auto_merge.py` additionally exercises the
webhook-driven `auto` HM path directly (at `experimental` risk, since none of
the required scenarios reach it at `standard`).

## CLI

```text
factory serve                 run the FastAPI app
factory worker                run the background worker loop
factory migrate                apply Postgres migrations (no-op in local/in-memory mode)
factory validate-kit           validate the factory-kit policies
factory products validate      validate every <product>/factory.yaml + mandates
factory classify --git BASE..HEAD [--repo PATH]
factory gates --class AC4 --profile standard [--level L3] [--protected]
factory inbox-link --request REQ --approver @x --roles tech_lead
factory demo [--scenario NAME|all] [--serve]
```

## Production mode

```bash
cp factory-controller/deploy/.env.example factory-controller/deploy/.env
# fill in .env, then from the repo root:
docker compose -f factory-controller/deploy/docker-compose.yml --env-file factory-controller/deploy/.env up
```

`deploy/docker-compose.yml` runs Postgres, the LLM gateway (B3's
`deploy/litellm/config.yaml`), the egress allowlist proxy (B3's
`deploy/egress-proxy/`), and one `controller` (`factory serve`) plus one
`worker` (`factory worker`) container built from `deploy/controller.Dockerfile`.
`.env.example` documents every `FACTORY_*` variable `config.py` reads.
`FACTORY_KIT_DIR`/`FACTORY_PRODUCTS_DIR` mount the kit and your product
configs read-only, separate from the controller image, so a kit or product
change never needs a rebuild.

`wiring.py` is the only place that assembles concrete adapters. A few
production adapters need configuration `Settings` doesn't carry yet (see
"Requests to orchestrator" below); those are wired to a small `_NotConfigured`
stand-in that raises only when actually called, so `factory serve`/`factory
worker` still start and work for everything that *is* configured — a
holdout run or deploy that hits it simply fails closed, exactly like a real
failure would.

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
- **The webhook queue is process-local.** `Factory.enqueue_webhook`/
  `drain_webhooks` decouple the HTTP handler (fast: verify + parse + enqueue)
  from the worker loop (which does the actual agent/GitHub/deploy work), but
  the queue itself isn't persisted — a crash between "enqueued" and "drained"
  loses that one webhook delivery (GitHub's own retry may or may not resend
  it). Every side effect that follows (push, merge, deploy) is still
  write-ahead-intent protected and single-use-approval protected regardless.
- **Intent reconciliation is a visibility pass, not a generic replayer.**
  `worker._reconcile_intents` logs anything still pending rather than
  guessing a per-operation-kind retry. The side effects that most need
  exactly-once semantics don't depend on it: HM/H2 merges and deploys are
  independently protected by the approval's single-use consume and, for
  deploys, the target's own fencing-token check.
- **Mission-scoped scratch state (the gateway key, the architect's task
  plan, per-task diffs, the sha awaiting an auto-merge/post-merge check) is
  cached in the `Factory` instance**, with the fields that matter for restart
  recovery also durably recorded as `_cache.*` events (ignored by
  `telemetry.metrics`, which only reads its documented kinds). A cleaner home
  would be a small structured field on `MissionRecord`/`TaskRecord`, but
  those are shared, orchestrator-only files this build didn't touch.

## Requests to orchestrator

Precise, worked-around-where-possible gaps found while integrating Wave 1:

1. **`FakeGitHub` has no way to seed a branch's real head commit.**
   `head_commit` fabricates an opaque sha the first time it's asked about a
   branch, unrelated to any real git repository, but `SandboxPort.create`
   needs an actual checkoutable commit. `add_issue`/`set_checks`/`add_review`
   exist for exactly this kind of test seeding; a `set_head(repo, branch,
   sha)` helper would too. Worked around in `demo/harness.py` by writing
   `FakeGitHub._branches` directly (commented, with this note).
2. **No `RepoMirror` port/adapter.** `SandboxPort.create`/`DockerSandbox`
   assume `repos_root/<repo>` is already a git mirror ("the github module
   clones it into repos_root", per `DockerSandbox`'s own docstring), but
   nothing in `github/` actually maintains one — `RestGitHub.push_diff` only
   ever clones into a throwaway directory it deletes afterward. B7
   deliberately does not add host-side git cloning to fill this gap (it would
   mean the orchestrator holding GitHub credentials, which §13.1 reserves for
   the push/merge bots specifically); `pipeline.orchestrator._ensure_mirror`
   fails closed with a clear message instead. Production needs a mirror
   provisioning step (cron job, init container, or a new port) before a
   product can be sandboxed.
3. **`Settings` has no holdout-runner credentials.** `WorkflowHoldoutRunner`
   needs its own read-only GitHub identity plus a workflow file name, scoped
   to one repo; a multi-product deployment needs one per product. Add
   `FACTORY_HOLDOUT_TOKEN` / `FACTORY_HOLDOUT_WORKFLOW_FILE` (and a
   per-product override) to `config.py`.
4. **`Settings.deploy_command` is one string; `CommandDeployTarget` needs
   five** (`build`/`deploy`/`health`/`rollback`/`url`). Replace it with
   `FACTORY_DEPLOY_{BUILD,DEPLOY,HEALTH,ROLLBACK,URL}_COMMAND`.
5. **`factory.yaml` has no owners-to-Slack-id mapping.** `ApproverContact`
   requires `slack_user_id` for `chat.postMessage` DMs, but the schema only
   has `owners: {role: "@login"}`. Until it grows something like
   `owners_slack: {po: "U0123..."}`, `wiring._build_roster` always returns an
   empty roster (no DMs; the channel-wide FYI still works if a webhook URL is set).
6. **`webhooks.parse_event` only turns a `push` into `PushToDefault` for the
   repo's actual default branch.** Final draft §7 step 13's human-intervention
   signal is specifically "a push to `factory/*` by a non-bot user" — a push
   to a mission branch, not `main` — which the current parser silently
   ignores (`logger.debug("ignoring push to %s...")`). `on_push_to_default`
   therefore only detects a human pushing straight to `main`. Recognizing
   pushes to non-default branches (at least ones matching `factory/*`) would
   close this.
7. **`DockerSandbox` needs the host Docker socket to run per-task
   containers**, which `deploy/docker-compose.yml` mounts into `controller`/
   `worker` — the standard, but blunt, way to run containers from a
   container. A locked-down Docker-in-Docker or a dedicated sandbox executor
   (gVisor/Firecracker) would be a meaningfully smaller blast radius for a
   compromised controller process.
