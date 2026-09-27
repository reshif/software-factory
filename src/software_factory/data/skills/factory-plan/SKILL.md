---
name: factory-plan
description: Translate a factory mission specification into bounded tasks, owned paths, dependencies and verification requirements, or replan when evidence invalidates the approach.
---

# Plan bounded work

## Inputs and preconditions

The constitution in AGENTS.md applies; read [the constitution](../../../.factory/CONSTITUTION.md) only if it is not in your context. Follow [the planner role](../../../.factory/roles/planner.md) (already part of the exported factory-planner agent). Read the accepted specification, product configuration and current repository. Use [the plan template](../../../.factory/templates/plan.md). Do not duplicate research that is already supported by current evidence.

## Procedure

Choose an approach proportionate to the change and uncertainty. Explain decisions with meaningful tradeoffs. Split work into inspectable outcomes with task IDs, dependencies, owned paths and configured check IDs. Do not invent check IDs or divide work into parallel writers that share a workspace.

Provide the orchestrator with a dependency order and useful parallel research/review opportunities. Include integration and final verification work, not only implementation. Add delivery and data recovery planning only when those outcomes are authorized.

For model-bound work, include the immutable model_assignment binding returned by `software-factory mission model-plan` in each task input; specify the actual role/task needs, not an assumed universal model ranking. Unresolved requirements remain explicit. Model changes use pending-task replanning and never reset attempts.

Return schema-compatible task inputs and the proposed plan to the orchestrator. The orchestrator persists them with `software-factory mission task-add --mission ID --input PATH` using the schema and [CLI runbook](../../../.factory/docs/runbooks/factory-setup.md). A read-only specialist must not run state mutations or edit plan files. Preserve existing completed tasks during replanning; explain changes to pending work and record material scope decisions. Use state transitions only after their prerequisites hold.

For existing pending work, the orchestrator uses `software-factory mission task-update --mission ID --task TASK --input PATH`. Supply a concrete `reason` plus only changed title, dependencies, owned paths or checks. Stop active tasks before replanning. Attempts and identity cannot be reset; previous contracts are retained in task history. If accepted scope removes planned work, convert its pending task into an explicit reconciliation task that verifies the removal and dependent expectations; report that outcome truthfully rather than claiming the removed feature was built.

## Outputs and verification

A rationale in `plan.md`, task records with resolvable dependencies and check mappings, and identified decisions. Confirm every acceptance criterion has implementation or investigation work and a verification route. Dependency validation is performed by the state tool.

## Failure behavior

Return concrete uncertainty or missing authority to the orchestrator. Do not add speculative tasks or silently expand the accepted scope to make a preferred architecture possible.
