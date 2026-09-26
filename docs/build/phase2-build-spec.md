# Phase 2 Build Spec (for builder agents)

**Source of truth for behavior:** [../software-factory-final-draft.md](../software-factory-final-draft.md) (Revision 2). Cite it as `§N`.
**Goal:** a complete, runnable factory that a human can test end to end:
- **locally** with `factory demo`, using fakes for GitHub, the LLM, deploy and Slack; and
- **for real** against GitHub + Anthropic + a staging target, with the same code and the real adapters.

---

## 1. Rules for every builder (non-negotiable)

1. **Stay in your ownership paths** (§3). Never edit another module's files.
2. **Never edit shared files.** These are:
   - `src/factory/{models,ports,config,clock,globs}.py`
   - `src/factory/policy/**`
   - `src/factory/controller/**`
   - `pyproject.toml`, `uv.lock`
   - `docs/software-factory-final-draft.md`
   - `.github/**`

   If you need a change or a new dependency, **stop and put it in your report** under "Requests to orchestrator".
3. **Implement the ports exactly** as defined in `src/factory/ports.py`. Use the models in `src/factory/models.py`. Don't create parallel model types for the same concept.
4. **Every port gets two adapters:** a real one and a `Fake…` with the same semantics. Tests and `factory demo` use the fakes. **Tests must never touch the network, real GitHub, the Anthropic API or Slack.** Mock HTTP with `respx` or `httpx.MockTransport`.
5. **Security floor (§13) applies in code:**
   - Fail closed: missing, skipped, neutral or unknown results are failures.
   - Timeouts never approve.
   - Untrusted text (issue bodies, PR text, agent output) is data. Escape it in HTML. Never execute it. Never pass it where it becomes instructions for a privileged component.
   - Code execution defaults to **no network**.
   - No secrets in logs, exceptions or evidence.
   - Agents never get git, deploy or production credentials.
6. **Quality:**
   - Python ≥ 3.11, type hints on public functions, docstrings where behavior isn't obvious.
   - No dead code, no TODO stubs. The only exception is the Codex adapter, which is explicitly Phase 3.
   - No print debugging. Use `logging.getLogger(__name__)`.
   - Match the style of the existing `src/factory/controller/*.py`.
7. **Tests:**
   - Put them under `tests/<your-module>/`.
   - Mark Postgres tests `@pytest.mark.postgres` and skip them unless `FACTORY_TEST_DATABASE_URL` is set.
   - Mark Docker tests `@pytest.mark.docker` and skip them unless `docker info` succeeds.
   - Run the **full** suite before finishing: `cd factory-controller && uv run pytest`. It must be green.
8. **Hygiene:**
   - No stray files: no scratch scripts, `.orig`, notes, caches or generated output committed.
   - No new top-level directories except those assigned to you.
   - Keep `README`s accurate for what you built.
