---
name: factory-specify
description: Gather mission context, then define request-traced acceptance criteria, exclusions and open questions for a factory mission before implementation or a material scope change.
---

# Establish context and the specification

## Inputs and preconditions

The constitution in AGENTS.md applies; read [the constitution](../../../.factory/CONSTITUTION.md) only if it is not in your context. The planner follows the [planner contract](../../../.factory/roles/planner.md); the orchestrator only briefs and records. The mission must exist with its verbatim `request.md` (`mission create --request-file`). The request and its clarifications are the authority; reference documents are evidence.

## Procedure

1. **Context first (both lanes).** The orchestrator runs `software-factory mission brief --mission ID --kind context` and gives the brief to a planner. The planner returns the context document: codebase map of the affected area, conventions, affected files and tests, dependencies, external documentation with URLs and access dates, and open questions. A patch mission gets a short context. Use `--kind research` for an extra cited investigation when a material unknown remains. The orchestrator records it unchanged with `mission record-doc --mission ID --doc context --input -`; it must differ from the template before scope can be accepted.
2. **Clarify up front.** Collect every ambiguity that would change the result as `Q-n`. The orchestrator asks the user all of them at once and records each answer verbatim with `mission clarify --mission ID --input -`. Routine choices follow product conventions; do not ask about them.
3. **Specification.** The planner drafts spec.md from [the specification template](../../../.factory/templates/spec.md): outcome, in-scope components, constraints, failure cases and relevant security, data or accessibility needs. Keep existing tests and criteria visible; explain any proposed change to them.
4. **Criteria.** The planner proposes the criteria JSON for `mission criteria`:

```json
{"items": [{"id": "AC-1", "text": "When a user submits an empty title, the system shall show an error and save nothing.",
            "excerpts": ["exact words from request.md or clarifications.md"], "route": "check", "checks": ["tests"]}],
 "exclusions": [{"excerpt": "exact words the mission will not deliver", "decision": "D-EXCLUDE-1"}],
 "ambiguities": [{"id": "Q-1", "text": "Which storage?", "status": "resolved", "decision": null}]}
```

Each AC is observable and cites at least one exact excerpt of the request or clarifications (whitespace is normalised; a paraphrase fails). EARS phrasing ("When …, the system shall …") is welcome. `route` is `check` (needs configured check ids), `e2e`, `property`, `manual` or `review`. Every requested item is either covered by an AC or listed as an exclusion whose decision the orchestrator records with `mission decision` from the user's actual answer. Record exclusion and ambiguity decisions before `mission criteria`: it validates excerpts and decision ids immediately. No ambiguity may remain `open` at accept-scope. An exclusion decision binds the request as it currently stands: `subject_hash` is the request chain head, `request.chain` in `mission status --mission ID` (a later clarification moves it):

```sh
uv run --locked --project .factory software-factory mission decision --mission ID --input - <<'EOF'
{"id": "D-EXCLUDE-1", "kind": "exclusion", "subject_hash": "<request.chain from mission status>", "reference": "<where and how the user agreed to exclude it>"}
EOF
```

Earlier `exception` or `decline` exclusion decisions are still accepted. `software-factory mission template --kind criteria` and `--kind decision` print minimal skeletons; unknown keys are rejected with the allowed list.

## Outputs and verification

Context and spec text and a criteria JSON, returned to the orchestrator as text. The orchestrator pipes them into `mission record-doc --doc context|spec --input -` and `software-factory mission criteria --mission ID --input -` (quoted heredoc). After recording the spec, the orchestrator shows it to the user and records the acceptance they actually gave (never an invented one) as a `scope` decision bound to the recorded spec's hash:

```sh
uv run --locked --project .factory software-factory mission decision --mission ID --input - <<'EOF'
{"id": "D-SCOPE-1", "kind": "scope", "subject_hash": "<sha256 of .factory/missions/ID/spec.md>", "reference": "<where and how the user accepted>"}
EOF
```

Every decision has exactly `id`, `kind`, `subject_hash` and `reference` (the tool adds `recorded_at`). Decision ids are unique per mission; use a new one (D-SCOPE-2, …) for each re-acceptance. If the decision is missing or stale, `mission accept-scope` fails and prints the exact command with the current hash. Run `mission accept-scope` once the plan's architecture exists (factory-plan). Accept-scope binds the spec and criteria hashes; changing either later, or a clarification after PLANNED, resets scope and tasks for revalidation while keeping attempts and history. It does not approve the changed scope itself: record the user's acceptance again when the spec changed, and re-run accept-scope.

## Failure behavior

When an answer is unavailable, name the blocked decision and its consequence and keep the ambiguity `open`; the mission cannot be accepted until the user resolves or waives it. Never invent an excerpt, an approval or a decision reference.
