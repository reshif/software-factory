# Mission specification

This is an authoring template. Replace its guidance with the actual mission facts before moving to PLANNED; it is not an accepted specification by itself. `mission accept-scope` refuses a spec.md that is still this template, only headings, or adds fewer than 20 characters of its own text.

## Outcome and context

Describe the requested user-visible result and current behavior. Reference the originating request or issue and distinguish observations from assumptions.

## Scope

List included components, relevant paths and constraints. Reference existing authorization; identify any material scope decision still needed.

## Acceptance criteria

Give each criterion a stable ID `AC-<n>`. Describe an observable condition, representative input, expected result and validation route (`check`, `e2e`, `property`, `manual` or `review`). The EARS form is allowed ("When <trigger>, the <system> shall <response>"). Include relevant failure cases. Use configured check IDs when available; route `check` requires at least one.

Cite for every criterion one or more excerpts copied exactly from `request.md` or `clarifications.md`, each at least 8 characters after whitespace normalisation; `mission criteria` refuses a shorter excerpt or one that matches nothing (whitespace differences are ignored). Record the same criteria as JSON with `software-factory mission criteria`.

## Exclusions

List requested items that are deliberately out of scope, each quoting its request excerpt and naming the `exclusion` decision (`decision` ID) that records the user's agreement. That decision must be bound to the current request chain head (`request.chain` in `mission status`); `exception` and `decline` decisions and `<...>` placeholders are refused. A later clarification moves the chain head, so record a new exclusion decision and run `mission criteria` again.

## Ambiguities

List each open question as `Q-<n>` with status `open`, `resolved` or `waived`. A `resolved` or `waived` ambiguity names an existing decision (`decision` ID). Scope cannot be accepted while any ambiguity is open; resolve it with `mission clarify` and a decision.

## Dependencies and decisions

Record required services, versions and external dependencies. Cite official documentation supporting version-sensitive assumptions. State unresolved decisions and their effect on implementation.

## Risks

Record known risks to users, data or operations and how acceptance or rollout addresses them. The plan's Risks section may refine this; the PR packet uses the first authored one.

## Change and recovery implications

Describe compatibility, rollout and code/data recovery implications appropriate to the product. Say when delivery is outside this mission's completion boundary.
