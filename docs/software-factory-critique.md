# Critique: Software Factory Final Draft

> **Reviewed document:** [software-factory-final-draft.md](software-factory-final-draft.md)
> **Date:** 2026-09-26
> **Reviewers (independent, in parallel, no file access to edit):**
> - **Fable:** strategy, operating model, coherence
> - **Claude Opus 5.5:** technical architecture, correctness, security
> - **Claude Sonnet 5:** feasibility, cost, fact-checking against official docs
>
> Where reviewers conflicted or made unverified claims, the adjudication is noted.

---

## 1. Verdict

All three reviewers agree the **core idea is sound**: a deterministic controller enforces, agents reason, and humans approve packets bound to exact inputs. The draft is **not yet decision-ready or safe to build**, for three reasons:

1. **The rules contradict each other.** Four dimensions set gate behavior (risk profile, autonomy level, lane, action class), and the draft never says which one takes precedence.
2. **Several security guarantees are claimed, not enforced.** Examples: hidden holdouts, the tool gateway, "no side channels", and approval replay protection. The MVP of GitHub labels + Actions + Environments **cannot enforce** several of them.
3. **It is sized for a 30-product organization, around a pilot nobody has run.** Approval load, factory ownership and total cost are missing. Phase 2 is realistic at about **10–16 person-weeks**, but only if a lot is cut.

**Recommendation:** run a **2-week experiment** before building any platform (§5). Then revise the draft into a smaller v1 with the fixes below.

---

## 2. Ranked findings (merged and de-duplicated)

### Critical

| # | Finding | Section | Failure scenario | Fix | Raised by |
|---|---|---|---|---|---|
| C1 | **The gate rules contradict each other.** The risk profile says experimental products auto-release. The autonomy level says everything starts at L3, where all gates are human. Lanes say every feature needs H1. The action-class table says a flagged feature is "auto build". | §5.2, §5.4, §7, §16 | Two readers (or the controller) resolve the same change to different gates | **One resolution function:** `gate = f(risk_profile, action_class, autonomy_level)`, with an explicit precedence rule (**strictest wins**). Fold lanes into action classes. Publish one decision table. | Fable, Opus |
| C2 | **Running tests is arbitrary code execution.** `bash:test` runs code the agent wrote (conftest.py, npm scripts). Hooks only see the command string. "No side channels" is false. | §13.5, §5.4, M5 | The agent adds a conftest.py that reads credentials and POSTs them out, and the hook approved "pytest" | Enforce at the **sandbox and network layer**: no default network, an egress proxy with an allowlist, no credentials mounted during tests. Treat every bash grant as arbitrary code execution. | Opus |
| C3 | **The "lethal trifecta" isn't broken.** Implementers read untrusted repo text and still have outbound paths: push, PR body, comments, MCP calls. | §13.4, §10 | An injected README gets secrets or holdout text into a public PR description, or into web-fetch MCP arguments | **Only the controller pushes** (from the diff). PR text is controller-generated. MCP servers sit behind the gateway with their own egress allowlist. | Opus |
| C4 | **Holdouts can't be both hidden and run by CI.** `tests/holdout/**` is in the product repo. The mandate's `evals/holdout/` isn't protected. Agent code in the same CI process can read or patch holdouts. | §8.2, §12.1, §12.2 | The agent reads holdout expectations and overfits to them | **A separate holdout repo with its own runner and identity.** Run it black-box against the *deployed staging digest*, and return only pass/fail counts. | Opus |
| C5 | **Agent PRs can hijack CI.** Same-repo PRs run the head commit's workflow YAML with secrets. `protected_paths` only acts at merge, after CI has already run the change. `pull_request_target` combined with a checkout of the head commit gives direct theft. | §13.2, M3 | An agent edits a workflow and exfiltrates repo secrets during CI | Agent identities have **no `workflows` permission**. Agents push to forks, or use `pull_request` with no secrets. Ban `pull_request_target` with a head checkout. Secrets only in Environment-scoped jobs. Turn off "Actions can create/approve PRs". | Opus |
| C6 | **Approvals can be replayed.** `operation_id` is deterministic, with no nonce and no consumed flag. Validation and execution are separate steps (TOCTOU). | §9, §12.4 | After a rollback, the old H2 re-authorizes redeploying the bad digest | **Single-use approvals**, consumed atomically at execution with a compare-and-swap and a fencing token. Add `mandate_id`, `attempt`, `nonce`, `approvers[]` with roles, and a signature. Define whether expiry means "must *start* before". | Opus |
| C7 | **Approval load is never estimated.** A standard product at L3 needs PO+TL at H1, a human on every merge and release, plus daily HX, with up to 6 named roles per product. A small team can't do this without rubber-stamping. | §1, §5.2, §9, §21 | One person is PO, TL and release owner, approves everything, and the rejection rate goes to 0 | Add an **approval budget** (decisions/week × minutes vs `reviewer_hours_per_week`). **Role-collapsing rules:** two-person rules apply to *regulated* only. | Fable |

