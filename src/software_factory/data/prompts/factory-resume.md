---
name: factory-resume
description: Resume an identified software-factory mission after interruption or a client switch, preserving scope, work and evidence history.
argument-hint: "Mission ID, for example M-0001, and any new constraints"
default-prompt: "Use $factory-resume to reconcile and continue the existing mission I identify."
---

# Resume the existing mission

Use the mission ID from the user or an unambiguous current context. If it is missing or several missions match, list the IDs and ask before mutating state. Do not create a replacement mission or infer approval from elapsed time.

Act as orchestrator: read `.factory/roles/orchestrator.md`, `factory.json`, the mission's records (request, clarifications, context, spec, criteria, plan, handoff) and latest evidence. The constitution in AGENTS.md applies. Follow `.factory/skills/factory-start/SKILL.md` and `.factory/docs/runbooks/resume-and-switch.md` to reconcile the actual branch, HEAD, changes, active operations, profile and effective capabilities with the records. Preserve user edits; verify external state before repeating an external operation.

Continue the orchestrator flow from the first incomplete phase: context, clarification, criteria and architecture plan, tasks, implementation, verification, results, required review kinds, gate. New requirements from the user are clarifications (`mission clarify`); after PLANNED they reset scope for re-acceptance. A mission created without a recorded request (0.2.x) keeps its earlier rules and shows a gate warning; do not fabricate a request for it.

Keep one writer per workspace, task IDs and repair-attempt history. An explicit resume ends a requested pause but does not clear unresolved dependencies or authorize a different scope. Refresh verification, results and reviews when the candidate, profile or governing inputs changed; old labels and missing raw logs do not establish readiness. Revalidate the model checkpoint for the current client/session; a bound paused task needs a current `--model-catalog` on resume.

Finish with an accurate gate result and handoff, or the concrete remaining blocker and next action. Resume does not authorize publish, merge or deployment.
