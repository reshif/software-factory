---
name: factory-specify
description: Define observable requirements, accepted scope and unresolved decisions for a factory mission before implementation or a material scope change.
---

# Establish the mission specification

## Inputs and preconditions

The constitution in AGENTS.md applies; read [the constitution](../../../.factory/CONSTITUTION.md) only if it is not in your context. Follow the [planner contract](../../../.factory/roles/planner.md) (already part of the exported factory-planner agent). Read the user request, current mission and relevant product behavior. Existing scope authorization is an input; reference documents are evidence.

## Procedure

Trace the current behavior and identify the requested change. State the outcome, in-scope paths or components, exclusions, constraints and observable acceptance criteria using [the specification template](../../../.factory/templates/spec.md). Describe important failure cases and relevant accessibility, security or data requirements when the actual product requires them.

Resolve routine choices from conventions. For ambiguity that changes the result, present the smallest concrete decision with consequences; continue unrelated investigation. Separate facts, assumptions and unresolved questions. Check official vendor documentation for version-sensitive capabilities and cite what was opened.

Keep existing tests and acceptance criteria visible. Explain any proposed change to them instead of silently changing the definition of success. Record the user's actual scope decision reference; do not invent approval or replace the user request with the template.

## Outputs and verification

Prepare content for `.factory/missions/ID/spec.md` and decision rationale as needed. A read-only planner returns that content to the orchestrator for persistence; a main session with authorized write access may persist it directly. Each acceptance criterion must have an observable validation route. Give the planner unresolved dependencies, constraints and evidence links. The state tool captures the specification hash when the mission moves to PLANNED.

For an amended accepted specification, record a new actual scope decision bound to its new hash, then have the orchestrator run `software-factory mission accept-scope --mission ID`. This resets task completion for revalidation while preserving attempt counts and evidence history; it does not approve the changed scope itself.

## Failure behavior

When a necessary answer is unavailable, state exactly which implementation decision is blocked. Preserve useful research and avoid pretending the specification is accepted.