### High

| # | Finding | Section | Fix | Raised by |
|---|---|---|---|---|
| H1 | **The MVP controller can't honor its guarantees.** Labels have no compare-and-swap, and anyone with triage access (or the agent token) can set "approved". Events made with GITHUB_TOKEN don't trigger workflows. Concurrency groups drop pending runs. There's no durable receipt store and no write-ahead intent. §20 scenario 5 (crash recovery) isn't achievable on labels alone. | M3, §11.1, §20 | A **small real state store** from Phase 2 (a Postgres table, or a signed state branch) and write-ahead intent records. Labels only *mirror* state. | Opus, Sonnet |
| H2 | **The MVP approval inbox doesn't work as specified.** GitHub Environments: (a) required reviewers on **private repos need Enterprise**; (b) **any one** listed reviewer approves, so two-human rules can't be enforced; (c) an approval binds to a workflow run, not a digest, and has no expiry. | §5.2, §11.1, §21 | Use **PR review + CODEOWNERS** (supports N approvals) for HM. For H1, H2 and HX, use a small **controller-owned approval service**, or budget for Enterprise. Bind to the digest in the controller. | Opus, Sonnet |
| H3 | **The state machine is incomplete.** Missing skip edges (standing mandate, auto-merge, standing release). No ARCHIVED, SPIKE_DONE, FLEET_PAUSED, EXPIRED or BUDGET_KILLED states. BLOCKED and HELD have no exits. Back-edges are missing. Task and mission states are mixed together. | §12.5 vs §6/§7/§15 | **Two machines** (task and mission) with a full transition table, and an exit and timeout for every side state | Opus |
| H4 | **Merge and release units don't match.** A mission's code is on main before release. A failed release check leaves unverified code on main. The next digest bundles several missions. | §6, §14 | Merge behind a flag per mission, or **auto-revert on post-merge failure**. H2 packets list **every mission in the digest**. | Opus |
| H5 | **Privilege escalation through the integration bot and the Learning agent.** An LLM doing conflict repair holds merge authority. The Learning agent reads untrusted text and proposes policy and eval changes. Kit PRs have no gate in the H1/HM/H2/HX model. | §10, §13.10 | Conflict resolutions go back through verification. **Only a deterministic bot holds merge credentials** and checks the evidence hash. Add a **kit-change gate** (two humans for policy and scenarios); evals are protected from the same PR. | Opus |
| H6 | **A model decides whether work falls under a standing mandate.** The Haiku intake agent decides "defect fix under N lines" vs "new scope". This is where scope creep and cost leaks enter. | §5.1, §6, §7 | Coverage is decided by **deterministic rules** (labels, paths, diff size), and anything ambiguous goes to H1. The discovery allowance is itself a mandate with $ and tool limits. | Fable |
| H7 | **Enforcement is claimed, but the MVP is client-side hooks.** PreToolUse hooks are Claude Code-only, can't see subprocesses, and don't apply to Codex. `max_budget_usd` is in-process, which contradicts "enforced outside the agent". | §11.2, §13.6, M5, M6 | State honestly that in Phase 2 **enforcement = branch protection + required checks + sandbox/network + an LLM gateway budget**, and hooks are defense in depth. | Fable, Opus, Sonnet |
| H8 | **Phase 2 is too big, and the sample config contradicts it.** The sample `factory.yaml` is *regulated* and requires mutation, twins and holdouts, but the pilot is *standard* and the MVP covers layers 1–3. With fail closed, every Phase 2 mission would block. | §19, §12.1, §11.1 | Phase 2 = one repo, feature lane, one worker, HM through ordinary PR review, **layers 1–2 + a black-box holdout**. Fix the sample to `standard`. List what's deliberately out. | Fable, Opus, Sonnet |
| H9 | **Nobody owns the factory.** There's no headcount, on-call, factory owner or RACI. Existing engineers' roles are undefined. Comprehension debt isn't addressed. | §4.2, §13.10 | An **Org section**: factory owner, RACI, platform FTE in the economics, and approvers keep coding part-time so they stay able to judge packets. | Fable |
| H10 | **The riskiest assumption is untested:** that reviewing packets is cheaper than reviewing PRs, and that humans rarely need to step in outside approvals. | §6, §9, §18 | Run the **2-week experiment** (§5) before Phase 2 | Fable |

