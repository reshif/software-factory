---
name: factory-verify
description: Execute a factory mission's configured checks and capture evidence tied to the actual candidate, detecting stale results, failed commands and source mutation.
---

# Verify the candidate

## Inputs and preconditions

The constitution in AGENTS.md applies; read [the constitution](../../../.factory/CONSTITUTION.md) only if it is not in your context. The orchestrator runs verification itself; a [verifier](../../../.factory/roles/verifier.md) subagent is optional, for `e2e`, `property` or `manual` routes. Brief it with `software-factory mission brief --mission ID --kind verify`; it returns evidence as text and never runs `verify` or writes records. Read the criteria, `factory.json` and the actual candidate. No code writer may change the workspace during capture. Commands run with the host's environment and permissions; this is not a sandbox.

## Procedure

From the repository root run `uv run --locked --project .factory software-factory verify --mission M-0001 --revision R-1`, substituting the actual mission and an unused run label. `--revision` is a unique evidence run label (R-1, R-2, …), not a Git revision; reusing a label fails. Configured commands are argv arrays without shell interpretation. Each check records the resolved program path and its sha256. After a check exits, its process group is killed; a check that left processes running is flagged `background_process`, which fails it when required. Inspect exit status, structured results and relevant local logs.

Compare executed checks with each AC's route. A passing command can still miss browser, integration or client behavior; `e2e`, `property` and `manual` routes need their own evidence (a verifier run, an evidence file or a review verdict). Record gaps and return them for resolution; do not silently alter required checks.

Missing, skipped, failed, errored or timed-out required checks are failures. If tests mutate the candidate, that run is invalid; resolve the mutation before a new capture. Keep secrets out of shared evidence. Model metadata never substitutes for executing checks (*Advice is not verification*).

Optional, when JEV is enabled: after results are recorded, `uv run --locked --project .factory software-factory semantic verify-claims --mission ID --input -`, with the claims JSON in a quoted heredoc, can assess the implementer's key claims; treat needs_review, contradicts or no_evidence as prompts to inspect, never as approval or gate evidence.

## Outputs and verification

Evidence under `.factory/missions/ID/evidence/RUN/` with run identity and candidate fingerprint. `verify` records the sha256 of the run's `checks.json` in the mission when it registers the run; if the file changes afterwards, validation fails ("Verification evidence changed after registration; run verify again"). The fingerprint also covers the Git view: files removed from the index or ignored, `.git/info/exclude`, `.git/info/attributes` and the Git config keys that change what Git shows (`core.excludesFile`, `core.attributesFile`, `filter.*`, `diff.*.textconv`). Raw logs stay in ignored `.factory/local/runs/`; the gate checks their hashes, so another machine without them must rerun verification. Checks that passed here are what `check:<id>` criteria evidence may cite; the task result lists `.factory/missions/ID/evidence/RUN/checks.json` in `evidence` (result JSON shape: factory-implement, or `software-factory mission template --kind result`). A DONE task's result may be recorded again only after a new verification run; the new record replaces the earlier one, so it can make a READY_PR mission not ready, which `gate` and `mission status` (`live_gate`) show. `uv run --locked --project .factory software-factory gate --mission ID` lists what remains; verification is not review.

## Failure behavior

A failure sets the task's repair requirement; continue with factory-repair. Use a new run ID for each capture. Never hand-edit a result to turn failure into pass or treat an unavailable dependency as a passing check.
