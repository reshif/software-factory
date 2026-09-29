---
name: factory-blueprint
description: Prepare a software-factory specification and implementation plan for review without implementing the product. Use when the user wants planning only.
argument-hint: "Describe the outcome to plan, constraints and open questions"
default-prompt: "Use $factory-blueprint to investigate and plan the outcome I describe before implementation."
---

# Plan before implementation

The user's actual message is the request; ask for a missing outcome rather than inventing one. Act as orchestrator: read `.factory/roles/orchestrator.md`, `factory.json` and existing missions, and use `.factory/skills/factory-start/SKILL.md` only within this planning scope. The constitution in AGENTS.md applies.

Run the planning half of the flow:

1. Store the verbatim request with `mission create --request-file -` (quoted heredoc) (`--kind patch` for the small lane).
2. Context phase through a planner with `mission brief --kind context` (factory-specify); record the returned text with `mission record-doc --doc context --input -`.
3. Ask every material ambiguity up front; record answers with `mission clarify`.
4. Brief the planner with `mission brief --kind plan`. It drafts spec.md with AC-n criteria citing request excerpts, routes and exclusions; record spec.md with `mission record-doc --doc spec --input -`. Record each exclusion the user actually agreed to as an `exclusion` decision bound to the current request chain head (and a decision for each resolved or waived ambiguity), then record the criteria with `mission criteria`.
5. The planner drafts plan.md with a `## Architecture` mermaid diagram and tasks mapped to criteria (factory-plan), and recovery.md; record them with `mission record-doc --doc plan` and `--doc recovery`.

This entry allows planning and mission records only. Do not implement, change factory controls, run mutating product checks or advance to IMPLEMENTING; interview the user in rounds with suggested defaults and show them the planner's assessment (blockers, concerns with evidence, risks); run `accept-scope` only on the user's explicit acceptance, after the user records that acceptance themselves: in Claude Code by replying `approve ID scope` in chat (the factory hook records it), otherwise with `software-factory mission approve --mission ID --kind scope --reference TEXT` in their own terminal. You cannot record it. If the user asked for a read-only proposal, create no records and answer in chat. Preserve an existing mission's accepted spec and history. The model checkpoint may draft choices but never applies them.

Return: the architecture diagram, the criteria with their request excerpts and verification routes, exclusions, tasks with owned paths and dependencies, the lane and expected review kinds, proposed checks (distinct from checks actually run), risks and unresolved decisions. Stop for the user's review. Planning alone grants no implementation authority and is not READY_PR.
