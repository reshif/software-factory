# Candidate review

## Identity and coverage

Record mission ID, exact candidate fingerprint, specification version, actual reviewer session/person and time. List examined code, acceptance criteria and evidence, with any coverage limitation.

## Findings

For each finding record a unique `id` (required when blocking), severity (`blocking` or `nonblocking`), path, concrete defect, impact and supporting evidence. Include reproduction steps when practical. Keep suggestions separate from defects.

## Disposition

List each earlier unresolved blocking finding under resolutions as `{"finding": ID, "reason": TEXT}`; the gate stays closed until every one is resolved.

Use `changes_requested` for unresolved blocking findings. A `pass` means the current candidate was examined and no blocking finding remains; explain remaining nonblocking risk. Do not treat an unavailable check as passing.

Return the corresponding structured review to the orchestrator for `software-factory mission review`. Do not modify the candidate while reviewing it.
