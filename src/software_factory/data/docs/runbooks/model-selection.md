# Model selection

`jev.enabled` chooses the selector used by `software-factory models plan`: **true → JEV**, **false → factory-models**. Both use actual inventory and hard constraints. JEV chooses a model; the coding agent executes the work. `jev.claim_mode` controls only claim/source judgments. A JEV choice below `jev.min_confidence` (default 0.6) stays unresolved for a human decision, as does an abstention. Transient provider failures (HTTP 408, 429, 500, 502, 503, 504, 529, connection errors) are retried at most twice within the same deadline. See [JEV routing](../jev-routing.md) for the complete path, limits and unresolved behavior.

The `factory-models` skill discovers eligible models and explains task fit before substantial factory planning or delegation. It is available after profile rendering as `$factory-models` in native Codex and `/factory-models` in Claude Code or Copilot. With combined profiles, Copilot discovers the shared exported skill tree. Select the Copilot `factory` agent first.

Start with:

> Use factory-models to inspect this client's available models for [objective]. Check current official vendor recommendations. Recommend models and supported effort for orchestration, planning, implementation, verification and independent review. Explain fit, fallbacks, evidence and unknowns. Draft only; do not change settings or start implementation.

Use `$factory-models` or `/factory-models` in place of the plain skill name if the client exposes it. Later, an authorized factory-build can consume the plan. A skill invocation is not itself a model switch or subagent launch.

## Startup checkpoint

You can start with `factory-build` or `factory-start`; a separate manual `factory-models` invocation is optional. Startup inspects the prior result, completes model planning inline when required evidence is missing or stale, and checks the returned outcome before dependent work. It reuses a current result instead of repeating discovery only to show that the skill ran. `factory-resume` repeats this check for the current context. The models skill returns to its caller without starting another factory workflow.

The orchestrator records the checkpoint in the existing mission `plan.md` and carries it into `handoff.md`. Before a mission exists, or for an explicitly read-only request, it reports the checkpoint in chat. Include:

- Check time, objective and next role/task requirements covered by the decision.
- Selection mode and actual profile/harness, client version, billing context and non-sensitive session label; preserve unknown values honestly.
- Plan and assignment references when present, catalog observation/provenance, policy and guidance used, and actual validation commands/outcomes.
- Outcome: **selected**, **inherited**, or **unresolved**, with its reason, affected work and remaining evidence gaps.

**Selected** requires a compatible current plan and catalog, reconciled guidance, and supported controls for the next action. Run `software-factory models validate --kind plan --input PATH` (or `--input -` with the plan JSON on stdin); before execution, also run `software-factory models dispatch --plan PATH --assignment ID --catalog CURRENT-PATH` for each assignment needed next, where PATH can be the registered plan under `.factory/missions/ID/models/`. Validation replays the recorded decision offline and rechecks eligibility at the recorded creation time; dispatch checks current availability. Apply supported settings through the actual host before dependent work and record execution observations separately. Neither a "selected" label nor `applied: false` proves that the model is executing.

With JEV disabled, **Inherited** records an intentional decision to retain host defaults under inherit/omitted mode, or after assessment in recommend mode when the task's requirements permit it. Required mode, an existing binding, an explicit user selection requirement or another hard constraint cannot be bypassed this way. Reconcile bindings through the existing task workflow; never silently remove them. An unknown effective model remains unknown.

**Unresolved** records missing or incompatible evidence and holds the work that depends on it. Continue independent intake/research where useful. Request concrete missing information when needed; repeated skill launches without new evidence do not resolve the checkpoint.

A new session, changed client/harness or billing context, changed objective/requirements, policy/guidance changes, or expired/changed availability requires reassessment. Record which next action is covered rather than declaring every future assignment settled. Blueprint may investigate unresolved choices within its planning-only scope, without applying selections or claiming execution readiness. Status only reports the existing checkpoint and never refreshes it.

This checkpoint is an orchestrator instruction backed by existing validation/dispatch tools and mission records. The state tools still enforce bound-task and required-mode constraints; no new boolean flag is treated as proof that a skill ran or a model executed.