### Medium

| # | Finding | Fix | Raised by |
|---|---|---|---|
| M1 | **Budgets are too tight.** $5/task and 2 repair attempts would send routine work to HX. A real small feature costs about $30–150. | Feature task **$8–15**, patch task **$2**, mission **$100–150** (tiered by lane), **3 repair attempts** in the feature lane | Sonnet |
| M2 | **Mutation, twin E2E and holdouts on every revision** multiply CI time 10–100x | **Diff-scoped mutation before merge**, full runs nightly, twins and holdouts **before release only** | Opus, Sonnet |
| M3 | "Append-only tests" can't be enforced: fixtures, mocks and conftest changes alter behavior | Diff-level rules: no edits to existing assertions or fixtures without HX; holdouts and mutation run against untouched tests | Opus |
| M4 | **Quorum semantics are broken.** The requester can be one of the two approvers. The regulated H1 sample has no security approver. `merge_policy: human` conflicts with the regulated two-human default. Mixed votes are undefined. | Quorum spec: distinct identities per role, requester excluded, any "revise" voids the decision | Opus |
| M5 | The M7 adapter is too thin: no `cancel` or `checkpoint`, no approval callback, no usage normalization. Session resume depends on local files that are lost with the sandbox. | Extend the interface. Persist session state outside the sandbox. | Opus |
| M6 | Contracts have no `schema_version`. The kit can be upgraded while a mission is running. | Add `schema_version`. **Pin kit and policy version at mandate creation**; re-validate at the next gate. | Opus, Sonnet |
| M7 | Economics count only tokens | Add **CI compute**, sandboxes, reviewer hours, platform FTE and Temporal operations; a **total-cost-of-ownership** view for the pilot; measure a **baseline** before Phase 2 | Fable, Sonnet |
| M8 | Vendor, legal and privacy risk is missing: model deprecation and pricing, provider outages, licensing and indemnity of AI code, training opt-out, PII classification | A **Risks section**; the exit test is one lane on a second runtime by Phase 3 | Fable |
| M9 | Missing practical pieces: how the approval inbox works concretely, credential bootstrapping (OIDC / GitHub App tokens / Vault), approver onboarding, a trace/replay tool, **feature-flag system not owned by any module** | Specify each for Phase 2; assign flags to M11 | Sonnet |
| M10 | Not decision-ready: 775 lines, and the ask is at the end, with no cost, timeline or stop conditions | A front page, **"Decision requested"**: pilot, people, budget, 90-day milestone, kill criteria | Fable |
| M11 | Single point of failure: GitHub is the tracker, controller, CI, inbox and release system | Accept this for the pilot; separate the controller state store in Phase 2 | Opus |

---

## 3. Cut or defer (to make v1 buildable)

| Item | Defer to |
|---|---|
| L0 portfolio layer (WSJF, 70/20/10 split, portfolio dashboard) | A spreadsheet until Phase 5 |
| M4 fleet orchestrator + FLEET lane | Phase 6 |
| M9 marketplace with stable and beta channels | A folder in the kit repo until Phase 3 |
| Enabling team, Learning agent, SPIKE and INCIDENT lanes | Phase 4+ |
| Codex adapter | Keep the interface; don't build it |
| Verification layers 3–7 (mutation, fuzz, contract, twins, visual) | Phase 3+ (keep a black-box holdout, which is cheap) |
| SBOM/SLSA/Sigstore, per-role model routing, separate release bot and deployment controller | Phase 3+ |
| Autonomy promotion automation | Phase 6 (manual promotion until then) |

---

## 4. Fact-check results

