---
name: factory-start
description: Start or resume a repository software-factory mission and coordinate its specialists, evidence and handoff. Use when the user asks to run the factory, not for an unrelated coding question.
---

# Start or resume a factory mission

## Required inputs and preconditions

Use the user's request or active mission ID. Resolve the Git repository root. The constitution in AGENTS.md applies; read [the constitution](../../../.factory/CONSTITUTION.md) only if it is not in your context. Read the [orchestrator role](../../../.factory/roles/orchestrator.md), `factory.json` and the mission records. Follow [setup](../../../.factory/docs/runbooks/factory-setup.md) if prerequisites are missing. Existing authorization persists; do not invent a fresh approval gate for already-approved work.

## Procedure

1. Run `uv run --locked --project .factory software-factory doctor` from the repository root. Inspect actual Git status, selected profile and effective runtime capabilities. Diagnostics do not establish authentication or live model behavior.
2. Identify existing work with `software-factory mission list` and `software-factory mission status --mission ID`; for READY_PR or later its `live_gate` shows whether the stored state still holds. Read the plan and handoff, reconcile actual changes, and identify the immediate outcome and requirements before choosing models.
3. Complete the **model-selection checkpoint** below before substantial planning, implementation or delegation that depends on those choices. Inspect `jev.enabled`: enabled uses JEV for model selection, disabled runs factory-models. Check whether the selected path produced a usable result; reading or invoking the skill alone is insufficient. Run it inline when needed, then inspect its returned outcome before continuing.
4. For a new mission, use the documented `software-factory mission create` command within the selected entry's scope; never fabricate hashes or copy a template as a live mission. Load only the next relevant skill: specify, plan, implement, verify, review, repair or handoff. Use release/recover only when delivery is in scope and configured.
5. Delegate bounded work to available specialists when authorized. Supply task scope, constitution version, accepted criteria and result contract explicitly. Keep one active writer per workspace. Retain responsibility for inspecting their output.
6. Use state commands to record actual progress. PAUSED, BLOCKED (or `software-factory mission block`) and CANCELED need `--reason`; resume needs `--resolution`. Replan within scope when evidence warrants it, and request a human decision only for missing information or authority that affects dependent work.

## Model-selection checkpoint

Inspect the existing model plan and checkpoint in `plan.md` or `handoff.md`, or the current conversation before a mission exists. Match the current objective and next role/task requirements, selection mode, policy/guidance, actual profile/harness, client version, billing context and session. Check catalog freshness and assignment references; a prior invocation, cached file or old "selected" label is not completion evidence.

When JEV is enabled, or for recommend/required mode, an explicit model-planning request, or model-bound work, load `.factory/skills/factory-models/SKILL.md` and complete its discovery/planning procedure if the result is absent, incomplete or stale. Reuse an applicable result after checking it; do not rerun unchanged research simply to prove invocation. Validate a reused plan with `software-factory models validate --kind plan --input PATH`. Before execution, also use `software-factory models dispatch --plan PATH --assignment ID --catalog CURRENT-PATH` for each choice needed next: plan validation alone does not establish current availability. Reconcile stale-guidance warnings before describing a selected choice as settled. If revalidation still fails, return the unresolved outcome rather than repeatedly relaunching the skill without new evidence.

With JEV enabled, run `software-factory models plan` on the current request/catalog. Abstention leaves that assignment unresolved in the written plan; a missing key, provider failure or timeout exits 2 and writes no plan. Do not substitute factory-models. The objective and research text are sent verbatim to TypeSafe. `jev.claim_mode` affects source assessment only. Planning and validation never apply a model to the running host automatically.

Record a concise checkpoint using the [model runbook](../../../.factory/docs/runbooks/model-selection.md#startup-checkpoint). Settle the next action with one of these outcomes:

- **Selected:** the applicable plan and current inventory pass the relevant checks, and supported host controls can apply the requested selection before the dependent work. Record plan/assignment references and remaining observation limits. A dispatch report does not apply settings or prove effective execution.
- **Inherited:** with JEV disabled, inherit/omitted mode intentionally keeps host defaults, or recommend mode permits inherited work after model assessment. Record why inheritance meets this task's requirements and what remains unknown. It cannot bypass required mode, an existing task binding, an explicit user selection requirement, or another hard requirement. Existing bindings must be reconciled explicitly, never silently dropped.
- **Unresolved:** record the missing evidence or incompatible requirement and the affected work. Hold dependent planning, implementation and delegation; continue only independent intake/research within scope. Ask for a concrete missing input only when necessary. Do not mark choices settled because factory-models was started.

Carry the outcome into the mission plan and handoff when persistence is authorized; explicit read-only requests keep it in chat. Recheck on resume and whenever requirements, controls, availability or client/session change. Blueprint keeps its planning-only scope: it may investigate an unresolved execution choice without applying models or claiming execution readiness. Status does not enter startup or refresh this checkpoint. The already-running main model performs this bootstrap; no skill switches itself retroactively.

## Outputs and verification

A model-selection checkpoint with evidence, a current mission record when in scope, appropriate task contracts and a next action or final handoff. Run the readiness gate before claiming READY_PR. A chat summary or subagent completion is not a gate result.

## Failure behavior

If the selected client lacks a capability, diagnose it and state the limitation. Do not imply that another vendor was invoked. Record blockers and preserve a handoff when progress cannot continue. Follow [resume and switch](../../../.factory/docs/runbooks/resume-and-switch.md) for interruptions.
