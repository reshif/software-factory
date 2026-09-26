# Phase 0 — Software Factory Experiment Kit

A 2-week, go/no-go experiment from [software-factory-final-draft.md §19.1](../software-factory-final-draft.md). **No platform is built.** Claude runs in plain GitHub Actions through `anthropics/claude-code-action`. Humans only approve or reject.

## The question

> Can humans approve agent-prepared packets **without touching code**, **faster than today's PR review**, and with a **rejection rate above zero**?

**GO** if ≥ 6 of 10 items reach staging with **zero interventions outside approvals**, *and* total packet-review minutes are below the PR-review baseline. `experiment/analyze.py` computes this.

## What's in the kit

```text
phase0-experiment/
├── README.md                                   ← this file
├── CLAUDE.md                                   ← merge into the pilot repo's CLAUDE.md
├── .github/
│   ├── workflows/factory-experiment.yml        ← two label-triggered jobs: plan, then build
│   └── ISSUE_TEMPLATE/factory-experiment.md    ← one issue per backlog item
└── experiment/
    ├── items.md                                ← the 10 chosen items
    ├── baseline.md                             ← how to measure today's PR review time
    ├── templates/
    │   ├── h1-packet.md                        ← mission packet Claude posts on the issue
    │   ├── pr-evidence.md                      ← PR body Claude must fill
    │   └── release-proposal.md                 ← release proposal Claude posts on the PR
    ├── results.csv                             ← one row per item, filled in by the observer
    └── analyze.py                              ← computes GO / NO-GO
```

## Setup (about half a day)

1. **Copy** `.github/`, `CLAUDE.md` and `experiment/` into the pilot repo. Merge `CLAUDE.md` with the existing one if there is one.
2. **Install the Claude GitHub App** on the pilot repo (`/install-github-app` in Claude Code, or install it manually from github.com/apps/claude).
3. **Create a dedicated API key** in a separate Anthropic Console workspace with a **spend limit of $500**. That limit is the experiment's external budget cap. Add the key as the repo secret `ANTHROPIC_API_KEY`.
4. **Create the labels** `factory:plan`, `factory:build` and `factory:experiment`.
5. **Edit the allowed test commands** in `factory-experiment.yml` (search for `EDIT:`) to match the repo's real test and lint commands.
6. **Restrict who can label.** Only the PO, TL and observer should have triage rights, because applying `factory:build` *is* the H1 approval.

## How each item flows

| Step | Who | Action | Counts as |
|---|---|---|---|
| 1 | Observer | Opens an issue from the template and applies `factory:plan` | — |
| 2 | Claude | Posts an **H1 packet** comment (plan, scope, risks, estimate). **No code.** | — |
| 3 | PO/TL | Reads the packet. **Approve:** apply `factory:build`. **Reject:** comment the reason and close. | **H1** (minutes logged) |
| 4 | Claude | Implements on a branch, runs tests, opens a PR using `pr-evidence.md`, posts a **release proposal** | — |
| 5 | TL | Reviews the PR. Approve and merge, or request changes (Claude can be re-invoked with `@claude`). | **HM** (minutes logged) |
| 6 | TL | Releases through the normal process after reading the release proposal | **H2** (minutes logged) |
| any | Anyone | **Edits code, fixes CI, or debugs for Claude** | ⚠️ **Intervention outside approvals.** Log it. |

Requesting changes through a PR review comment counts as an approval decision, not an intervention. A human pushing a commit **is** an intervention.

## The observer's rules

- Log minutes **honestly**, including time spent reading the packet.
- Log every intervention with a one-line reason.
- Record `$` per item from the Console workspace usage, and the CI minutes from the Actions run.
- Don't help Claude outside the defined steps. The point is to measure the gap.

## Important: this is NOT the production security model

For speed, Phase 0 lets `claude-code-action` push branches and open PRs with the action's token. The Phase 2 design removes that power (§13 of the final draft: controller-only push, network-off execution, holdouts in a separate repo). So:
- **Use a non-regulated repo** with no production secrets available to Actions.
- **Don't** enable `pull_request_target` workflows.
- Keep branch protection on `main` with required review.

## After 2 weeks

```bash
python experiment/analyze.py experiment/results.csv
```

Take the output to the factory owner. **GO** → Phase 2. **NO-GO** → improve specs and verification, then rerun.