## Policy and evidence

`factory.json` accepts `model_selection.mode`:

| Mode | Behavior |
| --- | --- |
| `roles` (new installations) | Each role's agent file pins a model from `model_selection.roles.<client>.<role>` (`claude`, `codex`, `copilot`; roles `orchestrator`, `planner`, `implementer`, `verifier`, `reviewer`). Claude accepts aliases (`opus`, `sonnet`, `haiku`) or full IDs as `model:` frontmatter; Codex writes `model` to the agent TOML; Copilot writes `model` to `.agent.md`. A role with no entry, or `"inherit"`, keeps the session's model. The Claude main session runs whichever model the user started it with; only `claude --agent factory-orchestrator` uses the orchestrator entry. New projects get a suggested map for every installed client (deep work on the strongest model, implementation on the coding model, verification on the fast one); `software-factory models roles` shows it with the choices, and a mission cannot start until the user approved the map through a setup proposal, which stamps `confirmed_sha256`. Each mission records the map it started with (`models`), and the gate refuses it if the map changes. The Claude guard denies an `Agent` call that passes `model`; Codex and Copilot follow the pinned agent files without a hook. |
| `inherit` or omitted | Keep host inheritance unless the user explicitly requests model planning or a task is explicitly bound. In a rendered project, `mission create` asks for a confirmed per-role map first (`models roles` proposes one). |
| `recommend` | Factory preflight prepares recommendations; uncertain tasks may retain inherited execution when their requirements permit it. Every bound task still validates its assignment and observation. |
| `required` | Tasks must have valid model assignments before execution and completion. Missing inventory is unresolved, never silently downgraded to inheritance. |

Portable defaults live in `.factory/models/policy.json`; `.factory/models/recommendations.json` contains dated vendor guidance. The preference order is an editable factory decision, not a claim that vendors measured those models against this repository. An explicit preference needs a rationale and must still satisfy hard constraints. Model access, available tools and account spending controls remain governed by the host.

Published guidance, visible account inventory, requested selection and observed execution are different evidence. JSON files and human-reported inventory are local, unauthenticated records. Do not save credentials, account identifiers or sensitive transcripts. Use a non-sensitive session label such as `WORK-20260926-A` and regenerate/revalidate when the real session changes.

## Prepare inventory and a request

These commands print JSON unless `--output` names a file directly under `.factory/local/models/`. That directory is ignored. Only `models plan` calls JEV inference, and only when `jev.enabled` is true; it then sends the request objective and any research `strengths` text to TypeSafe after masking recognized secrets with the shared redaction helper (a best-effort denylist; the receipt records the mask count). Other model commands do not run inference. These tools do not fetch arbitrary webpages, change native settings or configure credentials. Research into the linked official sources is delegated: the orchestrator briefs the planner with `software-factory mission brief --mission ID --kind research` and records the cited findings the planner returns; it does not browse itself.

The examples below use files, as a human operator would. During a mission the orchestrator writes no files: `models plan`, `models validate`, `models dispatch` and `models outcome-record` accept `-` for their request, plan, observation or outcome JSON argument and read it from stdin (at most one `-` per invocation, 256 KiB, UTF-8), supplied through a quoted heredoc:

```bash
uv run --locked --project .factory software-factory models validate --kind request --input - <<'JSON'
{"schema_version": 1, "kind": "request", "id": "PLAN-001", ...}
JSON
```

Catalogs come from `models discover --output`, which is a user setup step (the Claude orchestrator guard denies `models discover`). A plan registered with `mission model-plan --input -` is stored under `.factory/missions/ID/models/`, and `models dispatch --plan` can read it from there.

```bash
uv run --locked --project .factory software-factory models sources --profile claude
uv run --locked --project .factory software-factory models template --kind request --profile claude --session WORK-20260926-A --id PLAN-001 --objective "Implement the accepted change" --output .factory/local/models/request.json
uv run --locked --project .factory software-factory models template --kind catalog --profile claude --session WORK-20260926-A --output .factory/local/models/catalog.json
```

For Codex metadata discovery, supply the executable belonging to the client you actually use. A PATH CLI may differ from VS Code's bundled runtime:

```bash
uv run --locked --project .factory software-factory models discover --profile codex --client /absolute/path/to/codex --session WORK-20260926-A --provider openai --billing subscription --output .factory/local/models/catalog.json
```

Set provider and billing to their actual values; the factory itself does not read credentials to infer them. Discovery spawns `codex app-server` in the repository root with the inherited environment, so that process may use Codex's own stored authentication and network access and loads the project's `.codex` configuration. It paginates `model/list` and reports only fields it can establish. Tools, lifecycle, context capacity and subagent availability may need separate documentation/runtime evidence. The discovery result alone can legitimately produce unresolved assignments. [Codex app-server](https://learn.chatgpt.com/docs/app-server#models)

Claude Code exposes no machine-readable model list. Copy the `Available:` line that `/model` prints and pass it as `--picker`; add the version reported by `claude --version`:

```bash
uv run --locked --project .factory software-factory models discover --profile claude --session WORK-20260926-A --picker "sonnet, opus, haiku, fable, best, sonnet[1m], opus[1m], fable[1m], opusplan, default, or a full model ID" --client-version 2.1.300 --billing subscription --output .factory/local/models/catalog.json
```

The command maps each listed family alias (`sonnet`, `opus`, `haiku`, `fable`) or full model ID to the reviewed guidance entry for the `claude` profile and lists that entry's exact full model ID as `visible`, with `provenance.kind: "user_report"` quoting the picker text. Selection strategies (`best`, `default`, `opusplan`) and aliases or IDs without reviewed guidance are reported under `skipped`; `X[1m]` maps to the same model and is noted, because guidance does not state context capacity. The alias-to-ID mapping comes from reviewed guidance, not from this account's resolution; check `/status` when identity matters. A guidance entry's `minimum_client_versions` stays a hard constraint, so an unknown `--client-version` produces a warning and leaves that model ineligible.

Without `--picker`, discovery lists every reviewed Claude guidance entry with `availability: "unknown"` and `provenance.kind: "fixture"` ("availability not verified for this account"). Planning rejects unknown availability, so such a catalog documents the choices but selects nothing until you rerun with `--picker` or edit availability from real evidence.

Guidance-derived entries use conservative values: `identity: "exact"`, `operations: ["main", "subagent"]` (Claude Code accepts full model IDs for both), `capabilities: ["text", "tools"]` (image input is not recorded), `lifecycle: "stable"` (reviewed guidance covers generally available models), and `context_tokens: null`, `efforts: []`, `default_effort: null`, `cost_tier: null`. Requirements that need a context size, effort or cost tier therefore stay unresolved until you research and record those values. With `--output`, the command summary lists the models, `skipped` entries, notes and warnings; otherwise the catalog prints on standard output and those diagnostics on standard error.

For Copilot, specify `--profile copilot --harness copilot-local` or `--harness copilot-agent-host`. For Copilot, `discover` returns an explicitly unavailable inventory template until actual picker/host evidence is supplied. It does not pretend a shell API can inspect a VS Code picker.

`models plan` refuses a catalog with no models (exit 2), because JEV and factory-models can only choose among listed models. When every assignment ends without an eligible candidate, the command summary (or standard error) names the most common rejection reasons.

When importing picker information, retain the literal selectable identifier and use `provenance.kind: "user_report"` with a concrete reference such as `VS Code Manage Language Models, current session, observed by operator`. Preserve the actual observation date and client version. An example model entry (replace each value from real evidence):

```json
{
  "id": "claude-sonnet-5",
  "provider": "anthropic",
  "identity": "exact",
  "resolved_model": null,
  "availability": "visible",
  "operations": ["main", "subagent"],
  "capabilities": ["text", "image", "tools"],
  "context_tokens": null,
  "efforts": ["medium", "high"],
  "default_effort": "medium",
  "lifecycle": "stable",
  "cost_tier": null,
  "guidance_key": "anthropic-sonnet"
}
```

This illustrates the schema, not account availability or a universal list of supported efforts. Use `identity: "alias"` for aliases such as `sonnet`, with the evidenced exact resolution in `resolved_model`; an unresolved alias stays ineligible. Claude subagent assignments always require an evidenced full native identifier; the factory does not reuse main-picker alias resolution for a child, because invocation/frontmatter aliases can follow the parent's exact model. Main-session alias selection remains available. Use `"auto"` for Copilot Auto. Unknown lifecycle is `"unknown"`, unknown numeric limits are `null`, and unestablished capabilities/operations are omitted from their arrays. Default policy requires established stable lifecycle. Research missing fields from the actual surface; never relabel edited inventory as wholly runtime-attested.

Guidance must match the exact model identifier, or an alias's evidenced resolution. A `guidance_key` cannot apply a newer model's strengths to an older model. Literal provider syntax such as `opus[1m]` and `claude-sonnet-4-5@20250929` is accepted; it does not establish availability or task fit by itself.

For Copilot Local, set the catalog's `parent_model` to the actual selected main model identifier when observable. Planning derives child bounds from the proposed main assignment, or this observed parent if the plan contains only child tasks. Dispatch checks the actual parent again; a guessed per-child `parent_cost_tier` cannot override it.

The request template contains five role assignments. Change their task intent and requirements as appropriate; add a supporting task only when needed. `capabilities`, `min_context_tokens`, `allowed_providers`, `exact_model`, `effort`, `max_cost_tier` and `parent_cost_tier` are constraints. Cost tiers are relative to the same client/catalog, not universal dollar prices. Do not derive them from unrelated API prices. Preview models require both policy and request allowance.

Refresh official guidance after the configured seven-day interval, or earlier when model behavior changes. The request's `research` list accepts entries with the same fields as canonical recommendations: `key`, `provider`, `profiles`, `model_ids`, `strengths`, `sources`, `checked_at`. This records current research without editing canonical controls during a product task. For a new model absent from the configured preference order, use a reasoned explicit preference. Stale guidance is visibly flagged; selection from stale guidance should be reconciled with current research before dispatch.

Invalid dates and guidance dated more than five minutes into the future are rejected, including research entries not used by the selected assignment. Optional `minimum_client_versions` records documented prerequisites by harness; planning and dispatch both check them.

## Plan and dispatch

```bash
uv run --locked --project .factory software-factory models validate --kind catalog --input .factory/local/models/catalog.json
uv run --locked --project .factory software-factory models plan --input .factory/local/models/request.json --catalog .factory/local/models/catalog.json --output .factory/local/models/plan.json
uv run --locked --project .factory software-factory models dispatch --plan .factory/local/models/plan.json --assignment implementer --catalog .factory/local/models/catalog.json
```

Inspect the resulting JSON. `selected` means the recorded data satisfies the declared requirements. `unresolved` explains rejected candidates; it does not select a weaker model automatically. An explicit preference that is unavailable remains unresolved. Fallbacks are eligible alternatives; adopting one creates a new plan/assignment before a new attempt. A runtime substitution must be reported and cannot satisfy an incompatible hard requirement.

`dispatch` revalidates current inventory and returns native configuration fields with `applied: false`. The orchestrator uses supported per-invocation controls when exposed, or the user operates the main model picker. Keep the full factory role/constitution instructions. Settings fields are not a universal subagent API signature. No command writes global or generated configuration.

A change of billing context requires a new plan, including a change from a known context to `unknown`. A fresh session can reuse a valid assignment only after its availability, model metadata, client prerequisites and parent constraints pass again.

| Surface | Supported selection and observation |
| --- | --- |
| Native Codex | `model` and `model_reasoning_effort` are agent configuration fields; actual spawn tools can expose different parameter names and inheritance restrictions. Inspect the current tool schema. Record runtime metadata when available. [Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents) |
| Claude Code | Main session uses its picker; `/model <name>` can persist the default, while the picker offers session-only selection. Subagent `model`/`effort` depend on client/version and actual capabilities. `/status` and `/tasks` provide observable identity; force settings or fallback can substitute models. [Model configuration](https://code.claude.com/docs/en/model-config), [Subagents](https://code.claude.com/docs/en/sub-agents#choose-a-model) |
| Copilot Local | Use the actual model picker or exposed subagent control. A single `model` string avoids ambiguity with other harnesses. Explicit child selection above the parent cost tier is rejected; unknown tiers remain unresolved. Effort is a picker control, not a portable agent field. [VS Code subagents](https://code.visualstudio.com/docs/agents/run/subagents#select-the-model-for-a-subagent) |
| Copilot Agent Host | Native agent configuration documents a model string. Validate its own host controls; do not apply Local array/precedence rules automatically. [Native agent schema](https://docs.github.com/en/copilot/reference/custom-agents-configuration) |

Copilot Auto can be an explicit preference when variable identity and its permitted model pool are acceptable. The first version rejects Auto under hard provider, context, effort, cost-tier, exact-model or stable-only requirements because a strategy name cannot prove them. Do not enable experimental Auto settings during discovery. Record the effective response model when visible, without claiming the strategy itself is an exact model. [Copilot Auto](https://docs.github.com/en/copilot/concepts/models/auto-model-selection)

## Bind a mission task and record execution

During an authorized mission, register a plan once:

```bash
uv run --locked --project .factory software-factory mission model-plan --mission M-EXAMPLE --input .factory/local/models/plan.json
```

The command stores `.factory/missions/M-EXAMPLE/models/PLAN-001.json` exclusively and returns bindings containing `plan_path`, `plan_hash`, `assignment_id`, `assignment_hash`. Copy the implementer binding into the task-add input's `model_assignment` field, or a pending task-update input alongside a concrete `reason`. Use a new plan ID for revisions. Do not hand-edit a registered plan or reset retry counters. Plans bind an implementation hash of the factory's `routing.py`, `models.py`, `jev.py` and `core.py`; any upgrade that changes one of them invalidates saved and registered plans, so regenerate and register a new plan afterwards.

```bash
uv run --locked --project .factory software-factory mission task-update --mission M-EXAMPLE --task T-001 --input .factory/local/models/task-update.json
uv run --locked --project .factory software-factory mission task-transition --mission M-EXAMPLE --task T-001 --to RUNNING --model-catalog .factory/local/models/catalog.json
```

A paused bound task also requires `--model-catalog` on `software-factory mission resume`. A fresh catalog may revalidate the same assignment in a new session. Changed model metadata requires reconciliation. Completed execution keeps its original profile provenance across later profile switches. Stop active work before changing its contract.

If a paused assignment cannot resume because its client/model changed, use `software-factory mission resume --mission ID --to IMPLEMENTING --resolution "<actual change and recovery plan>" --replan-models`. This explicitly leaves the suspended task BLOCKED for task-update, removes the suspended restoration request and preserves its attempt history. Register a new plan and reassign before starting another attempt; no old assignment runs automatically.

Add `model_observation` to the existing task-result JSON. Fill its assignment hash, current attempt and requested values from the actual binding; observed values must come from evidence:

```json
{
  "schema_version": 1,
  "kind": "observation",
  "assignment_hash": "<actual 64-character assignment hash>",
  "attempt": 1,
  "requested_model": "<actual requested identifier>",
  "requested_effort": null,
  "observed_model": null,
  "observed_effort": null,
  "provenance": "unknown",
  "reference": null,
  "fallback_reason": null,
  "observed_at": "2026-09-26T00:00:00Z"
}
```

Replace the timestamp with the real observation time. Use `runtime` with a concrete runtime evidence reference, `user_report` for an operator's UI report, or `self_report` for an agent's own statement. Exact-model and explicit-effort requirements require matching runtime evidence. Human reports remain useful but do not become runtime attestation. After final configured verification, save the task result before transitioning a bound task from VERIFYING to DONE; the completion gate validates it again.

Completion also checks the current model policy. Tightening allowed providers, denied models or preview rules can invalidate readiness for an earlier assignment. Historical records remain intact; reconcile the plan and obtain new evidence instead of relabeling an earlier run. A later profile switch alone does not erase a completed run's provenance.

The assignment content is included in the candidate fingerprint and watched during verification. Observation timestamps and local research-cache updates do not alter that contract. Model reassignment does not grant a fresh repair budget. Tests and independent review remain required regardless of model choice.

## Complete a model-bound task

For the setup guide's `M-0001` / `T-001` example, start the bound task with its current catalog, perform the authorized implementation, then capture checks:

```sh
uv run --locked --project .factory software-factory mission task-transition --mission M-0001 --task T-001 --to RUNNING --model-catalog .factory/local/models/catalog.json
uv run --locked --project .factory software-factory mission task-transition --mission M-0001 --task T-001 --to VERIFYING
uv run --locked --project .factory software-factory mission transition --mission M-0001 --to VERIFYING
uv run --locked --project .factory software-factory verify --mission M-0001 --revision R-001
```

Create `.factory/local/task-result.json` from that capture's actual fingerprint, evidence reference and task outcome, adding the observation above for the current assignment and attempt. Complete in order: `record-result`, task `DONE`, mission `REVIEWING`, actual separate-context `review`, `gate`, `READY_PR`, and the PR packet. Every task needs a current recorded result before DONE. Python lifecycle tests exercise these transitions in disposable repositories; fixture identities and reviews do not establish live vendor execution.

## Optional repository calibration

Record metadata from already-authorized work; this facility never launches inference or a paid benchmark. Create a template and fill the actual task IDs, measurements and evidence reference:

```sh
uv run --locked --project .factory software-factory models outcome-template --output .factory/local/models/outcome-input.json
uv run --locked --project .factory software-factory models outcome-record --input .factory/local/models/outcome-input.json
uv run --locked --project .factory software-factory models calibration
```

Use a unique outcome ID. `outcome-record` requires an existing task execution and schema-valid result, validates its bound model observation, and preserves a hash plus metadata snapshot of that result in an immutable `.factory/local/models/outcomes/ID.json`. The directory must be effectively Git-ignored and untracked. Do not include prompts, code, credentials or transcripts. Local records and hashes catch accidental changes; they do not authenticate the observer. A later task attempt may replace the task result without rewriting the earlier observation.

`comparison.prompt_hash`, `tools_hash` and `acceptance_hash` are SHA-256 hashes of the comparable prompt template, tool/permission contract and acceptance rubric. Leave unavailable hashes `null`; such records cannot join comparison cohorts. Keep task classes narrow and comparable. Record the actual execution client version and its evidence reference in `execution_context`; a plan's catalog client version is preserved separately and does not prove the runtime version. Unknown execution versions cannot join comparison cohorts. `accepted`, `missed_defects`, `wall_time_ms` and `usage` stay `null` when not measured. Supply `runtime`, `user_report` or `self_report` measurement provenance with a concrete reference; quality judgments remain judgments. Accepted means the referenced review accepted that task, not that the factory gate passed; a blocked or unresolved result cannot be recorded as accepted. Repair count is derived from the task attempt; usage remains in its original tokens, credits or requests, without currency conversion.

The report groups only matching task class, prompt, tools, acceptance, client, billing and provenance contracts, then displays observations per effective model and effort. Unknown runtime identity remains unattributed; requested model names do not become execution evidence. Invalid or duplicate execution records are reported and excluded. Counts, acceptance rates and medians are descriptive, with uncertainty retained for every group; there is no automatic ranking or sample-size guarantee. Review the source references and representative failures, then use justified findings in an explicit model-plan preference/rationale. Current availability, vendor guidance and hard constraints still control eligibility.

## Validation and limitations

Run the configured factory suite and `uv run --locked --project .factory software-factory render --check` after factory maintenance. Native smoke tests in `.factory/docs/vendor-smoke-tests.md` distinguish parsing/discovery from live account behavior. A successful metadata probe or syntax check does not prove model execution. Keep unavailable live checks explicit.

The optional calibration report provides local observational evidence only. It does not establish causal model superiority, account availability, or model execution beyond the provenance supplied.

Calibration reports `prior_task_attempts` as task history context. These attempts may have used other models; they are not a repair count attributable to the model in the current outcome. All `software-factory models --output` destinations and outcome directories must be effectively ignored and untracked, including atomic temporary files.
