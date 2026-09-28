---
name: factory-review
description: Independently review a factory candidate by kind (code, acceptance or adversarial) against its criteria, real diff and evidence, returning per-criterion verdicts and verified findings for that exact fingerprint.
---

# Review a candidate independently

## Inputs and preconditions

The constitution in AGENTS.md applies; read [the constitution](../../../.factory/CONSTITUTION.md) only if it is not in your context. Follow the [reviewer contract](../../../.factory/roles/reviewer.md). Work from the brief the orchestrator generated with `software-factory mission brief --mission ID --kind code|acceptance|adversarial` and its `diff.patch`. Use a context separate from implementation, and prefer a model different from the implementer's where available. Do not spawn nested agents. If a separate context is unavailable, report that; do not simulate independence.

## Kinds

- **code:** correctness, regressions, tests and test integrity, scope, factory-control changes, failure paths.
- **acceptance:** does the diff deliver what the user asked? Use only the acceptance brief (request, clarifications, criteria, exclusions, diff). Do not read the spec rationale or implementer report. Its `brief_hash` is required.
- **adversarial:** try to break each AC: edge inputs, concurrency, error paths, misuse. Required at high risk.

The lane and `mission risk` decide which kinds the gate requires.

## Procedure

1. Read the code and diff first. Read the implementer's report last, as claims to falsify.
2. For each AC ask: what would be observably wrong if this were not delivered, and does any recorded check or test actually exercise it? An untested AC is a verification gap.
3. Give each AC a verdict: `pass`, `fail` or `needs_human`. Use `needs_human` when you cannot decide from the evidence.
4. Report only real defects; there is no quota, and zero findings is a valid result. Each finding has exactly these keys: a unique `id` (required when blocking), `severity` (`blocking` or `nonblocking`), `path`, `message` (the defect, its impact and supporting evidence in one text) and optional `verified` (true only when you reproduced or directly confirmed it); other keys are rejected. Use [the review template](../../../.factory/templates/review.md).

## Outputs and verification

A review JSON for the actual fingerprint: `kind`, `status` (`pass` or `changes_requested`), `criteria_verdicts` (`{"AC-1": "pass"}`), `brief_hash` (the `sha256` that `mission brief` returned for your brief; required for acceptance), findings, author/source and creation time. Return it as text; the orchestrator records it with `software-factory mission review --mission ID --input -` while the mission is REVIEWING or READY_PR. The latest review of each required kind must be current, pass and have no unresolved blocking finding; the acceptance-bearing review needs `pass` for every AC. A later review must list each earlier unresolved blocking finding in `resolutions` as `{"finding": ID, "reason": TEXT}`, even for a new fingerprint. A blocking finding recorded without an `id` by an earlier release is resolved as `REVIEW-ID-FN` (its 1-based position N). A local author string does not authenticate a human or prove independence.

If code changes during review, repeat the affected kinds against the new fingerprint. Optional JEV claim assessment (`semantic verify-claims`) is advisory and never gate evidence.

## Failure behavior

Report unavailable evidence as a limitation and do not return a complete `pass`. Do not edit the candidate or fix findings; return repairs through the orchestrator.
