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
2. Context phase through a planner with `mission brief --kind context` (factory-specify).
3. Ask every material ambiguity up front; record answers with `mission clarify`.
4. Planner drafts spec.md with AC-n criteria citing request excerpts, routes and exclusions; record them with `mission criteria`.
5. Planner drafts plan.md with a `## Architecture` mermaid diagram and tasks mapped to criteria (factory-plan).

This entry allows planning and mission records only. Do not implement, change factory controls, run mutating product checks or advance to IMPLEMENTING; run `accept-scope` only on the user's explicit acceptance. If the user asked for a read-only proposal, create no records and answer in chat. Preserve an existing mission's accepted spec and history. The model checkpoint may draft choices but never applies them.

Return: the architecture diagram, the criteria with their request excerpts and verification routes, exclusions, tasks with owned paths and dependencies, the lane and expected review kinds, proposed checks (distinct from checks actually run), risks and unresolved decisions. Stop for the user's review. Planning alone grants no implementation authority and is not READY_PR.
