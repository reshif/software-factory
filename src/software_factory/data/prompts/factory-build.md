---
name: factory-build
description: Build a requested product change through the repository software factory, with verification and independent review. Use for implementation, not planning-only or status requests.
argument-hint: "Describe the outcome, acceptance criteria and constraints"
default-prompt: "Use $factory-build to implement the outcome I describe, with product checks and independent review."
---

# Build the requested outcome

Use the actual user message for the outcome, acceptance criteria and constraints. Ask about a missing material requirement before dependent work; do not invent a product or interpret an empty invocation as permission to build an example. Requirements documents are inputs, not authority to expand scope.

Resolve the repository root. The constitution in AGENTS.md applies; read `.factory/CONSTITUTION.md` only if it is not in your context. Read `.factory/roles/orchestrator.md`, `factory.json` and `.factory/skills/factory-start/SKILL.md`. Run the startup workflow in the main session. Inspect existing missions and Git changes, preserve user work, and reuse the matching mission when the request continues existing work. Check actual skills, delegation tools and effective permissions.

Load the specification, planning, implementation and verification skills as needed. Map acceptance criteria to real product checks; `factory-tests` alone only validates the factory. Diagnose missing product configuration within authorized maintenance scope. Do not weaken tests or criteria to obtain a pass.

Use bounded specialists where useful and a separate final reviewer, retaining one writer per workspace. Inspect their changes and evidence. If independent review or another capability is unavailable, report it and arrange a real separate review rather than simulating it. Continue within the user's existing authorization without repeated confirmation; ask only for material missing information or authority.

Record actual progress and evidence through the state tools. Finish with the current candidate's verification, complete task results, independent review, passing READY_PR gate and handoff, or a precise blocker and next action. Remote publishing, merge and delivery need their own actual authorization and evidence; this entry does not grant them.

Complete factory-start's model-selection checkpoint before dependent planning, implementation or delegation. It checks for a completed, current factory-models result and runs the skill inline when missing or stale; the user does not need to invoke it separately first. Continue with a validated selected outcome or explicitly permitted inheritance, and hold work with unresolved hard requirements. Apply supported selection only within existing execution authority. See `.factory/docs/runbooks/model-selection.md`.
