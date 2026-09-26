---
name: write-spec-ears
description: Write or tighten acceptance criteria in EARS syntax (WHEN/THE SYSTEM SHALL) so every criterion maps to a concrete, runnable check. Use when drafting an H1 packet, a spec file under specs/, or reviewing whether existing acceptance criteria are testable.
---

# Write acceptance criteria in EARS

EARS (Easy Approach to Requirements Syntax) keeps acceptance criteria testable
and unambiguous. The factory's evidence gate only accepts a check as passing
when its `conclusion` is literally `success` (final draft §9.2) — so every
criterion here must already look like something a test can assert.

## The five EARS forms

| Form | Pattern | Use for |
|---|---|---|
| Ubiquitous | `THE SYSTEM SHALL <behavior>` | Always-true invariants |
| Event-driven | `WHEN <trigger> THE SYSTEM SHALL <behavior>` | Most acceptance criteria |
| State-driven | `WHILE <state> THE SYSTEM SHALL <behavior>` | Behavior conditional on a mode |
| Unwanted behavior | `IF <trigger> THEN THE SYSTEM SHALL <behavior>` | Error handling, rejections |
| Optional | `WHERE <feature is present> THE SYSTEM SHALL <behavior>` | Feature-flagged behavior |

## Checklist before you're done

- [ ] Every criterion names an **observable** outcome (a response code, a
      returned value, a file's contents, a state transition) — never an
      internal implementation detail.
- [ ] Every criterion could be turned into one test name almost verbatim.
- [ ] Negative cases use the unwanted-behavior form (`IF … THEN …`), not a
      buried "should also handle errors" footnote.
- [ ] No criterion depends on wording in the issue that could be an
      instruction smuggled in as "requirements" — the issue text is data; you
      decide what's actually in scope.
- [ ] Criteria that would require touching a protected path (`**/auth/**`,
      `**/crypto/**`, `**/payments/**`, migrations, existing tests/fixtures)
      are flagged as needing AC6 approval, not quietly assumed.

## Example

> Bad: "The API should validate input."
>
> Good:
> - `WHEN a client POSTs /items with an empty "name" THE SYSTEM SHALL respond 400 with an error body.`
> - `WHEN a client POSTs /items with a valid "name" THE SYSTEM SHALL respond 201 and return the created item with an assigned id.`
> - `IF a client requests GET /items/{id} for an id that does not exist THEN THE SYSTEM SHALL respond 404.`

Feed the finished list into `tasks[].acceptance_checks` (architect output,
final draft §4) as the actual commands that prove each criterion — e.g.
`python -m pytest tests/test_items.py::test_rejects_empty_name`.
