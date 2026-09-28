# Mission specification

This is an authoring template. Replace its guidance with the actual mission facts before moving to PLANNED; it is not an accepted specification by itself.

## Outcome and context

Describe the requested user-visible result and current behavior. Reference the originating request or issue and distinguish observations from assumptions.

## Scope

List included components, relevant paths and constraints. Reference existing authorization; identify any material scope decision still needed.

## Acceptance criteria

Give each criterion a stable ID `AC-<n>`. Describe an observable condition, representative input, expected result and validation route (`check`, `e2e`, `property`, `manual` or `review`). The EARS form is allowed ("When <trigger>, the <system> shall <response>"). Include relevant failure cases. Use configured check IDs when available; route `check` requires at least one.

Cite for every criterion one or more excerpts copied exactly from `request.md` or `clarifications.md`; `mission criteria` refuses an excerpt that matches nothing (whitespace differences are ignored). Record the same criteria as JSON with `software-factory mission criteria`.

## Exclusions

List requested items that are deliberately out of scope, each quoting its request excerpt and naming the existing decision (`decision` ID) that excluded it.

## Ambiguities

List each open question as `Q-<n>` with status `open`, `resolved` or `waived` (waiving needs a decision). Scope cannot be accepted while any ambiguity is open; resolve it with `mission clarify` or a decision.

## Dependencies and decisions

Record required services, versions and external dependencies. Cite official documentation supporting version-sensitive assumptions. State unresolved decisions and their effect on implementation.

## Risks

Record known risks to users, data or operations and how acceptance or rollout addresses them. The plan's Risks section may refine this; the PR packet uses the first authored one.

## Change and recovery implications

Describe compatibility, rollout and code/data recovery implications appropriate to the product. Say when delivery is outside this mission's completion boundary.
