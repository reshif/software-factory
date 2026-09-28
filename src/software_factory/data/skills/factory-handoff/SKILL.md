---
name: factory-handoff
description: Prepare a recoverable factory session handoff or an evidence-backed local PR packet without claiming a remote PR, merge or deployment occurred.
---

# Prepare the next reader's handoff

## Inputs and preconditions

The constitution in AGENTS.md applies; read [the constitution](../../../.factory/CONSTITUTION.md) only if it is not in your context. Read the current mission, Git state, decisions and evidence. Determine whether this is an interruption/vendor switch or a completed PR candidate.

## Procedure

For a session handoff run `uv run --locked --project .factory software-factory packet --mission ID --kind handoff`. It writes `handoff-packet.md` (HEAD, `git status --short`, blockers, suspended tasks, task status and gate reasons; record the branch and active profile in `handoff.md`) and never overwrites the authored `handoff.md`. Record `handoff.md` with `mission record-doc --mission ID --doc handoff --input -`, covering the accepted outcome, actual active operations, uncommitted user work, important decisions and the next useful action using [the handoff template](../../../.factory/templates/handoff.md). Do not include secrets or full transcripts.

For a PR candidate, the plan needs a non-empty `## Risks` section and the planner drafts the recovery text, which the orchestrator records with `record-doc --doc recovery --input -`; then run the gate on the final candidate. The readiness gate and `uv run --locked --project .factory software-factory packet --mission ID --kind pr` check these shared prerequisites; the packet also carries the Architecture diagram copied from plan.md, a Request → Criterion → Evidence → Verdict table, the risk tier with reasons, and the review kinds with their sources. After the gate passes, transition to READY_PR and generate the packet so it shows the recorded state. Inspect the packet against the diff and evidence: every AC row needs its request excerpt, evidence and a `pass` verdict; do not hide unavailable validation or `needs_human` verdicts.

Use the documented state transition to READY_PR only when the gate passes and the authored packet prerequisites are satisfied. Creating or sending a remote PR is a separate external action governed by existing authorization and available tools. This skill prepares a local artifact. READY_PR means local evidence is consistent, not attested. The user records remote CI afterwards with `software-factory mission ci-result` in their own terminal (its URL and conclusion are caller-supplied and unchecked); a failure (with `--reason`) returns the mission to IMPLEMENTING. Cancelling needs `--reason` (and `--decision` for a decline reference) and generates the handoff packet.

A session handoff names the current phase (context, clarification, criteria, plan, task, verify, review kind, gate), open ambiguities and the next brief to generate. Include the model checkpoint line (or, when models were planned, its outcome, assignments and unresolved requirements). A new client/session revalidates it through factory-start before dependent work; completed attempts keep their original profile and evidence.

## Outputs and verification

A readable handoff or `pull-request.md` packet backed by current records. Confirm paths and evidence references resolve, current work is preserved and the completion state is accurate.

## Failure behavior

If the gate rejects the PR candidate, preserve a blocked handoff instead. An interrupted mission can always explain its current state even when it is not ready to merge. Follow [resume and switch](../../../.factory/docs/runbooks/resume-and-switch.md).
