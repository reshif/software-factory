---
name: factory-implement
description: Implement one assigned software-factory task from its generated brief, within accepted scope, and return a provisional report for the orchestrator.
---

# Implement the assigned task

## Inputs and preconditions

The constitution in AGENTS.md applies; read [the constitution](../../../.factory/CONSTITUTION.md) only if it is not in your context. Follow the [implementer contract](../../../.factory/roles/implementer.md). Work only from the task brief the orchestrator generated with `software-factory mission brief --mission ID --task TASK` (under `.factory/local/briefs/`): its criteria and request excerpts, owned paths, dependencies, checks, attempt budget, base commit and notes from earlier results. Check current Git status before editing. Do not spawn nested agents.

## Procedure

Read the relevant code and `context.md`. Verify version-sensitive APIs in official documentation before depending on them. Implement the smallest complete change that satisfies the brief's criteria, following existing conventions, with tests for changed behavior and failure cases.

Stay within owned paths. Report conflicting edits or needed out-of-scope changes instead of making them. Factory-control edits need explicitly authorized maintenance scope. Explain any assertion or fixture change; never weaken criteria or tests to obtain a pass.

Run focused checks while developing. If the task has a model assignment, report requested versus observed model and effort, or unknown; never claim an effective model from self-description (see `.factory/docs/runbooks/model-selection.md`).

## Outputs and verification

A diff within owned paths and a provisional report: changed paths, behavior per AC id, commands actually run with outcomes, and unresolved concerns. The report is a set of claims for the orchestrator to check, not evidence. Do not record results, transition state, claim review or invent a fingerprint.

The orchestrator then moves the task to VERIFYING, runs `software-factory verify --revision R-n` (a new run label), and records the result with `mission record-result --mission ID --input -`, listing `.factory/missions/ID/evidence/R-n/checks.json` in `evidence` and giving `criteria_evidence` for each AC (`check:<id>` for a check that passed in the current verification, `evidence:<path>`, or `note:<text>`), before moving the task to DONE.

## Failure behavior

Report the failing operation, its evidence and the smallest decision needed. Do not claim success on partial output or retry equivalent actions without new evidence. The orchestrator decides repair, replanning or reassignment within the task's limit.
