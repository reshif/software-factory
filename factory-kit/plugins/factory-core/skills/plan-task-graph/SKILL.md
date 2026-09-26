---
name: plan-task-graph
description: Break an approved mission into a task graph of at most 2 parallel tasks, each with disjoint owned_paths, dependencies and acceptance checks, and expand a task into a full task contract. Use when the architect is planning a feature mission, or the coordinator is releasing a READY task after H1 approval.
---

# Plan a task graph

The join barrier (final draft §9.1) fans a mission out to **at most 2 parallel
tasks in v1**, each an implementer working in its own `owned_paths`, plus an
independent QA check, joined with an AND barrier before integration. A task
graph that doesn't respect this shape can't actually run in parallel — it just
looks like it can.

## Rules for splitting work

1. **Owned paths must be disjoint.** If two tasks would touch the same file,
   they are one task, or one must depend on the other (`depends_on`).
2. **No more than 2 tasks ready at once.** If the natural split has three or
   more independent pieces, pick the two most valuable to parallelize now and
   sequence the rest behind them.
3. **Every task names its own action class.** A task that turns out to touch
   a protected path is AC6 regardless of what the rest of the mission is.
4. **Every task carries concrete acceptance checks** — commands, not
   descriptions — so both the implementer and the reviewer can verify it the
   same way.
5. **A conflict fix during integration is a new task/revision**, never a
   silent edit onto the old one — give it a fresh id and dependency on the
   revision it's fixing.

## Expanding a task into a task contract

The architect's lightweight task (`task_id`, `objective`, `owned_paths`,
`action_class`, `acceptance_checks`, `depends_on`) becomes a full **task
contract** when the coordinator releases it, matching
`factory-kit/schemas/task-contract.schema.json`:

```json
{
  "schema_version": 1,
  "ids": {"mission": "MIS-0042", "task": "T-1", "run": "R-1", "depends_on": []},
  "objective": "…",
  "base_commit": "<40-char sha>",
  "owned_paths": ["src/x/**"],
  "action_class": "AC4",
  "tools": ["read", "edit", "run_tests"],
  "acceptance_checks": ["python -m pytest tests/test_x.py"],
  "limits": {"repair_attempts": 2, "infra_retries": 3, "minutes": 30, "usd": 12}
}
```

`limits` come from `factory.yaml`'s `repair_attempts`, `infra_retries` and the
lane's per-task budget (`patch_task_usd` / `feature_task_usd`) — never
invented per task.