9. **Git:**
   - You are in your own worktree. Create branch `build/<module>` from the current HEAD.
   - Make small, meaningful commits. End each message with `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.
   - **Don't push. Don't merge.**
10. **Final report** (your last message). It must include:
    - branch name and commit list
    - files added
    - how each requirement below is met
    - the exact test command and result line
    - deviations
    - requests to the orchestrator

---

## 2. Package map (final state)

```text
factory-controller/
├── src/factory/
│   ├── models.py ports.py config.py clock.py globs.py     (shared: orchestrator only)
│   ├── policy/  controller/                               (shared: orchestrator only)
│   ├── store/          B1: memory + Postgres StateStore, migrations
│   ├── github/         B2: App auth, REST client (push bot / merge bot), webhooks, fake, repo setup
│   ├── runtime/        B3: Claude Agent SDK adapter, fake runtime, Codex stub (P3)
│   ├── sandbox/        B3: Docker sandbox (network off), local sandbox, diff capture
│   ├── budget/         B3: LiteLLM per-mission keys, fake gateway
│   ├── inbox/          B4: approval inbox (FastAPI router, HTML), signing, Slack notifier, fake notifier
│   ├── verification/   B5: fail-closed checks, local check runner, reviewer-agent check, evidence, holdout client
│   ├── release/        B5: deploy targets (command + fake), flags, auto-revert
│   ├── telemetry/      B5: metrics (DORA 5, interventions, approval latency), optional OTel
│   ├── pipeline/       B7: orchestrator (mission lifecycle), agent-output parsing, packet builder
│   ├── wiring.py       B7: build adapters from Settings (local -> fakes)
│   ├── app.py          B7: FastAPI app: /webhooks/github, /inbox, /healthz, /metrics
│   ├── worker.py       B7: background loop (timeouts, dispatch, reconciliation)
│   ├── cli.py          B7: `factory` command
│   └── demo/           B7: scripted end-to-end scenarios on fakes
├── migrations/         B1
├── deploy/             B3: litellm/, egress-proxy/, sandbox/Dockerfile · B7: docker-compose.yml, Dockerfile
└── tests/<module>/
factory-kit/
├── plugins/factory-core/   B6: Claude Code plugin (agents, skills, hooks)
├── guidance/               B6: base.md + render.py -> CLAUDE.md / AGENTS.md
├── templates/              B6: backend-service/ (golden path), holdouts-repo/
├── workflows/              B6: reusable ci-pr.yml (no secrets), deploy.yml (OIDC)
└── schemas/agent-outputs.schema.json   B6
```

---

## 3. Module assignments

### B1: State store (`src/factory/store/`, `migrations/`, `tests/store/`)
- `MemoryStateStore`: implements `StateStore`, using `controller.approvals.ApprovalStore` and `controller.intents.IntentLog`.
- `PostgresStateStore` (psycopg 3):
  - Tables: missions, tasks, evidence, packets, events, approvals, approval_decisions, intents.
  - `update_mission` is a CAS: `UPDATE … WHERE state_version = %s RETURNING`. It raises `ConcurrentUpdate`.
  - Approvals keep **identical semantics** to the reference `ApprovalStore`:
    - quorum, security role, editor/requester rules, revise/cancel voids, expiry means "start before";
    - atomic single-use consume, with the fencing token from a Postgres **sequence**;
    - `invalidate_for_artifact`, `list_open`.
  - Intents use write-ahead semantics.
- `migrations/NNN_*.sql` plus `apply_migrations(conn)`. The migrations must be idempotent.
- `open_store(settings) -> StateStore`: memory if `database_url` is unset.
- **Contract tests** run the **same test functions against both stores**. The Postgres run is skipped without `FACTORY_TEST_DATABASE_URL`. Port every behavior tested in `tests/test_approvals.py` and `tests/test_intents_and_budget.py`.

### B2: GitHub (`src/factory/github/`, `tests/github/`)
- `app_auth.py`: GitHub App JWT (RS256, pyjwt) → installation token, cached until 60 s before expiry.
- `client.py`: `RestGitHub(GitHubPort)` using httpx, with **two separate identities**: a push-bot client and a merge-bot client (separate App credentials).
- `push_diff` runs these steps:
  1. **Classify the diff with `policy.action_classes.classify`. Refuse AC8.**
  2. Clone into `repos_root` using the push-bot token.
  3. Check out `base_commit`, create the branch, and `git apply --index` the patch.
  4. Commit as the bot and push over HTTPS.
  5. Return the sha.

  The token never appears in logs or remote URLs saved to disk. Use a credential helper or `http.extraHeader`.
- `merge_pr(expected_head_sha)` uses the merge-bot client and `PUT /pulls/{n}/merge` with `sha`.
- `check_runs` maps GitHub conclusions to `CheckResult`. A null or in-progress run maps to `missing`.
- `webhooks.py`:
  - Verify `X-Hub-Signature-256` with a constant-time compare.
  - Parse into typed events: `IssueLabeled`, `PullRequestReview`, `CheckSuiteCompleted`, `PushToDefault`, `IssueCommentCreated`.
  - Ignore everything else explicitly.
- `fake.py`: `FakeGitHub(GitHubPort)` keeps in-memory issues, branches, PRs, reviews and check runs. It has helpers for tests and the demo (`add_issue`, `set_checks`, `add_review`). It also refuses AC8 diffs.
- `setup.py`: `recommended_ruleset(repo)` and `apply_repo_settings(...)`. These set branch protection or rulesets on `main`: required checks, required CODEOWNERS review, and a merge restriction to the merge-bot App. They also disable "Allow GitHub Actions to create and approve pull requests" (§13.2). Include a dry-run mode that prints the JSON.

### B3: Runtime, sandbox, budget (`src/factory/runtime/`, `src/factory/sandbox/`, `src/factory/budget/`, `deploy/litellm/`, `deploy/egress-proxy/`, `deploy/sandbox/`, `tests/runtime|sandbox|budget/`)
- `runtime/claude.py`: `ClaudeRuntime(AgentRuntime)` on `claude_agent_sdk.query`.
  - Options: `cwd=workdir`, `model`, `max_turns`, `max_budget_usd`, `allowed_tools`, `system_prompt`, `setting_sources=["project"]`.
  - `env`: `ANTHROPIC_BASE_URL=gateway_url`, `ANTHROPIC_API_KEY=gateway_key`. The **gateway key only**, never a raw provider key.
  - A `can_use_tool` callback: allow if the tool is in `allowed_tools`. Otherwise call `on_tool_approval(tool, input)`. If that returns False or is None, deny.
  - Parse `ResultMessage` (`total_cost_usd`, `num_turns`, `session_id`, `is_error`) into `RuntimeResult`. Map budget or turn exhaustion to `budget_exceeded`.
  - `resume` uses `options.resume`. `checkpoint` returns `{session_id, workdir, messages}` via `get_session_messages` where available.
  - Wrap async calls safely for sync callers.
  - Unit-test option construction and result mapping by monkeypatching `query`. No real calls.
- `runtime/fake.py`: `FakeRuntime`, scripted per role. A script is a callable that receives the `RuntimeRequest` and may write files in `workdir`, returning `RuntimeResult`. It must be able to emit the agent-output JSON formats in §4.
- `runtime/codex.py`: `CodexRuntime` raises `NotImplementedError("Phase 3 exit test")` with a docstring describing the mapping. This is the only allowed stub.
- `sandbox/docker.py`: `DockerSandbox(SandboxPort)`.
  - `create`: a clean checkout of `base_commit` under `sandbox_root/<id>`, cloned from a local mirror in `repos_root`.
  - `exec`: `docker run --rm --network none --user 1000:1000 --read-only (where practical) -v <workdir>:/work -w /work <image> …`. When `network=True`, attach to the `factory-egress` network with `HTTP(S)_PROXY=egress_proxy_url`. **No credentials mounted.**
  - `capture_diff`: `git add -A`, then `git diff --cached --binary <base>`, plus `--numstat` and added text. Build `FileChange` statuses from `--name-status`.
- `sandbox/local.py`: `LocalSandbox`, subprocess in a temp checkout, for demo and tests only. Its docstring and a startup warning say **it is not a security boundary**.
- `budget/litellm.py`: `LiteLLMGateway(BudgetGateway)`:
  - `POST /key/generate` with `max_budget` and `metadata.mission_id`
  - `GET /key/info` → spend
  - `POST /key/delete`
  - Authenticated with the master key. **Never log keys.**
- `budget/fake.py`: `FakeBudgetGateway`, with the ability to add spend and to exceed the budget.
- `deploy/litellm/config.yaml`: model list for `claude-opus-5-5`, `claude-sonnet-5` and `claude-haiku-4-5-20251001` via the Anthropic provider, master key from env, budgets enabled.
- `deploy/egress-proxy/`: a proxy config (tinyproxy or squid) that allows only the LLM gateway host and a package mirror. Include a README.
- `deploy/sandbox/Dockerfile`: non-root, git, python3, node. **No credentials.**

### B4: Approval inbox (`src/factory/inbox/`, `tests/inbox/`)
- `signing.py`: HMAC-SHA256 signed tokens encoding `{request_id, approver, roles, exp}`. Verification uses a constant-time compare, checks expiry, and rejects tampering.
- `router.py`: `build_inbox_router(store: StateStore, decide: Callable, signer, clock) -> APIRouter`.
  - `GET /inbox?token=…` lists the open packets the approver may act on.
  - `GET /inbox/{request_id}?token=…` shows the packet page: the recommendation, alternatives, **raw diff**, evidence checks (with the policy rule fired), holdout counts, recovery plan, cost, untrusted inputs, expiry, and required approvals.
  - `POST /inbox/{request_id}/decision` takes a form with the token, `decision` (approve/revise/defer/cancel) and `content_hash`. It calls `decide(request_id, approver, roles, decision, content_hash)`.
  - It maps `ApprovalError` subclasses to clear 409/410/403 responses.
  - **All untrusted text is HTML-escaped. No inline JS is needed.** Use a CSRF-safe design: the token is in the form body, and POST only.
- `slack.py`: `SlackNotifier(Notifier)` posts Block Kit messages to an incoming webhook, with **URL buttons** to signed per-approver inbox links. It also provides `verify_slack_signature()` (v0 scheme, 5-minute replay window) for future interactive use.
- `fake.py`: `RecordingNotifier`.
- Tests: FastAPI TestClient, escaping of a malicious issue title, tampered or expired tokens, and wrong `content_hash` → 409.

### B5: Verification, release, telemetry (`src/factory/verification/`, `src/factory/release/`, `src/factory/telemetry/`, `tests/verification|release|telemetry/`)
- `verification/checks.py`: `evaluate(required: list[str], results: dict[str, CheckResult]) -> CheckVerdict(ok, failing, missing)`. **Only `success` passes.**
- `verification/local_checks.py`: run named check commands (from a mapping) through `SandboxPort.exec(network=False)` → `CheckResult`s, with trimmed output as detail.
- `verification/review.py`: run the **reviewer** agent (`RuntimeRequest(role="reviewer")`) on the captured diff. The diff is passed as data. Parse its JSON (§4) → `CheckResult("review_agent")`. **Unparseable output is `failure`.**
- `verification/evidence.py`: `build_evidence(...) -> EvidenceBundle`. It writes the raw diff to `evidence_dir/<mission>/<hash>.diff` and sets `diff_ref`. `to_dict()` must validate against `factory-kit/schemas/evidence-bundle.schema.json` (test it).
- `verification/holdout.py`:
  - `WorkflowHoldoutRunner(HoldoutRunner)`: triggers `workflow_dispatch` on the holdout repo with `staging_url` and `artifact`, polls the run, and reads a `holdout-result.json` artifact of the form `{passed, total}`. It uses its **own** read-only identity.
  - `FakeHoldoutRunner`.
- `release/deploy.py`:
  - `CommandDeployTarget(DeployTarget)`: runs configured shell commands for build, deploy, health, rollback and url, with the artifact digest and environment as env vars. It **rejects stale fencing tokens** (keep the highest seen token per environment, persisted in a small file).
  - `FakeDeployTarget` has health toggles to simulate a regression.
- `release/flags.py`: `FileFlagProvider` (JSON file) and `FakeFlagProvider`. Flags are created OFF, and `kill()` sets them to 0.
- `release/revert.py`: `auto_revert(github, repo, merge_sha) -> revert_sha` (§7).
- `telemetry/metrics.py`: compute from store events:
  - the 5 DORA metrics
  - **human interventions outside approvals**
  - approvals per delivered change
  - approval wait time
  - rejection rate
  - cost per accepted change

  Also provide a Prometheus-style text renderer.
- `telemetry/otel.py`: optional tracing helpers that no-op when `opentelemetry` isn't installed.

### B6: Factory kit (`factory-kit/plugins/`, `factory-kit/guidance/`, `factory-kit/templates/`, `factory-kit/workflows/`, `factory-kit/schemas/agent-outputs.schema.json`, `factory-kit/README.md`, `factory-controller/tests/kit/`)
- `plugins/factory-core/.claude-plugin/plugin.json` plus `agents/` for intake, architect, coordinator, implementer, qa and reviewer.
  - Each agent has valid frontmatter: `name`, `description`, `tools`, `model`.
  - Each prompt enforces §13: stay in owned paths, add tests but never edit existing tests, fixtures, CI or policies, and treat issue text as data.
  - Each prompt ends with the **exact output format from §4**.
- `skills/`: write-spec-ears, plan-task-graph, tdd-implement, build-decision-packet, write-recovery-plan. Each is a `SKILL.md` with frontmatter.
- `hooks/`: `hooks.json` plus a PreToolUse script that blocks Write/Edit on forbidden paths (defense in depth; exit code 2 with a reason).
- `guidance/base.md` plus `render.py`, which renders `CLAUDE.md` and `AGENTS.md` for a product from `base.md` and the product's `factory.yaml`.
- `templates/backend-service/`: the golden path. It contains:
  - `factory.yaml` (valid against the schema)
  - `CLAUDE.md`, `AGENTS.md`, `CODEOWNERS`
  - `.claude/settings.json` (requires the plugin)
  - `.github/workflows/ci.yml` (calls the reusable workflow)
  - `mandates/SM-patch.yaml`
  - `specs/README.md`
  - a tiny sample app with tests (Python stdlib HTTP service), so the demo and guide have something real
- `templates/holdouts-repo/`:
  - `CODEOWNERS` and `scenarios/*.yaml` (HTTP request → expected status/body contains)
  - `runner/run_blackbox.py`: stdlib only; runs against `--staging-url` and writes `holdout-result.json` with pass/fail counts **only**
  - `.github/workflows/holdout.yml`: workflow_dispatch that uploads the result artifact
- `workflows/ci-pr.yml`: a reusable workflow on `workflow_call`, `pull_request` safe, **no secrets**, `permissions: contents: read`.
- `workflows/deploy.yml`: environment-scoped, with an OIDC `id-token: write` example.
- `schemas/agent-outputs.schema.json`: the JSON Schemas for the §4 formats.
- Tests:
  - plugin files parse
  - agent frontmatter complete
  - `render.py` output
  - the template `factory.yaml` and mandate validate against the schemas
  - the holdout runner against a local stdlib HTTP server
  - the hook script blocks a forbidden path

### B7: Integration, wave 2 (`src/factory/pipeline/`, `wiring.py`, `app.py`, `worker.py`, `cli.py`, `demo/`, `deploy/docker-compose.yml`, `deploy/controller.Dockerfile`, `tests/pipeline/`, `tests/e2e/`)

B7 wires the modules into the mission lifecycle of §7 and §14.1. **B7 only depends on ports.** It never imports concrete adapters, except in `wiring.py`.

**Products** (`pipeline/products.py`)
- `ProductRegistry` loads `<products_dir>/<product>/factory.yaml` and `mandates/*.yaml`, validating both against the kit schemas. Invalid config fails loudly at startup.
- It maps repo → product through the `repo` field.
- `checks` maps check names to sandbox commands. Every required name except `review_agent` and `holdout_blackbox` must have a command, or startup fails.

**Triggers**
- The issue label `factory:patch` or `factory:feature` on a repo registered to a product starts a mission.
- It's idempotent: one mission per work item. Re-labeling doesn't duplicate.
- Other labels are mirrors of state only (`factory:state/<STATE>`). The controller never reads labels as truth.

**Tool guard** (`pipeline/tool_guard.py`)
- Agents run with `allowed_tools=()`, so **every** tool call goes through `on_tool_approval`.
- The guard allows Read/Glob/Grep/Edit/Write only when the resolved path is inside the task's sandbox workdir. Edit/Write additionally need the path inside the task's `owned_paths` and not matching the floor or product forbidden paths.
- It denies Bash, WebFetch, WebSearch and everything else. **The controller, not the agent, runs code**: checks run through `SandboxPort.exec(network=False)`, and failures feed back into the repair prompt.
- Document this as the v1 containment model. The runtime process itself moves into the sandbox container in Phase 3.

**Lifecycle** (`pipeline/orchestrator.py`, class `Factory`)

Every step is idempotent and re-entrant. Every mission update is a CAS through `store.update_mission`. Every side effect (push, merge, deploy, rollback) goes through `controller.intents.execute_once` with a stable operation id.
1. **Intake.** Run the intake agent on the issue, with the issue text passed as quoted data. Then classify.
   - A patch lane with a matching standing mandate (`coverage.check_request`) → `covered` → ADMITTED.
   - Otherwise → DISCOVERING.
2. **Discovery.** Run the architect agent on a read-only sandbox checkout, then parse and validate the §4 JSON.
   - Build the H1 `DecisionPacket` (`pipeline/packets.py`).
   - Create an `ApprovalRequest` with `gate_resolver.resolve(...).h1`, then `notifier.decision_requested`.
   - Move to AWAITING_H1.
   - Invalid architect output → the mission goes to AWAITING_HX with the error. It never advances.
3. **Decisions** (`Factory.decide(request_id, approver, roles, decision, content_hash)`). This is the function the inbox calls. It records the decision.
   - Quorum met → consume the approval right before the guarded action and fire the FSM event.
   - revise → back to DISCOVERING.
   - cancel/decline → ARCHIVED or CANCELED as the FSM says.
4. **Admission.** `approval_budget.can_admit` plus the mission budget. Create a per-mission gateway key (`BudgetGateway.create_key`). Create tasks from the plan with `TaskRecord` contracts that are schema-valid. Move to ACTIVE.
5. **Tasks.** The controller releases READY tasks, with at most `max_parallel_workers` running.
   1. Create a sandbox at `base_commit`.
   2. Run the implementer agent.
   3. `capture_diff`, then `classify` it.
   4. For standing missions, run `coverage.check_diff`. Exceeding coverage → `coverage_exceeded` → DISCOVERING.
   5. Run the product `checks` in the sandbox with no network, and `verification.checks.evaluate`.
   6. On failure, repair: resume or rerun with the trimmed check output, up to `repair_attempts[lane]`. Exhausted → `boundary` → AWAITING_HX.
   7. Record spend from the gateway. Over budget → `budget_exhausted`.
6. **Join + integrate.** When every task is DONE (AND barrier), apply all task diffs onto a fresh sandbox at the base. A conflict → `conflict` → ACTIVE, with a repair task.
   - Rerun the checks and the **reviewer** check (`verification.review`) on the combined diff.
   - Build the evidence (`verification.evidence.build_evidence`).
   - The push bot runs `github.push_diff` to branch `factory/<mission_id>`, then `open_pr` with a **controller-generated** body: evidence summary, checks table and rule fired. The body has no agent-authored free text except the architect summary, rendered as a quoted block.
7. **HM.** Resolve with `protected_touched`.
   - `auto` → consume nothing and merge once the required GitHub check runs are all `success` (fail closed).
   - Otherwise → HM request plus a notification. GitHub `pull_request_review` APPROVED events on the **current head sha** from a listed approver are converted into `decide(... approve ...)`. The inbox works too.
   - The merge bot calls `merge_pr(expected_head_sha)` after consume → MERGED.
   - Create the mission flag (default OFF) via `FlagProvider.create`.
8. **Post-merge.** Check runs on the merge sha. Failure → `release.revert.auto_revert` → REVERTED → ACTIVE (repair).
9. **Release candidate.**
   1. `DeployTarget.build(repo, merge_sha)` → artifact, then deploy to `staging`.
   2. `HoldoutRunner.run(product, staging_url, artifact)` → a `holdout_blackbox` CheckResult: success only if `passed == total > 0`.
   3. Complete the evidence → RELEASE_READY.
   4. H2 = `release_requirement([...])`: v1 has one mission per release. `standing` → DEPLOYING. Otherwise H2 request + notification → AWAITING_H2.
10. **Deploy.** Consume H2, getting a fencing token. `execute_once(deploy prod)`. `flags.set_rollout(flag, 100)`. Then OBSERVING.
11. **Observe** (worker).
    - Healthy through `observation_window` → DELIVERED.
    - Unhealthy → `flags.kill`, `DeployTarget.rollback`, then `store.approvals.invalidate_for_artifact` → RECOVERING. The recovery plan is followed; with no automated fix path → AWAITING_HX.
12. **HX.** The packet offers alternatives: extend budget by 50% and reset repair attempts, reduce scope, or cancel. Approve → ACTIVE with the extension applied. Decline → CANCELED.
13. **Events.** Append store events for every transition, decision, check and deploy. Use the kinds B5's `telemetry/metrics.py` expects, reading B5's docs. **Human interventions outside approvals** (a human commit on a factory branch, a manual re-run) are recorded when detected from webhooks: a push to `factory/*` by a non-bot user.

**Worker** (`worker.py`)
- `run_once(now)` does the following:
  - applies timeouts (`mission_fsm.TIMEOUTS`): notify the backup approver first, then fire `timeout`
  - releases READY tasks
  - reconciles pending intents
  - advances OBSERVING missions
- `run_forever(interval)`.

**App** (`app.py`, FastAPI)
- `POST /webhooks/github`: verify the signature, parse the event, dispatch. Unknown events → 204.
- `/inbox`: B4's router with `decide = factory.decide`.
- `GET /healthz`.
- `GET /metrics`: B5's Prometheus text.
- No other endpoints.

**Wiring** (`wiring.py`): `build_factory(settings) -> Factory`.
- `local`: MemoryStateStore (or Postgres if `database_url`), FakeGitHub, FakeRuntime (demo scripts), LocalSandbox, FakeBudgetGateway, RecordingNotifier (which also logs the inbox links), FakeHoldoutRunner, FakeDeployTarget, FakeFlagProvider.
- `production`: the real adapters from Settings.

**CLI** (`cli.py`, entry point `factory`)
- `factory serve`
- `factory worker`
- `factory migrate`
- `factory validate-kit`
- `factory products validate`
- `factory classify --git BASE..HEAD [--repo PATH]`
- `factory gates --class AC4 --profile standard [--level L3] [--protected]`
- `factory inbox-link --request REQ --approver @x --roles tech_lead`
- `factory demo [--scenario NAME|all] [--serve]`

**Demo** (`demo/`)
- It uses the kit's `templates/backend-service` sample app, copied into a temporary git repo, with FakeGitHub and FakeRuntime scripts that **really edit the sample app**.
- Scenarios:
  - `happy_path` (feature: H1 → HM → H2 → DELIVERED)
  - `patch_standing` (covered patch, auto/1 gates per the table)
  - `coverage_exceeded`
  - `h1_reject`
  - `repair_then_pass`
  - `repair_exhausted_hx`
  - `forbidden_path_blocked` (the agent tries `.github/workflows` → AC8 → blocked before push)
  - `post_merge_revert`
  - `prod_regression_rollback`
  - `stale_approval_rejected`
  - `replay_after_rollback`
- Each scenario prints a readable timeline (state transitions, packets, decisions, receipts) and **asserts** its expected end state.
- `--serve` starts the app with fakes plus a background worker, and prints signed inbox links, so a human can approve in the browser.

**Deploy**
- `deploy/docker-compose.yml`: postgres, litellm (B3 config), egress-proxy (B3 config), controller (`factory serve`) and worker (`factory worker`). The sandbox network is internal. Secrets come via env_file (`.env.example` documents every `FACTORY_*` variable).
- `deploy/controller.Dockerfile`.

**Tests**
- `tests/pipeline/`: unit tests of each lifecycle step with fakes.
- `tests/e2e/`: every demo scenario runs as a test and asserts its end state.

---

## 4. Agent output formats (B3 fakes, B5 reviewer check, B6 prompts and B7 parsing must agree)

Agents end their final message with **one fenced ```json block**. The pipeline parses the **last** JSON block. Invalid or missing output means the step **failed** (fail closed).

**intake**
```json
{"lane": "patch|feature", "suggested_class": "AC1|AC2|AC3|AC4|AC5|AC6|AC7", "summary": "…", "risk_notes": ["…"]}
```

**architect** (H1 packet material + task graph)
```json
{
  "summary": "…",
  "recommendation": "APPROVE|REVISE|DECLINE: one sentence why",
  "acceptance_criteria": ["WHEN … THE SYSTEM SHALL …"],
  "tasks": [
    {"task_id": "T-1", "objective": "…", "owned_paths": ["src/x/**"], "action_class": "AC4",
     "acceptance_checks": ["python -m pytest tests/test_x.py"], "depends_on": []}
  ],
  "alternatives": ["…"],
  "risks": ["…"],
  "recovery_plan": "…",
  "estimate_usd": 12.5
}
```

**implementer** (edits files in its workdir; no pushing)
```json
{"status": "done|blocked", "summary": "…", "tests_added": ["tests/…"], "commands_run": ["…"], "blocker": null}
```

**qa**
```json
{"status": "pass|fail", "scenarios": [{"name": "…", "result": "pass|fail", "detail": "…"}]}
```

**reviewer**
```json
{"verdict": "pass|fail", "findings": [{"severity": "blocking|major|minor", "path": "…", "line": 1, "message": "…"}]}
```
`verdict` is `fail` whenever any finding is `blocking`.
