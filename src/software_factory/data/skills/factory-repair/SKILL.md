---
name: factory-repair
description: Diagnose and repair a failed factory check or blocking review finding, respecting the task's retry limit and preserving acceptance criteria.
---

# Repair from evidence

## Inputs and preconditions

The constitution in AGENTS.md applies; read [the constitution](../../../.factory/CONSTITUTION.md) only if it is not in your context. Read the failed evidence or review finding, affected task, specification and attempt count. Identify whether the failure is code, environment, test defect, ownership conflict or an incorrect plan before choosing a fix.

## Procedure

Optionally, before retrying a failed check, run `uv run --locked --project .factory software-factory triage --mission ID --revision REV` ([check triage](../../../.factory/docs/runbooks/check-triage.md)); use the category to choose diagnosis, never to skip diagnosis or retry blindly.

Reproduce or narrow the failure with the smallest useful investigation. State the hypothesis and what evidence would discriminate it. Make a scoped correction or return a concrete replan request; do not repeat equivalent actions without new evidence.

Use the task state commands so retry attempts are recorded. Check `factory.json` limits before another attempt. Changes to accepted behavior, protected factory rules or required checks follow the actual scope policy; a failure does not grant new authority.

Run relevant focused validation, then obtain a new verification run and review for the changed candidate. Explain how each blocking finding is resolved with file/evidence references. Prior passing evidence does not automatically apply after repair.

Diagnose context, tooling and code defects before model escalation. Reassignment requires a validated new plan and pending-task update with rationale; preserve attempts and existing constraints. Record runtime substitutions and refuse incompatible fallbacks.

## Outputs and verification

A repair diff or diagnosis, updated attempts/status and references to fresh checks. The orchestrator inspects the result and chooses the next stage based on evidence.

## Failure behavior

Starting a task beyond its limit moves the task and mission to BLOCKED with the cause recorded. When progress depends on missing authority, run `uv run --locked --project .factory software-factory mission block --mission ID --reason TEXT --next TEXT`. Save a handoff with attempts, cause and smallest next decision. Resume refuses an exhausted task until the plan changes: after a diagnosis, `task-update` with new owned paths, checks or dependencies and a new reason starts a new budget once per task while the cumulative count is kept; titles, reordering and earlier contracts do not count. If the task exhausts again, the mission stays BLOCKED: report it, and let a human replan the work as a new task (a new mission) or change the configured limit. Local records cannot authenticate that human step; CI and branch protection remain the real controls. Otherwise resume with `--resolution`. Do not reset counters, rename the task or weaken acceptance to evade the limit.
