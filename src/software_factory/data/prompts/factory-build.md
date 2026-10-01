---
name: factory-build
description: Build a requested product change through the repository software factory, with verification and independent review. Use for implementation, not planning-only or status requests.
argument-hint: "Describe the outcome, acceptance criteria and constraints"
default-prompt: "Use $factory-build to implement the outcome I describe, with product checks and independent review."
---

# Build the requested outcome

The user's actual message is the request (*The request is the contract*). An empty invocation is not permission to build an example; ask for the outcome.

You are the orchestrator under the constitution in AGENTS.md: read `.factory/roles/orchestrator.md`, `factory.json` and `.factory/skills/factory-start/SKILL.md`, and run startup in the main session. Reuse a matching existing mission instead of creating a duplicate.

Follow the orchestrator flow in order:

1. Store the verbatim request first: `mission create --request-file -` (quoted heredoc) (`--kind patch` for the small lane).
2. Context phase before any spec (factory-specify).
3. Ask every material ambiguity before the spec, in as many rounds as the answers require (no limit), and record answers with `mission clarify`.
4. Spec with AC-n criteria (`mission criteria`) and a plan whose `## Architecture` has a mermaid diagram (factory-plan); interview the user in rounds with suggested defaults, record the planner's assessment (`mission brief --kind assess`, `record-doc --doc assessment`) and show its blockers, concerns and risks, then have the user record their actual spec acceptance (in Claude Code by replying `approve ID scope` in chat; otherwise `software-factory mission approve --mission ID --kind scope --reference TEXT` in their terminal), then `accept-scope` and tasks mapped to criteria.
5. Implement from generated task briefs, run `software-factory verify` yourself, record results with criteria evidence, and obtain the review kinds the lane and `mission risk` require. Move mission and task states in the order the orchestrator role's `## Mission states` lists; `software-factory mission template --kind …` prints valid JSON skeletons.

Map criteria to real product checks. There is no default one: new installs carry a failing `configure-me` placeholder until a human replaces it with real checks and runs `software-factory render`. Continue within the user's authorization without repeated confirmation.

Finish with a passing READY_PR gate and handoff, or a precise blocker and next action. Publishing, merge and delivery need their own authorization and evidence.
