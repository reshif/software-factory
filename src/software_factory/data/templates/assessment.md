# Mission assessment

Written by the planner after the context and the interview, before the specification. The orchestrator shows it to the user before asking for scope approval: the user's `approve <MISSION> scope` means "I have seen these blockers, concerns and risks and I am ready to proceed". Replace this guidance with the real assessment; `mission accept-scope` refuses an unedited template or a missing section.

## What I understood

The request restated in plain words: the outcome, who it is for, how the user will judge it done, and what must not change. The user confirms or corrects this.

## Blockers

Anything that stops the work as requested (missing access, contradictions, unknowns only the user can answer). Write "None." when there are none.

## Concerns

Where the request may be the wrong thing to do. One item per concern, `C-<n>`:

- **C-1: claim.** Evidence: repository path and lines, test, or official documentation URL. Risk if we proceed as asked: … Recommended alternative: …

Write "None." only when there is genuinely nothing to challenge.

## Risks and tradeoffs

Security, data, compatibility, performance, cost and delivery risks of doing what was asked, with their likelihood and impact.

## Assumptions

Defaults chosen for questions the user left to judgment, each marked as an assumption.

## Readiness

One of: ready; ready with accepted risks (list them); needs answers (list them); recommend a change (say which).
