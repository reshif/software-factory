# Candidate review

## Identity and coverage

Record mission ID, exact candidate fingerprint, specification version, actual reviewer session/person and time. List examined code, acceptance criteria and evidence, with any coverage limitation.

Record the review `kind`: `code` (default), `acceptance` (sees only the acceptance brief: request, clarifications, criteria, exclusions and diff) or `adversarial` (tries to break each criterion). Set `brief_hash` to the sha256 that `software-factory mission brief` reported for the brief you reviewed; an acceptance review requires it and the gate recomputes it.

Read the code and diff first. Read the implementer report last, so it cannot anchor your judgement.

## Criteria verdicts

For every criterion ask what verification gap could let it pass while the behaviour is wrong, then record `criteria_verdicts` as `{"AC-1": "pass" | "fail" | "needs_human"}`. Any `fail` or `needs_human` verdict blocks readiness. There is no finding quota: report only real defects.

## Findings

Each finding has only these keys: a unique `id` (required when blocking), `severity` (`blocking` or `nonblocking`), `path`, `message` and optional `verified`. Write the concrete defect, its impact and supporting evidence (with reproduction steps when practical) together in `message`; there are no separate impact or evidence keys. Set `verified` to true only when you reproduced the defect. Keep suggestions separate from defects.

## Disposition

List each earlier unresolved blocking finding under resolutions as `{"finding": ID, "reason": TEXT}`; the gate stays closed until every one is resolved.

Use `changes_requested` for unresolved blocking findings. A `pass` means the current candidate was examined and no blocking finding remains; explain remaining nonblocking risk. Do not treat an unavailable check as passing.

Return the corresponding structured review to the orchestrator for `software-factory mission review`. Do not modify the candidate while reviewing it.
