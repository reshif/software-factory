---
name: factory-start
description: Start or resume a repository software-factory mission and coordinate its specialists, evidence and handoff. Use when the user asks to run the factory, not for an unrelated coding question.
---

# Start or resume a factory mission

## Inputs and preconditions

Use the user's request or active mission ID. Resolve the Git repository root. The constitution in AGENTS.md applies; read [the constitution](../../../.factory/CONSTITUTION.md) only if it is not in your context. Act under the [orchestrator role](../../../.factory/roles/orchestrator.md). Follow [setup](../../../.factory/docs/runbooks/factory-setup.md) if prerequisites are missing. Existing authorization persists; do not invent a fresh approval gate for already-approved work.

## Procedure

1. Run `uv run --locked --project .factory software-factory doctor`. Inspect Git status, the active profile and whether this client can spawn the factory specialists; if it cannot, stop and report. Diagnostics do not establish authentication or live model behavior.
2. Find existing work with `software-factory mission list` and `mission status --mission ID` (its `live_gate` shows whether a READY_PR-or-later label still holds). Reuse a matching mission; reconcile records with the actual changes.
3. Settle the model checkpoint below.
4. For new work, store the verbatim request with `software-factory mission create --id ID --title T --kind K --request-file -` before anything else, passing the user's words in a quoted heredoc (`<<'EOF'`); `--kind patch` selects the small lane. Never fabricate hashes or copy a template as a live mission.
5. Load only the next skill: specify (context, clarifications, criteria), plan, implement, verify, review, repair or handoff. Use release/recover only when delivery is in scope and configured.
6. Record every transition with the state commands, in the order of the orchestrator role's [mission states](../../../.factory/roles/orchestrator.md#mission-states). Only PAUSED, BLOCKED (or `mission block`) and CANCELED take `--reason`, and they need it; resume needs `--resolution`.

## Model checkpoint

When `model_selection.mode` is `inherit` (the default) and `jev.enabled` is false, record one line in the plan or handoff: "Models: inherited (mode inherit, JEV off)", and continue. Only when `jev.enabled` is true, or the mode is `recommend` or `required`, or the user asks for model planning, load `.factory/skills/factory-models/SKILL.md` and settle a selected, inherited or unresolved outcome as described in the [model runbook](../../../.factory/docs/runbooks/model-selection.md#startup-checkpoint); hold work that depends on an unresolved hard requirement. Recheck on resume or client change. Status never refreshes it; blueprint may draft choices but never applies them.

## Outputs and verification

A current mission record, the checkpoint line, recorded phase outputs and a next action or final handoff. Run the gate before claiming READY_PR; a chat summary or subagent completion is not a gate result.

## Failure behavior

If the client lacks a capability, state the limitation; do not imply another vendor was invoked. Record blockers and preserve a handoff when progress cannot continue. Follow [resume and switch](../../../.factory/docs/runbooks/resume-and-switch.md) for interruptions.