| Claim in the draft | Verdict | Note |
|---|---|---|
| Agent SDK `canUseTool`, `permission_mode`, `max_budget_usd`, `agents` | ✅ Verified | **Naming:** Python uses `can_use_tool`, `permission_mode`, `max_budget_usd`; TypeScript uses `canUseTool`, `permissionMode`, `maxBudgetUsd`. The draft mixes the two. `max_budget_usd` is **in-process**, not external enforcement. |
| claude-code-action triggers (@claude, assignee, label) | ✅ Verified | |
| Claude Managed Agents: managed harness + sandbox | ✅ Verified | "Worker pool" is our own term |
| Claude Code plugin marketplaces through managed settings | ✅ Verified | |
| GitHub required checks treat skipped/neutral as passing | ✅ Verified | The draft's fail-closed rule is right |
| GitHub Environments as the MVP approval gate | ❌ **Wrong as specified** | Private repos need Enterprise for required reviewers. Any single reviewer approves. Binds to a run, not a digest. |
| PreToolUse hooks as a gateway | ⚠️ Overstated | Claude Code only; can't see subprocesses |
| Model IDs `claude-opus-5-5`, `claude-sonnet-5`, `claude-haiku-4-5` | ✅ Valid | One reviewer flagged these as unverifiable. The current model list confirms Opus 5.5 = `claude-opus-5-5` and Sonnet 5 = `claude-sonnet-5`. Haiku 4.5's full ID is `claude-haiku-4-5-20251001`. **Move the IDs into `factory.yaml`** so they don't go stale in the doc. |
| Claude Code about $13/dev/day; caching about 90% off; Batch about 50% off | ✅ Verified | |
| DORA 2025: AI raises throughput, hurts stability | ✅ Verified | |
| METR: experienced developers 19% slower | ⚠️ **Stale** | METR's Feb 2026 update calls this historical and estimates about **18% speedup** with newer tools. Cite both. |
| §13 incident mapping (Replit → rule 2) | ❌ Mis-mapped | The Replit incident supports **rule 1** (prod credentials) |
| "Covers OWASP ASI01–ASI10" | ⚠️ Unsupported | Needs an explicit mapping table |

---

## 5. The experiment to run before Phase 2

**Assumption under test:** humans can approve agent-prepared packets (raw diff, evidence, recommendation) **without touching code or debugging**, **faster than today's PR review**, and with a **rejection rate above zero**.

**Setup (2 weeks, 1 engineer, no controller):**
1. Take **10 real backlog items** from the pilot repo, a mix of patch and small feature.
2. Run Claude Code in plain CI (claude-code-action) to produce, for each item, an **H1 packet**, a **PR with evidence**, and a **release proposal**.
3. Keep humans to approving or rejecting.
4. Measure:
   - reviewer minutes per decision vs the current PR review baseline
   - rejection rate
   - **interventions outside approvals**
   - % of items reaching staging untouched
   - $ per item, including CI minutes

**Decision rule:**

| Result | Action |
|---|---|
| ≥6 of 10 reach staging with **no interventions outside approvals**, **and** packet review is cheaper than PR review | Proceed to Phase 2 with the reduced scope |
| Otherwise | Re-plan: invest in verification and spec quality first, not in the platform |

---

## 6. Proposed changes for the next revision

1. Add a front-page **"Decision requested"** (pilot, people, budget, 90-day milestone, kill criteria) + an **approval budget**.
2. Replace §5.2, §5.4, §7 and §16 with **one gate-resolution table** and a precedence rule (strictest wins).
3. Rewrite §13 with **sandbox and network enforcement**, controller-only push and PR text, a separate holdout repo, CI hardening against agent PRs, and single-use approvals.
4. Change the MVP adapters: **state store + CODEOWNERS PR review + a small approval service**, instead of labels and Environments alone.
5. Replace §12.5 with **task and mission state machines** plus a transition table.
6. Add sections for **Org and ownership (RACI)**, **Risks** (vendor, legal, privacy) and **total cost of ownership**.
7. Recalibrate budgets (M1). Run mutation and holdouts at merge and release points only (M2).
8. Put the **2-week experiment** in front of Phase 2. Cut or defer the items in §3.
9. Fix the facts: SDK naming, Environments limits, METR update, the incident mapping, and an OWASP mapping table.
