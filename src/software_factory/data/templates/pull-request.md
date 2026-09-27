# Pull request packet

## Change

Explain the concrete problem and resulting behavior. Link the accepted specification and describe important design tradeoffs only when they help review.

## Evidence

Identify the exact candidate fingerprint, executed check outcomes and the latest independent review (ID, author, fingerprint, status). Link evidence and state any unavailable or manual validation. Refresh the gate after source/configuration changes.

## Scope

Describe compatibility, migrations and user-visible behavior changes. Surface changes to tests, acceptance criteria or factory controls explicitly.

## Risks

Copy or refine the authored Risks section from the plan or specification: each material risk with its impact and mitigation. `software-factory packet --kind pr` refuses to prepare a packet when that section is missing, empty or unedited template text, or when `recovery.md` is unedited.

## Delivery and recovery

State the completion boundary, actual remote CI/PR status if available, rollout needs and recovery implications. A prepared packet is not a created PR, merge or deployment. Record remote CI with `software-factory mission ci-result`; MERGED needs a successful CI result for the candidate commit and the actual merge commit.
