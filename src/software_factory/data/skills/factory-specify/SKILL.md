---
name: factory-specify
description: Gather mission context, then define request-traced acceptance criteria, exclusions and open questions for a factory mission before implementation or a material scope change.
---

# Establish context and the specification

## Inputs and preconditions

The constitution in AGENTS.md applies; read [the constitution](../../../.factory/CONSTITUTION.md) only if it is not in your context. The planner follows the [planner contract](../../../.factory/roles/planner.md); the orchestrator only briefs and records. The mission must exist with its verbatim `request.md` (`mission create --request-file`). The request and its clarifications are the contract (*The request is the contract*); reference documents are evidence.

## Procedure

1. **Context first (both lanes).** The orchestrator runs `software-factory mission brief --mission ID --kind context` and gives the brief to a planner. The planner returns the context document: codebase map of the affected area, conventions, affected files and tests, dependencies, external documentation with URLs and access dates, and open questions. A patch mission gets a short context. Use `--kind research` for an extra cited investigation when a material unknown remains. The orchestrator records it unchanged with `mission record-doc --mission ID --doc context --input -`; it must differ from the template before scope can be accepted.
2. **Interview in rounds.** Collect every question whose answer would change the result as `Q-n`, each citing the evidence behind it, why it matters and a suggested default the user can accept with "ok". The orchestrator asks one round at a time (with a recipe, round 0 asks whether each of its defaults is still true), includes the planner's "probably not considered" question and every contradiction with saved knowledge, records each round with `mission clarify --mission ID --input -` as the questions and defaults shown followed by the user's reply verbatim, and runs another round when answers open new questions. There is no limit on questions or rounds: stop when no remaining answer would change the result or the user says go, and list what is still open as assumptions in the assessment. Cover what the request leaves unclear: goal and users, how done is judged, what must not change, constraints, what is out of scope, how to undo it. Routine choices that follow product conventions become assumptions in the assessment, not questions.
2a. **Assess and challenge.** `software-factory mission brief --mission ID --kind assess` → the planner returns assessment.md from [the assessment template](../../../.factory/templates/assessment.md): what it understood, blockers, concerns `C-n` with evidence, risk and a recommended alternative, risks and tradeoffs, assumptions and readiness. The orchestrator records it with `mission record-doc --doc assessment` and shows it to the user before the spec; the user's answers to concerns are recorded verbatim with `mission clarify`. Accept-scope refuses a missing, template or incomplete assessment, and scope approval (chat or terminal) is refused until it exists, so the user always approves having seen the blockers and risks.
3. **Specification.** The orchestrator runs `software-factory mission brief --mission ID --kind plan` and gives the brief to the planner, which drafts spec.md from [the specification template](../../../.factory/templates/spec.md): outcome, in-scope components, constraints, failure cases and relevant security, data or accessibility needs. Keep existing tests and criteria visible; explain any proposed change to them. Accept-scope refuses a spec.md that is still the template or adds fewer than 20 characters of its own text (the same rule as context.md).
4. **Criteria.** The planner proposes the criteria JSON for `mission criteria`:

```json
{"items": [{"id": "AC-1", "text": "When a user submits an empty title, the system shall show an error and save nothing.",
            "excerpts": ["exact words from request.md or clarifications.md"], "route": "check", "checks": ["tests"]}],
 "exclusions": [{"excerpt": "exact words the mission will not deliver", "decision": "D-EXCLUDE-1"}],
 "ambiguities": [{"id": "Q-1", "text": "Which storage?", "status": "resolved", "decision": "D-STORAGE-1"}]}
```

Each AC is observable and cites at least one exact excerpt of the request or clarifications, at least 8 characters long (whitespace is normalised; a paraphrase fails). EARS phrasing ("When …, the system shall …") is welcome. `route` is `check` (needs configured check ids), `e2e`, `property`, `manual` or `review`. Every requested item is either covered by an AC or listed as an exclusion whose decision the orchestrator records with `mission decision` from the user's actual answer. Record exclusion and ambiguity decisions before `mission criteria`: it validates excerpts and decision ids immediately. A `resolved` or `waived` ambiguity must name an existing decision, and a `<...>` placeholder is refused as a decision reference. No ambiguity may remain `open` at accept-scope. An exclusion needs a decision of kind `exclusion` bound to the request as it currently stands: `subject_hash` is the request chain head, `request.chain` in `mission status --mission ID`. Every clarification moves it, so after one the orchestrator records new exclusion decisions for the new head and runs `mission criteria` again:

```sh
uv run --locked --project .factory software-factory mission decision --mission ID --input - <<'EOF'
{"id": "D-EXCLUDE-1", "kind": "exclusion", "subject_hash": "<request.chain from mission status>", "reference": "<where and how the user agreed to exclude it>"}
EOF
```

`exception` and `decline` decisions are no longer accepted for exclusions. `software-factory mission template --kind criteria` and `--kind decision` print minimal skeletons; unknown keys, in criteria and in decisions alike, are rejected with an error that lists the allowed fields.

## Outputs and verification

Context and spec text and a criteria JSON, returned to the orchestrator as text. The orchestrator pipes them into `mission record-doc --doc context|spec --input -` and `software-factory mission criteria --mission ID --input -` (quoted heredoc). After recording the spec, the orchestrator shows it to the user, who approves it themselves. In Claude Code the user replies in chat with a line reading `approve ID scope`; the factory's UserPromptSubmit hook records the decision from their own message and binds it to the current spec.md. In other clients, or as a fallback, the user runs this in their terminal (an agent cannot: `mission decision` refuses scope, exception, merge and release decisions, and `mission approve` needs an interactive terminal and a typed mission ID):

```sh
uv run --locked --project .factory software-factory mission approve --mission ID --kind scope --reference '<where and how the user accepted>'
```

It binds the decision to the current spec.md hash and names it D-SCOPE-<first 8 hex> unless `--id` is given. Exclusion decisions have exactly `id`, `kind`, `subject_hash` and `reference` (the tool adds `recorded_at`); decision ids are unique per mission. If the scope decision is missing or stale, `mission accept-scope` fails and prints the exact `mission approve` command with the current hash. Run `mission accept-scope` once the plan's architecture exists (factory-plan). Accept-scope binds the spec and criteria hashes and records the hashes of context.md and plan.md; changing spec or criteria later, or a clarification after PLANNED, resets scope and tasks for revalidation while keeping attempts and history, and a changed context.md or plan.md fails later scope checks until accept-scope runs again. On a PAUSED or BLOCKED mission, clarify and criteria keep the hold; resume it with `--resolution` before accepting. It does not approve the changed scope itself: when the spec changed, the user runs `mission approve --kind scope` again, then re-run accept-scope.

## Failure behavior

When an answer is unavailable, name the blocked decision and its consequence and keep the ambiguity `open`; the mission cannot be accepted until the user resolves or waives it. Excerpts must be the user's exact words.
