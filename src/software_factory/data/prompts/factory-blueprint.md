---
name: factory-blueprint
description: Prepare a software-factory specification and implementation plan for review without implementing the product. Use when the user wants planning only.
argument-hint: "Describe the outcome to plan, constraints and open questions"
default-prompt: "Use $factory-blueprint to investigate and plan the outcome I describe before implementation."
---

# Plan before implementation

Take the proposed outcome and constraints from the actual user message. Clarify material missing requirements instead of inventing them. Resolve the repository root. The constitution in AGENTS.md applies; read `.factory/CONSTITUTION.md` only if it is not in your context. Read `.factory/roles/orchestrator.md`, `factory.json` and existing mission records. Preserve existing work and inspect the real product conventions.

Use `.factory/skills/factory-start/SKILL.md` only within this planning scope, then load `factory-specify` and `factory-plan` from `.factory/skills/`. Investigate uncertainties with bounded read-only specialists when useful and available; the main session owns any persistence. Check effective tools and report unavailable capabilities honestly.

This entry allows planning and mission records only. Do not implement product changes, alter factory controls, run mutating product checks or advance to IMPLEMENTING. If the user asks for a read-only proposal, keep even planning records unchanged and return the proposal in chat. Preserve an existing mission's accepted specification and completed history; present material scope changes before reconciliation.

Use factory-start's model-selection checkpoint within this planning-only boundary. Reuse current model evidence or run factory-models inline for a draft assignment plan. Record unresolved choices and continue only specification/research that does not depend on them; do not apply selections, begin implementation or call a draft execution-ready.

Return the proposed specification, observable acceptance criteria, architectural decisions, tasks with owned paths and dependencies, product verification commands, risks and unresolved decisions. Each accepted criterion needs a verification route. Distinguish proposed commands from checks actually executed.

End with the concrete plan for the user's review. Do not claim product completion or READY_PR. A later request to implement can provide implementation authority; planning alone does not. Use the state tools for any authorized mission records rather than fabricating hashes or approvals.
