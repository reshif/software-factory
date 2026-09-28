---
name: factory-review
description: Independently review a factory candidate against its specification, real diff and verification evidence, returning actionable findings for that exact fingerprint.
---

# Review a candidate independently

## Inputs and preconditions

The constitution in AGENTS.md applies; read [the constitution](../../../.factory/CONSTITUTION.md) only if it is not in your context. Follow the [reviewer contract](../../../.factory/roles/reviewer.md) (already part of the exported factory-reviewer agent). Read the accepted spec, candidate diff and recorded checks. Use separate context from implementation. If separate review is unavailable, report that fact; do not simulate independence with a second paragraph.

## Procedure

Inspect changed and relevant surrounding code, acceptance behavior, tests and failure paths. Investigate changes to assertions, factory configuration or required checks. Validate vendor-specific assumptions against official documentation when material.

Produce findings with severity, path, impact and supporting evidence using [the review template](../../../.factory/templates/review.md). Separate blocking defects from suggestions. Resolve uncertainty by investigation where possible; do not create confident findings from guesses.

Return a structured review for the actual fingerprint, with status `pass` or `changes_requested`, findings, author/source and creation time. The orchestrator records it using `software-factory mission review --mission ID --input PATH`. PATH is repository-root-relative (for example `.factory/local/reviews/V-ID.json`); absolute paths and symlinks are refused. Reviews are accepted only while the mission is REVIEWING or READY_PR. Every blocking finding needs an `id`; a later review must list each earlier unresolved blocking finding in `resolutions` as `{"finding": ID, "reason": TEXT}`, even for a new fingerprint, or the gate stays closed. A blocking finding recorded without an `id` by an earlier release is resolved as `REVIEW-ID-FN` (its 1-based position N in that review). A local author string does not authenticate a human or prove independence.

For model selection changes, inspect actual assignment hashes, inventory provenance, rejected constraints and requested-versus-observed evidence. Different models do not establish review independence. A selected/configured name is insufficient proof of effective execution.

Optional, when Jev is enabled: after results are recorded, you may run `uv run --locked --project .factory software-factory semantic verify-claims --mission ID --input PATH` on the implementer's key claims; treat needs_review/contradicts/no_evidence as prompts to inspect, never as approval. It is advisory and never evidence for the gate.

## Outputs and verification

The review report and schema-compatible review input. A pass requires examination of the current candidate and no unresolved blocking findings. If code changes during review, repeat the affected review against the new fingerprint.

## Failure behavior

Report unavailable evidence and review limitations. Do not edit the candidate as reviewer or self-resolve defects on the implementer's behalf. Return repairs through the orchestrator.
