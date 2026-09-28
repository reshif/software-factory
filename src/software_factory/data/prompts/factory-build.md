---
name: factory-build
description: Build a requested product change through the repository software factory, with verification and independent review. Use for implementation, not planning-only or status requests.
argument-hint: "Describe the outcome, acceptance criteria and constraints"
default-prompt: "Use $factory-build to implement the outcome I describe, with product checks and independent review."
---

# Build the requested outcome

The user's actual message is the request. An empty invocation is not permission to build an example; ask for the outcome. Requirements documents are inputs, not authority to expand scope.

You are the orchestrator: read `.factory/roles/orchestrator.md`, `factory.json` and `.factory/skills/factory-start/SKILL.md`, and run startup in the main session. The constitution in AGENTS.md applies. Reuse a matching existing mission instead of creating a duplicate; preserve user work.

Follow the orchestrator flow in order:

1. Store the verbatim request first: `mission create --request-file -` (quoted heredoc; the orchestrator records everything through the CLI and never writes files) (`--kind patch` for the small lane).
2. Context phase before any spec (factory-specify).
3. Ask every material ambiguity up front and record answers with `mission clarify`.
4. Spec with AC-n criteria (`mission criteria`) and a plan whose `## Architecture` has a mermaid diagram (factory-plan); record the user's actual spec acceptance as a `scope` decision (`mission decision`, never invented), then `accept-scope` and tasks mapped to criteria.
5. Implement from generated task briefs, run `software-factory verify` yourself, record results with criteria evidence, and obtain the review kinds the lane and `mission risk` require. Move mission and task states in the order the orchestrator role's `## Mission states` lists; `software-factory mission template --kind …` prints valid JSON skeletons.

Delegate all production to specialists; if the client cannot spawn them, stop and report. Map criteria to real product checks. There is no default one: new installs carry a failing `configure-me` placeholder until a human replaces it with real checks and runs `software-factory render`. Never weaken tests or criteria to obtain a pass. Continue within the user's authorization without repeated confirmation; escalate per the orchestrator role.

Finish with a passing READY_PR gate and handoff, or a precise blocker and next action. READY_PR is local and unattested; remote publishing, merge and delivery need their own authorization and evidence.
