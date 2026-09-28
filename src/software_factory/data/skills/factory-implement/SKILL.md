---
name: factory-implement
description: Implement one assigned software-factory task within its accepted scope and produce a verifiable result for the orchestrator.
---

# Implement the assigned task

## Inputs and preconditions

The constitution in AGENTS.md applies; read [the constitution](../../../.factory/CONSTITUTION.md) only if it is not in your context. Follow the [implementer contract](../../../.factory/roles/implementer.md) (already part of the exported factory-implementer agent). Read the task record, accepted specification and relevant code. Confirm dependencies, permitted paths and existing changes before entering RUNNING.

## Procedure

Investigate enough to understand the actual change. Verify documentation for version-sensitive APIs before depending on them. Implement the smallest complete solution, follow existing conventions and include meaningful tests for changed behavior and failure cases.

Stay within ownership. Report conflicting edits or required out-of-scope changes; do not overwrite another writer's work. Factory-control edits require explicitly authorized maintenance scope. Explain necessary assertion or fixture changes and preserve the accepted criteria.

Run focused checks during development and inspect results. Return a provisional report of completed behavior, touched files, actual commands and unresolved concerns. The orchestrator records the complete `.factory/schemas/result.schema.json` result only after final configured verification for the integrated candidate. Return the candidate to verification rather than marking it independently reviewed.

If the task has a model assignment, preserve its identity and attempt number. Use the supplied supported host selection and report requested versus observed model/effort or unknown. Do not claim an effective model from your own self-description. Supply the model observation for the final result as described in `.factory/docs/runbooks/model-selection.md`.

## Outputs and verification

An inspectable diff and provisional task report. Transition to VERIFYING using `software-factory mission task-transition` when implementation is ready. The verifier captures final configured checks; focused tests alone do not satisfy all mission checks. After final integration and passing configured verification, the orchestrator records the structured result through `software-factory mission record-result --mission ID --input PATH` under `.factory/missions/ID/results/` (all result registration uses a hashed record index). Include all schema fields, real changed paths, the final candidate fingerprint and current check evidence. Record the result before transitioning the task to DONE. READY_PR requires complete, current results without unresolved issues.

## Failure behavior

Report the failing operation and evidence. Do not weaken checks, claim success on partial output or repeatedly retry without diagnosis. The orchestrator decides repair, replanning or reassignment within the task's configured limit.
