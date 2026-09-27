---
name: factory-verify
description: Execute a factory mission's configured checks and capture evidence tied to the actual candidate, detecting stale results, failed commands and source mutation.
---

# Verify the candidate

## Inputs and preconditions

The constitution in AGENTS.md applies; read [the constitution](../../../.factory/CONSTITUTION.md) only if it is not in your context. Follow the [verifier contract](../../../.factory/roles/verifier.md) (already part of the exported factory-verifier agent). Read the mission/specification, `factory.json` and actual candidate. Ensure no code writer changes the workspace during capture. Commands execute with the host's environment and permissions; this tool is not a sandbox.

## Procedure

From the repository root run `uv run --locked --project .factory software-factory verify --mission M-0001 --revision R-001`, substituting actual unused mission/run IDs. The configured commands are argv arrays and do not use shell interpretation. Inspect exit status, structured results and relevant local logs.

Compare the executed checks with the accepted criteria. A successful command can still omit necessary browser, integration or client behavior. Record such gaps explicitly and return them for resolution; do not silently alter the required check list.

Treat missing, skipped, failed, errored or timed-out required checks as failures. If tests mutate candidate content, invalidate that run and resolve the mutation before a new capture. Keep secrets out of shared evidence.

Bound task model results must reference the actual assignment and attempt. Record runtime identity or unknown separately from deterministic check outcomes. Model metadata does not substitute for executing checks. Save bound results before task completion; exact-model/effort requirements need matching runtime evidence.

Optional, when Jev is enabled: after results are recorded, you may run `uv run --locked --project .factory software-factory semantic verify-claims --mission ID --input PATH` on the implementer's key claims; treat needs_review/contradicts/no_evidence as prompts to inspect, never as approval. It is advisory and never evidence for the gate.

## Outputs and verification

Evidence under `.factory/missions/ID/evidence/RUN/`, with run identity and candidate fingerprint. Raw logs stay in ignored `.factory/local/runs/`; the gate checks their hashes, so another machine without those logs must rerun verification. Report exact checks and limitations to the orchestrator. Use `uv run --locked --project .factory software-factory gate --mission ID` to see remaining readiness requirements; verification alone does not constitute independent review. Return the final fingerprint for current task results as well as review.

## Failure behavior

Return the recorded failure and likely cause. Use a new run ID for subsequent evidence. Never hand-edit a result to transform failure into pass or treat an unavailable dependency as a successful check.
