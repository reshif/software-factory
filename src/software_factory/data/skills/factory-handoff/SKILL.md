---
name: factory-handoff
description: Prepare a recoverable factory session handoff or an evidence-backed local PR packet without claiming a remote PR, merge or deployment occurred.
---

# Prepare the next reader's handoff

## Inputs and preconditions

The constitution in AGENTS.md applies; read [the constitution](../../../.factory/CONSTITUTION.md) only if it is not in your context. Read the current mission, Git state, decisions and evidence. Determine whether this is an interruption/vendor switch or a completed PR candidate.

## Procedure

For a session handoff run `uv run --locked --project .factory software-factory packet --mission ID --kind handoff`. It writes `handoff-packet.md` (HEAD, `git status --short`, blockers, suspended tasks, task status and gate reasons; record the branch and active profile yourself in `handoff.md`) and never overwrites the authored `handoff.md`. Update `handoff.md` with the accepted outcome, actual active operations, uncommitted user work, important decisions and the next useful action using [the handoff template](../../../.factory/templates/handoff.md). Do not include secrets or full transcripts.

For a PR candidate, author a non-empty `## Risks` section in `plan.md` or `spec.md` and edit `recovery.md`, then run the gate on the final candidate. The readiness gate and `uv run --locked --project .factory software-factory packet --mission ID --kind pr` check these shared prerequisites; the packet also names the latest review. After the gate passes, transition to READY_PR and generate the packet so it shows the recorded state. Inspect the packet against the diff and evidence; do not hide unavailable validation.

Use the documented state transition to READY_PR only when the gate passes and the authored packet prerequisites are satisfied. Creating or sending a remote PR is a separate external action governed by existing authorization and available tools. This skill prepares a local artifact. READY_PR means local evidence is consistent, not attested. Record remote CI afterwards with `software-factory mission ci-result` (its URL and conclusion are caller-supplied and unchecked); a failure (with `--reason`) returns the mission to IMPLEMENTING. Cancelling needs `--reason` (and `--decision` for a decline reference) and generates the handoff packet.

Include the latest startup model-selection checkpoint (context, selected/inherited/unresolved outcome, evidence references and reason), recorded assignments, observation provenance and unresolved requirements in the handoff. A new client/session revalidates it through factory-start before dependent work; completed attempts keep their original profile and evidence.

## Outputs and verification

A readable handoff or `pull-request.md` packet backed by current records. Confirm paths and evidence references resolve, current work is preserved and the completion state is accurate.

## Failure behavior

If the gate rejects the PR candidate, preserve a blocked handoff instead. An interrupted mission can always explain its current state even when it is not ready to merge. Follow [resume and switch](../../../.factory/docs/runbooks/resume-and-switch.md).
