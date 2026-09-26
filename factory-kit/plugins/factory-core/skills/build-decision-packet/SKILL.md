---
name: build-decision-packet
description: Assemble the contents of an H1, HM, H2 or HX decision packet so a human can approve or reject without touching code. Use whenever architect or coordinator output needs to be turned into the material a human approver will actually read.
---

# Build a decision packet

A human approves or rejects **from the packet alone** (final draft §10) —
never by reading the whole codebase. If something the approver needs isn't in
the packet, the approval isn't informed, no matter how good the code is.

## Required contents (final draft §10)

Every packet must include, in a form a non-engineer approver can act on:

- **The recommendation and alternatives** — one sentence why, plus what else
  was considered and rejected.
- **The raw diff and a preview** — not a paraphrase. Approvers read the actual
  change.
- **The evidence**: check results (with which policy rule fired for the risk
  class) and the holdout pass/fail count.
- **The risk class and the rule that fired** — e.g. "AC6: protected path
  `src/auth/session.py`", not just "medium risk".
- **The blast radius** — what else could this affect.
- **For H2 only: every mission and commit in the digest**, not just the one
  that triggered the packet.
- **The recovery plan** — code and data impact both.
- **Cost** — dollars spent so far and estimated to finish.
- **The untrusted inputs the agents read** — issue bodies, web pages, files —
  so the approver knows what could have been an injection vector.
- **Agent, model and kit versions**, and the request id, nonce, required
  roles and expiry.

## Writing it so a human can act fast (§6.5 — approval capacity is the WIP limit)

- Lead with the recommendation, not the history.
- Put scope boundaries up front: what changed, what deliberately did not.
- Name protected files touched explicitly — "none" is a real, useful answer.
- State the rollback in one line: revert PR / disable flag `<name>` / data
  impact: none.
- Never bury a risk in prose the approver has to hunt for — a bullet list of
  risks beats a paragraph every time.

## What never belongs in a packet

- Untrusted text presented as if it were the controller's own claim — always
  attribute it ("the issue author wrote: …") so the approver knows its
  provenance.
- A request for the approver to also fix something — that's scope creep;
  flag it as a follow-up instead.
- Anything that assumes approval — the packet describes the decision to be
  made, not a fait accompli.
