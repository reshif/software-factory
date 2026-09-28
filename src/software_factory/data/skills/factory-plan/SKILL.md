---
name: factory-plan
description: Translate a factory mission specification and criteria into an architecture diagram and bounded tasks mapped to criteria, or replan when evidence invalidates the approach.
---

# Plan bounded work

## Inputs and preconditions

The constitution in AGENTS.md applies; read [the constitution](../../../.factory/CONSTITUTION.md) only if it is not in your context. Follow [the planner role](../../../.factory/roles/planner.md). Read the `mission brief --kind plan` brief, `context.md`, the spec and criteria, product configuration and the current repository. Use [the plan template](../../../.factory/templates/plan.md). Do not repeat research that `context.md` already supports.

## Procedure

1. **Architecture (every mission).** plan.md must contain a `## Architecture` section with at least one fenced `mermaid` block whose first line is a diagram type such as `flowchart`, `sequenceDiagram` or `classDiagram` (leading `%%` comments and front matter are allowed), followed by at least one real node or edge line. Show the components and data flow of what will be built or changed, not the factory process. A patch mission may use a tiny diagram (two or three nodes). Accept-scope refuses a plan without it, and the PR packet copies it.
2. **Approach.** Choose the smallest complete approach; explain only tradeoffs that matter.
3. **Tasks.** Split work into inspectable outcomes with task IDs, dependencies, owned paths (prefer explicit globs such as `src/app/**` and `tests/**`; a trailing slash like `tests/` also covers the directory), configured check IDs and `criteria` (the AC ids each task satisfies). Every AC maps to at least one task. Do not invent check IDs or divide work into parallel writers that share a workspace. Include integration and final verification.
4. **Risks and recovery.** Author a non-empty `## Risks` section, and draft recovery.md stating the change's recovery implications (how to undo it, data or compatibility effects, or why none apply). Add delivery and deployment recovery planning only when those outcomes are authorized.

For model-bound work, include the immutable `model_assignment` returned by `software-factory mission model-plan` in each task input. Model changes use pending-task replanning and never reset attempts.

## Outputs and verification

plan.md content and schema-compatible task inputs, returned to the orchestrator. A read-only specialist never runs state commands or edits files. The orchestrator records the plan with `mission record-doc --mission ID --doc plan --input -`, runs `mission accept-scope --mission ID` (which records the plan.md and context.md hashes; a later change to either needs accept-scope again), then `software-factory mission task-add --mission ID --input -` per task; the tool validates dependencies and criteria ids, and the gate requires every AC to be mapped. One task input (the tool sets `status` and `attempts`; `software-factory mission template --kind task` prints a skeleton):

```json
{"id": "T-1", "title": "Reject empty titles", "depends_on": [], "owned_paths": ["src/app/**", "tests/**"],
 "checks": ["tests"], "criteria": ["AC-1"]}
```

`checks` are configured check ids from `factory.json`. Omitted, every required check applies, so the default works only when at least one configured check is required; a task that ends up with no checks (an empty list included) is refused; `owned_paths` needs at least one entry; `model_assignment` is added only for model-bound work.

For pending work the orchestrator uses `software-factory mission task-update --mission ID --task TASK --input -` with a concrete `reason` and only the changed fields. Stop active tasks before replanning. Attempts and identity cannot be reset; earlier contracts stay in task history. If accepted scope removes planned work, convert its pending task into an explicit reconciliation task and report that truthfully.

## Failure behavior

Return concrete uncertainty or missing authority to the orchestrator. Do not add speculative tasks or silently expand the accepted scope to fit a preferred architecture.
