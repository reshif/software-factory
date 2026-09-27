---
name: factory-resume
description: Resume an identified software-factory mission after interruption or a client switch, preserving scope, work and evidence history.
argument-hint: "Mission ID, for example M-0001, and any new constraints"
default-prompt: "Use $factory-resume to reconcile and continue the existing mission I identify."
---

# Resume the existing mission

Use the actual mission ID from the user or an unambiguous current mission context. If it is missing or several missions could match, list the relevant IDs and ask which one to resume before mutating state. Do not create a replacement mission or infer approval from elapsed time.

Resolve the repository root. The constitution in AGENTS.md applies; read `.factory/CONSTITUTION.md` only if it is not in your context. Read `.factory/roles/orchestrator.md`, `factory.json`, the mission's `mission.json`, specification, plan, handoff and latest evidence. Follow `.factory/skills/factory-start/SKILL.md` and `.factory/docs/runbooks/resume-and-switch.md` for reconciliation. Inspect the actual branch, HEAD, changes, active operations, profile and effective capabilities. Preserve user edits; verify external state before repeating an external operation.

Continue the same accepted outcome within existing authorization. Keep one writer per workspace, retain task IDs and repair-attempt history, and use only legitimate state transitions. An explicit resume request ends a requested pause, but does not clear unresolved dependencies or supply authority for a different scope. Do not silently change profiles to hide a client mismatch; follow the switching workflow when a switch is requested.

Delegate bounded work and obtain real separate review when available; report missing capabilities. Refresh checks, task results and review for the actual final candidate when source, profile or other governing inputs have changed. Old status labels and missing raw logs do not establish readiness.

Finish with an accurate gate result and handoff, or the concrete remaining blocker and next action. Resume does not authorize a new publish, merge or deployment operation.

Re-enter factory-start's model-selection checkpoint: inspect the prior outcome and revalidate recorded assignments against the current client/session and actual inventory before dependent work. Run model planning through the current selector (`jev.enabled`: JEV when true, factory-models when false) if evidence is missing or stale; a previous invocation does not settle the resumed session. Preserve completed attempt provenance and retry counters. A bound paused task needs a current --model-catalog on resume; factory-status must not perform this refresh.
