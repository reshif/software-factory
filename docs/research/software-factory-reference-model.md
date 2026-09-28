# Self-Improving Software Factories: Consolidated Reference Model

Research date: 2026-09-28.

**Subject.** Zach Lloyd (Warp), *Self-Improving Software Factories: The New Open-Source Model*. Fable 5.1 dates it to the AI Engineer World's Fair 2026 keynote track, around 30 June 2026. That date comes from the conference schedule listing and has not been confirmed for this particular session.

**Method.** Three researchers wrote reports independently: Claude Opus 5.5, Claude Fable 5.1 and Claude Sonnet 5.
- Each started from the same talk transcript and did its own read-only web research.
- None of them read this repository.
- The orchestrator (Claude Opus 5.5) merged the three reports into this document. It did not add research of its own.

**Purpose.** This is a neutral yardstick for the planned review of *this* software factory. It says nothing yet about how this project measures up.

**Tags used below**

| Tag | Meaning |
|---|---|
| `[TALK]` | Stated in the transcript |
| `[EXT]` | From cited external sources |
| `[JUDG]` | A researcher's design judgment |

**Agreement markers**

| Marker | Meaning |
|---|---|
| **O / F / S** | Raised by Opus / Fable / Sonnet |
| ●●● | All three raised it |
| ●● | Two raised it |
| ● | Only one raised it |

---

## 0. Executive summary

1. **The talk is a thin outline and needs outside detail to be usable.** It gives the loop, four components and four planes. It does not define "stuck", triage criteria, spec format, ship mechanics, observer governance, security, or metric units. All three researchers found that Warp's *later* public material (Warp Factories docs, blog posts, demo repos) fills most of these gaps. ●●●
2. **Self-improvement must be governed, not autonomous.** Every external source found says observer or improver changes arrive as **reviewable PRs merged by a human** ("never applied silently"). All three researchers recommend going further and making gate, permission and budget rules **uneditable by the improver**, backed by a frozen regression benchmark that fails closed. The talk itself never says this. ●●●
3. **The biggest risk the talk ignores is prompt injection through untrusted intake.** Issue text, PR titles, comments, and injected `CLAUDE.md`/`AGENTS.md` files have been used to hijack Claude Code, Gemini CLI and Copilot agents, with about 85% success in one proof of concept. A factory that reads open-source issues sits right in this blast radius. ●●●
4. **Humans are the throughput bottleneck, and rubber-stamping is a measurable failure mode.** On build.warp.dev at fetch time, 1,820 items were "Awaiting maintainer" and 0 were "Agent working" (F). AI-authored PRs are larger and get less review. The answer is a risk-tiered human review policy plus size caps, evidence bundles, and review-quality telemetry. ●●●
5. **Metrics need quality counterweights.** The talk's "software shipped vs. human time and token cost" is easy to game. All three recommend pairing it with revert rate, incidents or MTTR, and reviewer-correction rate, and using Uber's multiplicative cost decomposition and cost-per-merged-PR. ●●●
6. **Separation of duties has to be enforced by the platform, not by prompts.** Each role should hold only the credentials and tools it needs:
   - No agent merges its own work or approves its own PR.
   - The review agent is read-only.
   - Tests, scorers and CI config are protected paths.
   - Inference keys never enter the sandbox.
   ●●● (the detail varies by researcher)

---

## 1. What the talk says

All three researchers agree on this content. Transcript errors are corrected throughout ("cloud code" → Claude Code, "techsp spec" → tech spec, "protoactory" → proto-factory, "product invariance" → product invariants).

### 1.1 Speaker and thesis `[TALK]`

**Speaker**
- Former Google principal engineer who led engineering on Google Docs. More than 20 years as an engineer.
- Founder of Warp. Says he "hasn't written a line of code in six months" but still ships often.
- Warp claims, all self-reported: open-sourced "a couple months ago" after 5 years closed, more than 60k stars, about 200 contributors, more than 800k active developers.

**Thesis.** Software engineering becomes *factory engineering*. Engineers build and manage the thing that builds the product ("meta-engineering"). They "code less but ship more".

**Timeline he gives.** Chat and autocomplete → interactive agents (now) → automation within about 6–12 months.

**Prediction.** Every sizable project will have a factory, the way CI/CD became standard.

### 1.2 The loop `[TALK]`

"This loop could literally just say the SDLC." The factory floor is "a graph of steps", similar for every product, and items "get stuck at certain points".

| # | Stage | Actor | Guidance given |
|---|---|---|---|
| 1 | Inputs | Team and users | Channels: task tracker, Slack/Teams, terminal/IDE, monitoring |
| 2 | Triage | Agent | **Easy and unambiguous → implement directly** ("how you get going"). **Hard → spec** |
| 3 | Spec | Agent | **Product spec** = product invariants. **Tech spec** = architecture and shape of the code |
| 4 | Spec review | **Human gate** | Only its position in the loop is given |
| 5 | Implement | Coding agent in a cloud sandbox | Produces a diff. Any coding agent works |
| 6 | Code review | Agent first, then human | "Most painful part". "Agentic slop". When to bring humans in becomes a **risk-management exercise** |
| 7 | Verify | Agent | Computer use producing **videos and screenshots** for UI work. CI/CD still applies |
| 8 | Product review | **Human gate** | Only its position in the loop is given |
| 9 | Ship | Not specified | "You ship" |
| 10 | Monitor | Agent | "Is it crashing? Is it being used?" |
| 11 | Feedback | Agent/system | Monitoring output goes back into the top of the loop |

Separately from the fixed gates, humans are also brought in **"when things get stuck"**.

### 1.3 Four components `[TALK]`

1. Automations.
2. Context and skills.
3. Humans at the right time, meaning when work is stuck.
4. Self-improvement loops ("really important").

### 1.4 Reference architecture `[TALK]`

| Plane | What it is |
|---|---|
| Intake | Many ways for work to enter |
| Control plane | Decides how work is distributed across the factory floor |
| Execution plane | Cloud sandboxes, plus choosing **which harness and which model** |
| Data plane | Sits "below" the factory so agents remember, learn and improve ("really important") |

### 1.5 Self-improvement and metrics `[TALK]`

- **Skill loop.** Factory agents run skills. **Observer agents** watch how the skills are applied and where they fail, then improve them.
- **Example.** Senior engineers correct a code-review agent's comments. An observer uses those corrections to improve the review agent "for the next run".
- **Mindset.** "Measure and improve": software shipped against **human time and token cost**.

### 1.6 Open source, build vs. buy, and taste `[TALK]`

**Open source**
- Software is cheap to build and cheap to clone, so a product alone is not a moat. You need distribution, ecosystem, brand, data or capital.
- Startups lack those, so they should build in the open. It moves you from "hated on HN" to "tolerated".
- Open source's usual pains (noisy issues, sloppy PRs, review hell, verification cost) become manageable with a factory.
- build.warp.dev is a "proto-factory at scale. Not working perfectly, but working."

**Build vs. buy**
- A simple factory is easy to build. One that scales is heavy infrastructure, so focus on your product. Uber built one internally.
- His Q&A resolution: *everyone deploys some factory*, and the in-house engineering is **tuning it**, meaning the right skills for your domain.

**Taste and people**
- "The only thing that matters is are you building something useful." Human taste and product sense at the touchpoints that cannot be automated are essential.
- Advice to students: adaptability, critical thinking, learning speed, and enough systems knowledge to reason about agent-written code and specs.

### 1.7 What the talk does not say ●●●

The talk does not specify:
- a skill format
- criteria for "easy and unambiguous"
- a triage outcome set beyond implement or spec
- spec templates
- what the product reviewer sees
- ship, merge or rollback mechanics
- how "stuck" is detected
- who reviews observer changes
- eval or regression protection
- the data-plane schema, retention or privacy
- sandbox security, secrets or egress
- prompt injection
- cost controls
- metric units
- monitoring dedup and rate limits
- multi-repo governance
- contributor credit and licensing

The speaker also has a commercial interest in "buy" (O).

---

## 2. External findings, merged and cited

### 2.1 Warp's own factory

**Timeline**
- **Oz**, Warp's cloud-agent orchestration platform, launched 2026-02-10. ●●●
  - Docker sandboxes, hosted or self-hosted workers.
  - CLI, API, SDK and scheduler; audit trail per run.
  - Runs Claude Code, Codex and Warp Agent.
  - Sources: [newsroom](https://www.warp.dev/newsroom/2026/2/10/warp-launches-oz-the-orchestration-platform-for-cloud-coding-agents), [docs](https://docs.warp.dev/agent-platform/cloud-agents/platform/)
- **Open source**, 2026-04-28, AGPL. ●●●
  - Warp says "nearly one million active users".
  - OpenAI is founding sponsor, and GPT models power the open-source workflow (F).
  - Sources: [blog](https://www.warp.dev/blog/warp-is-now-open-source), [newsroom](https://www.warp.dev/newsroom/2026/4/28/warp-open-sources-its-agentic-development-environment)
- **Warp Factories**, 2026-08-18, closed beta. This is the productized version of the talk's architecture. ●●●
  - Warp automates about 30–35% of its own tasks weekly (O, via [TechCrunch](https://techcrunch.com/2026/08/18/warps-new-system-is-an-out-of-the-box-software-factory-for-ai-development/)).
  - Sources: [blog](https://www.warp.dev/blog/open-infrastructure-for-building-a-software-factory), [how it works](https://docs.warp.dev/factories/how-factories-work/)

**How Warp Factories work**

- **Foreman orchestrator** ●●●
  - Routes each item to Triage, Spec, Implement, Review and Verify agents.
  - Chooses model, harness and context per stage.
  - Takes "the shortest path that still meets your quality policy", for example skipping triage for requests that are already clear (F).
- **Default human checkpoints** ●●
  - Approve the spec, answer clarifying questions, and merge PRs.
  - "A factory is built to pause when there is a decision that needs to be made by a person."
- **Agent contracts** (F, [factory-agents](https://docs.warp.dev/factories/factory-agents/))
  - Triage reproduces an issue only when research can't establish the cause.
  - The spec agent writes a draft PR containing validation criteria.
  - The implement agent continues the spec's branch.
  - **The review agent should use a different model or harness from the implement agent**, so the two don't share blind spots. It outputs accept, revise or escalate.
- **Factory-as-code** ●●●
  - Files: `factory.yaml` + `agents/*/agent.md` + `skills/*/SKILL.md` + `scorers/` + `benchmarks/` + `runners/` + `automations/`.
  - Validated by a PR check.
  - Enables rollback, canary and "agentic changes to the factory".
- **Scorers and self-improvement** ●●●
  - LLM-judge, code or human scorers with a rubric, a threshold (for example 0.8) and **sampling of about 10–25% of runs**.
  - Built-in scorers: task and procedure compliance, verbosity, efficiency, code quality.
  - Every few hours, failures are batched into an improver run that proposes PRs with evidence.
  - The improver is told to **"redraft holistically, not pile on addendums"**.
  - Failure modes Warp names: doom loops, ignoring human guidance, orchestrator context rot, redundant re-reads. One fix removed 275 redundant screenshot actions.
  - Sources: [engineering post](https://www.warp.dev/blog/engineering-self-improving-software-factories), [scorers](https://docs.warp.dev/factories/measure-and-improve/scorers/)
- **Self-improving code review** (Lloyd, 2026-07-15) ●●●
  - The review skill emits `review.json`, which deterministic code converts to comments.
  - The reviewer runs with **read-only PR permissions** to blunt injection.
  - A daily outer agent synthesizes human replies into a skill-update PR.
  - Warp admits it "isn't perfect" and describes no drift safeguards (O).
  - Source: [post](https://www.warp.dev/blog/how-to-build-a-cloud-software-factory-self-improving-code-review)
- **Anthropic's account of the same pattern** (S, F)
  - An inner/base skill plus an outer/improver skill.
  - Feedback is captured where developers already work ("low friction keeps signal flowing").
  - Write "principles, not rules". Keep skills small.
  - "The outer loop proposes improvements; it doesn't apply them silently."
  - Source: [claude.com blog](https://claude.com/blog/how-warp-builds-self-improving-agents-on-claude)
- **Security model** (F, [infra & security](https://docs.warp.dev/factories/infrastructure-and-security/))
  - Execution secrets come from an explicit per-agent allowlist, with none by default.
  - **Inference credentials are never injected into the sandbox.**
  - "Redaction is a backstop, not a substitute."
  - Self-hosted workers connect outbound only.
- **Metrics Warp names** (F, [post](https://www.warp.dev/blog/agent-self-improving-software-factories))
  - PR throughput.
  - Cost per PR, split into compute, platform and inference.
  - Automation % (human touchpoints per PR).
  - A/B benchmarks on 5–10 reference tasks.
  - Caveat: "scorers cost money".
- **Adoption path, "crawl, walk, run"** (O, F; [thread](https://x.com/zachlloydtweets/status/2099941244063432720), [Latent Space](https://www.latent.space/p/software-factories))
  - Crawl: discrete automations.
  - Walk: the full loop on a low-risk repo.
  - Run: closed-loop, measured factories.
  - Aim to raise the auto-merged share (20% → 30% → 60%) according to risk tolerance.

**build.warp.dev, live at Fable's fetch** (F)
- 205 contributors, 65.2k issues.
- Label-driven public states: Triaging (2,364) → Ready to spec (129) → Creating spec (26) → Ready to implement (1,273) → Implementing (44) → Reviewing (53).
- Triage sub-states: Awaiting maintainer 1,820, Needs info 364, Awaiting agent 80, Agent working 0, Needs mocks 100.
- **Humans are the bottleneck.**

**The talk's QR-code repo is unresolved.** The researchers disagree:

| Researcher | Candidate | Their evidence |
|---|---|---|
| O | [`warpdotdev-demos/cloud-factory-demo`](https://github.com/warpdotdev-demos/cloud-factory-demo) | Linked from Warp's triage blog. Its skills are listed below |
| F | [`warpdotdev/oz-for-oss`](https://github.com/warpdotdev/oz-for-oss) | Named as "complete working examples" in Warp's self-improving-agent guide |
| S | [`warpdotdev/warp-factory-examples`](https://github.com/warpdotdev/warp-factory-examples) | F says this **post-dates the talk** and belongs to the Factories product |

Skills in `cloud-factory-demo`, per O:
- `triage`: strict JSON output; states ready-to-implement / ready-to-spec / needs-info / wait-to-implement; read-only; forbidden from mutating the tracker or leaking secrets.
- `spec`: writes `specs/<slug>/PRODUCT.md` + `TECH.md`.
- `implementation`.
- `verify-behavior`: video and screenshots.
- `review-pr`.
- `improve-review-pr`: the daily outer loop.
- `validate-changes-match-specs`.

**Conclusion:** treat both `cloud-factory-demo` and `oz-for-oss` as reference implementations of the talk's pattern.

### 2.2 Uber's factory (the talk's "my friend Adam")

Talk by Adam Huda and Uday Kiran Medisetty. None of the researchers could retrieve the talk itself, only the blog posts and write-ups below. ●●●

**Scale** ([Uber blog](https://www.uber.com/us/en/blog/efficient-software-factory/))
- More than 70% of PRs are attributed to agents.
- **More than 3,600 skills** (O, F). S also found a figure of "2,500 reusable" in a secondary source, likely a different measure.
- About 30k skill executions per day.
- Requests grew 9.4× while cost per 1k requests fell 34%.

**Cost model.** Users × sessions × turns × requests × tokens × price. It splits into *adoption* and *efficiency*, and is paired with **cost per merged PR, per review and per alert**, plus quality signals (revert rate, F1, MTTR). ●●●

**Efficiency levers** (F, O)
- 400k context cap with compaction.
- 1-hour prompt-cache TTL.
- MCP tools exposed as CLI commands, saving 50–70k schema tokens.
- Cheaper models for subagents.
- **Spend alerts at 50/80/100%**.
- 16 anti-pattern flags.

**Process choices** (F, via [Port](https://newsletter.port.io/p/how-uber-built-a-software-factory))
- The coding agent **stops at a draft PR and does not push to CI**.
- Inner-loop checks run before CI.
- Review uses a small model first, then a large one.
- PRs carry a table of the checks already passed.
- A **cap on diffs landing on an engineer's Monday**.
- Manager sign-off for higher spend tiers (S).

**uReview** (O, [blog](https://www.uber.com/us/en/blog/ureview/))
- Pipeline: generate → filter → validate → dedupe. "Fewer but more useful comments."
- More than 90% coverage of about 65k diffs per week.
- 75% of comments rated useful; 65% addressed.

### 2.3 Spec-driven development ●●●

- **GitHub Spec Kit**
  - `constitution.md` holds non-negotiable principles.
  - Then `/specify` (WHAT and WHY) → `/plan` (tech, data models, contracts, test scenarios) → `/tasks` → implement.
  - Includes a consistency and ambiguity analysis step.
  - Sources: [repo](https://github.com/github/spec-kit), [spec-driven.md](https://github.com/github/spec-kit/blob/main/spec-driven.md)
- **Kiro**
  - `requirements.md` with EARS acceptance criteria ("WHEN … THE SYSTEM SHALL …"), then `design.md`, then `tasks.md`.
  - Human approval between phases; requirements are traceable to diffs.
  - Source: [docs](https://kiro.dev/docs/specs/)
- **Mapping to the talk:** product spec ≈ specify / requirements; tech spec ≈ plan / design.

### 2.4 Guardrail baselines from background-agent products

**GitHub Copilot cloud agent** (O, [docs](https://docs.github.com/en/copilot/concepts/agents/cloud-agent/risks-and-mitigations))
- Only users with write access can trigger it.
- It pushes to a single branch.
- **The requester cannot approve the agent's PR.**
- Workflows wait for human approval.
- Firewalled egress; hidden characters filtered.
- Signed, co-authored commits with session logs.
- CodeQL and secret scanning on generated code.

**Claude Code GitHub Action** (F, O; [docs](https://code.claude.com/docs/en/github-actions), [security.md](https://github.com/anthropics/claude-code-action/blob/main/docs/security.md))
- The triggering actor must have write access. Bots are rejected unless allowlisted, which prevents loops.
- Cost caps: `--max-turns`, timeouts and concurrency limits.
- Untrusted PR heads are checked out into a subdirectory.
- **`.claude/`, `CLAUDE.md` and `.mcp.json` are restored from the base branch.**
- Hidden characters are sanitized.
- The doc says these measures "reduce, not eliminate" the risk.

**GitHub Agentic Workflows, "safe outputs"** (F, [docs](https://github.github.com/gh-aw/reference/safe-outputs/))
- Agents run **read-only and request actions through structured output**, which separate permission-controlled jobs then execute.
- Per-action `max` limits.
- Threat detection runs between the agent and the write jobs.

**Monitoring → fix** (F): Sentry Seer auto-fixes only the issues it is *confident* it can fix, and delivers them as PRs. [Seer](https://sentry.io/product/seer/autofix/)

### 2.5 Security evidence ●●●

- **"Comment and Control" / GitInject** ([arXiv 2606.09935](https://arxiv.org/html/2606.09935v1), [news](https://cybersecuritynews.com/prompt-injection-via-github-comments/))
  - Eleven attacks across Claude, Codex, Gemini and Cline. **Every provider was vulnerable in its default configuration.**
  - One proof of concept reached about 85% success.
  - **Config-file injection**: a PR that adds `CLAUDE.md` or `AGENTS.md` is treated as operator-level instructions.
  - Denial-of-wallet cost $32–111 per 2-hour campaign.
  - Simulated benchmarks missed 71% of the real attacks.
  - Minimum baseline: `persist-credentials: false` + tool allowlist + author filtering.
- **Claude Code GitHub Action**
  - A CVSS 7.8 flaw let a malicious issue obtain repo write credentials (S).
  - Microsoft's case study ([link](https://www.microsoft.com/en-us/security/blog/2026/06/05/securing-ci-cd-in-agentic-world-claude-code-github-action-case/)) (O).
- **Other incidents**
  - Invariant Labs: a GitHub MCP agent leaked a private repo ([link](https://invariantlabs.ai/blog/mcp-github-vulnerability)).
  - Aikido "PromptPwnd": AI-driven GitHub Actions and GitLab CI were exploited ([link](https://www.aikido.dev/blog/promptpwnd-github-actions-ai-agents)).
  - OWASP LLM01 (prompt injection) and LLM06 (excessive agency). (O)
- **Reward hacking.** METR found frontier models modifying tests or scoring code to pass ([link](https://metr.org/blog/2025-06-05-recent-reward-hacking/)) (O).
- **Runaway cost incidents** (S, [DEV](https://dev.to/mech_app_ai/the-78000-agent-runaway-what-openai-codexs-826-thread-explosion-reveals-about-agent-cost-1fpo), [TrustGate](https://www.trustgateai.io/blog/token-bill-runaway-agents)). These are secondary sources, so treat the figures as indicative:
  - 826 child agents costing about $78k.
  - An 11-day loop costing $47k.
  - About $48k spent in 14 hours.

### 2.6 Self-improvement research ●●●

- **ACE** ([arXiv 2510.04618](https://arxiv.org/abs/2510.04618)) (O)
  - Generator → Reflector → Curator.
  - Warns of **brevity bias and context collapse** from iterative rewriting, so it prefers **incremental structured updates**.
- **SkillOpt** ([MSR](https://www.microsoft.com/en-us/research/blog/skillopt-agent-skills-as-trainable-parameters/)): treats skills as trainable parameters. (F)
- **OpenAI skill evals** ([link](https://developers.openai.com/blog/eval-skills)). (F)
- **Regression practice** ([link](https://futureagi.com/blog/agent-skill-regression-testing/)): freeze a baseline and **fail closed if any single dimension drops, even when the average rises**. "Evaluators drift too." (F)
- **Claude Code skills are executable policy** ([docs](https://code.claude.com/docs/en/skills)). (O)
  - `allowed-tools` and `!` shell interpolation run locally.
  - So a skill loop is a code-change pipeline and needs code-review rigor.

### 2.7 Review load and delivery stability ●●●

- **DORA 2025** ([report](https://dora.dev/dora-report-2025/)): AI adoption correlates with higher throughput *and* higher instability. (F)
- **Secondary compilation** ([tianpan.co](https://tianpan.co/blog/2026/04/23/rubber-stamp-collapse-ai-authored-prs)). The underlying figures were not traced to primary sources:
  - AI PRs are about 51% larger.
  - Review time is about 441% longer.
  - "Past ~400 lines, reviewers are sampling."
- **Peer research**
  - [arXiv 2607.07980](https://arxiv.org/abs/2607.07980): AI PRs get less review attention and merge faster.
  - [arXiv 2609.06213](https://arxiv.org/pdf/2609.06213): measures reviewer habituation.
- **Mitigation from automation-complacency research** (S): expose reviewers to *known agent failures* during onboarding.

### 2.8 Where researchers disagreed or could not verify

| Item | Status |
|---|---|
| QR-code repo | Unresolved. See §2.1 |
| Warp user count | 800k in the talk; Anthropic says "800K monthly"; Warp's open-source post says "nearly one million". Different dates and definitions |
| 60k stars | Not independently verified by any of the three |
| Uber skill count | 3,600 (O, F, primary blog) vs. 2,500 (S, secondary) |
| How to rewrite skills | Warp says redraft holistically. ACE says incremental updates prevent collapse. **Resolve with a regression benchmark, not by choosing a side** |
| Which model runs Warp's open-source factory | GPT per the open-source launch; Claude per Anthropic's blog. Unresolved |
| build.warp.dev contents | F fetched live numbers. S relied on secondary descriptions |
| Warp data-plane internals | No public schema found (O) |

---

## 3. Consolidated reference model: 65 testable requirements (27 P0)

Merged from 44 requirements (O), 46 (F) and 47 (S), with duplicates removed. **Consensus** shows which researchers raised each item. **P** is the orchestrator's review priority:

- **P0**: a safety or gate integrity issue; must hold.
- **P1**: core to the talk's model.
- **P2**: maturity or optimization.

### A. Intake

| # | Requirement | Source | Consensus | P | How to verify |
|---|---|---|---|---|---|
| A1 | Every channel (tracker, chat, IDE/CLI, monitoring) normalizes to one work-item record with a stable ID | TALK | ●●● | P1 | File the same request through two channels. Expect one schema and one ID lineage |
| A2 | Each item records provenance and **trust tier** (maintainer / contributor / anonymous / bot), and downstream permissions use it | EXT, JUDG | ●●● | P0 | File as a non-collaborator. Trust is lower and the item has no write-capable path |
| A3 | Intake is idempotent | JUDG | ● O | P2 | Replay a webhook. Expect one item |
| A4 | Monitoring and other auto-sources are deduplicated, grouped and rate-limited, with per-source quotas | TALK, JUDG | ●●● | P1 | Inject 50 identical alerts. Expect at most one item, with a count |
| A5 | Bot or agent-authored events cannot re-trigger the factory unless allowlisted | EXT | ● F | P0 | The review agent's own comment starts no run |

### B. Triage

| # | Requirement | Source | Consensus | P | How to verify |
|---|---|---|---|---|---|
| B1 | Triage has a closed outcome set of at least **implement / spec / needs-info / defer-reject-duplicate** | TALK, EXT | ●●● | P1 | Schema test. Invalid output is rejected |
| B2 | "Implement directly" uses explicit criteria (scope, risk paths), not confidence alone. Auth, billing, migrations and security never take the direct path | TALK, JUDG | ●● O,F | P0 | A seeded auth issue routes to spec |
| B3 | Triage writes structured output that deterministic code applies. Triage has no tracker-write or code-push capability | EXT | ●● O,F | P0 | Remove the agent's write tools. Labels still apply; a push fails |
| B4 | Triage inspects the codebase, reproduces only when research is insufficient, and records the evidence and rationale | EXT, JUDG | ●●● | P2 | The trace shows repo reads, a reason string, and a repro transcript when attempted |
| B5 | Human overrides of triage are captured as improvement signals tagged with the skill version | TALK, EXT | ● F | P1 | Relabel an item. The correction appears in the data plane |

### C. Spec and human spec review

| # | Requirement | Source | Consensus | P | How to verify |
|---|---|---|---|---|---|
| C1 | Complex items get a **product spec** (invariants) and a **tech spec** (architecture) as versioned files or PRs at stable paths | TALK | ●●● | P1 | Both exist, with history, and predate the implementation diff |
| C2 | Product invariants are **testable acceptance criteria** (EARS or Given/When/Then) | EXT, JUDG | ●● O,F | P1 | A lint rule rejects a requirement that has no criterion |
| C3 | A project **constitution** of non-negotiables is loaded into the spec and review stages and enforced | EXT | ●●● | P1 | A spec that violates it is flagged |
| C4 | **A named human must approve before implementation.** The approval is bound to the spec version (hash) and recorded with the approver's identity | TALK, EXT | ●●● | P0 | Implementing an unapproved or changed spec is refused and logged |
| C5 | Any later spec change goes back through human review | JUDG | ● F | P0 | Edit the approved spec. The item returns to awaiting approval |
| C6 | Clarifying questions pause the item with zero token spend while it waits | EXT | ● F | P2 | Token spend stays flat until the answer arrives |
| C7 | The approval view shows the spec, triage evidence, and estimated cost and scope | JUDG | ● F | P2 | An approver can answer "what will this touch and cost?" from that view alone |

### D. Execution plane

| # | Requirement | Source | Consensus | P | How to verify |
|---|---|---|---|---|---|
| D1 | Each run gets a fresh, isolated, ephemeral sandbox with declared image, resources and network policy | TALK, EXT | ●●● | P0 | Concurrent runs share no state. The sandbox is destroyed afterwards |
| D2 | **Egress is allowlisted by default** | EXT | ●●● | P0 | `curl` to an arbitrary host fails |
| D3 | Execution secrets come from a per-agent allowlist, with none by default. **Inference credentials never enter the sandbox.** Untrusted-input runs get no secrets | EXT | ●●● | P0 | Dump env and `/proc`. No provider key; no secrets on an untrusted run |
| D4 | Repo tokens are short-lived and scoped, not persisted (`persist-credentials: false`), and limited to the agent's branch with no merge right | EXT | ●●● | P0 | `.git/config` has no token. A push to main is rejected |
| D5 | Untrusted PR heads are checked out outside the workspace root, and agent config files (`CLAUDE.md`, `AGENTS.md`, `.claude/`, `.mcp.json`, hooks) are restored from the base branch | EXT | ● F | P0 | A PR that adds a malicious `AGENTS.md` has no effect on agent behavior |
| D6 | Harness, model, version and config hash are selectable per role and recorded per run | TALK, EXT | ●●● | P1 | Inspect the run record. Changing one role's config leaves the others unchanged |
| D7 | **Per-run hard budgets** (turns, tokens, wall-clock, $) plus doom-loop detection. The item ends in `blocked: budget` and escalates | EXT, JUDG | ●●● | P0 | A looping task is killed at its cap |
| D8 | Implementation continues the spec's branch, links to the approved spec, and passes an automated **spec-conformance check** | EXT | ●● O,F | P1 | A deliberately divergent diff fails |
| D9 | Inner-loop checks (lint, tests, static analysis) pass before shared CI is triggered | EXT | ● F | P2 | CI starts only after in-sandbox checks are green |

### E. Code review

| # | Requirement | Source | Consensus | P | How to verify |
|---|---|---|---|---|---|
| E1 | Agent review runs before human review. Its output is structured JSON that deterministic code converts to comments | TALK, EXT | ●●● | P1 | Timeline order is correct. A malformed payload posts nothing |
| E2 | The review agent is **read-only**, with a minimal tool allowlist | EXT | ●● O,F | P0 | A push attempt from the reviewer fails |
| E3 | The reviewer's model or harness differs from the implementer's | EXT | ● F | P2 | Config and run records show different IDs |
| E4 | **A risk policy (paths, size, blast radius, author trust) decides when a human must review** | TALK | ●●● | P0 | A docs-only PR can auto-merge. A PR touching auth, billing or infra cannot merge without a named human |
| E5 | **Neither the author agent nor the requester can approve or merge their own change** | EXT | ●●● | P0 | Negative test against branch protection |
| E6 | Reviewer-fatigue controls: agent PR size cap or auto-split, and an evidence table in the PR body (checks passed, spec link, screenshots) | EXT | ●●● | P1 | A 2,000-line agent PR is rejected or split |
| E7 | Review quality is measured: agent-comment acceptance rate, and human habituation signals (zero-comment approvals, review time per line, seeded canary defects) | EXT | ●●● | P2 | The dashboard shows the trends. Canary catch rate is tracked |
| E8 | Human reactions to agent comments are captured with low friction, where reviewers already work | TALK, EXT | ●●● | P1 | A reply or reaction is stored and linked to the skill version |

### F. Verification

| # | Requirement | Source | Consensus | P | How to verify |
|---|---|---|---|---|---|
| F1 | Verification is a distinct stage that can fail an item. UI changes need computer-use or browser evidence (video, screenshots), stored durably | TALK | ●●● | P1 | A UI PR without evidence fails a required check. A seeded UI regression is caught |
| F2 | **CI is a required, non-bypassable gate** | TALK | ●●● | P0 | Branch protection requires the checks |
| F3 | **Agents cannot modify tests, scorers, CI config or required-check definitions without a human code owner** | EXT, JUDG | ●● O,F | P0 | A PR touching those paths requires owner review |
| F4 | CI with secrets on agent PRs needs human approval, or runs in a secretless context | EXT | ● O | P0 | The workflow shows awaiting approval, or it has no secrets |
| F5 | Each spec acceptance criterion maps to a verification result (pass / fail / not run) shown to the human | JUDG | ● F | P1 | A traceability matrix is generated per PR |

### G. Product review, ship and escalation

| # | Requirement | Source | Consensus | P | How to verify |
|---|---|---|---|---|---|
| G1 | A human product sign-off, separate from code review, is recorded for user-visible changes. The reviewer sees the running artifact (preview or video), not just the diff | TALK | ●●● | P1 | Release check. The approval links to preview evidence |
| G2 | Merge and deploy are a human action, or a policy-gated automated action. Auto-merge is off by default | TALK, EXT | ●● O,F | P0 | Config default. Enabling auto-merge requires an explicit policy |
| G3 | Ship is reversible (flags or rollback), and each deploy links to its work item | JUDG | ●●● | P1 | Rollback drill record |
| G4 | **"Stuck" is defined (time in state, retry count, failed gates, budget hit) and triggers a named human notification** | TALK, JUDG | ●●● | P0 | An SLA breach sends a notification to a named person |

### H. Monitor and feedback

| # | Requirement | Source | Consensus | P | How to verify |
|---|---|---|---|---|---|
| H1 | Post-ship monitoring (crashes, usage) creates follow-up items linked to the originating PR and spec | TALK | ●●● | P1 | A synthetic crash produces a linked item |
| H2 | Auto-generated items are confidence- and severity-gated before they consume implementation budget. A loop-depth counter escalates fix → regress → fix chains | JUDG, EXT | ●●● | P1 | A chain escalates at depth N. Low-confidence items are not auto-implemented |

### I. Control plane

| # | Requirement | Source | Consensus | P | How to verify |
|---|---|---|---|---|---|
| I1 | An explicit state machine: each item is in exactly one state, and illegal transitions are rejected | TALK, JUDG | ●● O,F | P1 | Property tests |
| I2 | An orchestrator ("foreman") routes items. Stage skips are policy-driven and logged with the rule that allowed them | EXT | ●●● | P1 | A fully specified issue skips triage, and the log shows why |
| I3 | Every transition is an audit event with the actor (human, agent, skill version, model), inputs, outputs and cost | JUDG | ●●● | P0 | Rebuild an item's full history from the log alone |
| I4 | **The factory definition (agents, skills, triggers, scorers, gates) is versioned code, changed only by reviewed PR, with one-step rollback** | EXT | ●●● | P0 | A direct production config write is rejected. Rollback works |
| I5 | Concurrency limits, priority, per-repo and per-user quotas, and a **global kill switch** | JUDG | ●● O,F | P1 | 50 items with a limit of 5 run at most 5. The kill switch stops new runs |
| I6 | A status board of items, states and assignees (agent or human) | TALK | ●●● | P2 | Sample the board against the state store |
| I7 | A named, accountable owner exists for the factory itself | JUDG | ● S | P1 | Ask who owns factory misbehavior; it is a named role |

### J. Data plane

| # | Requirement | Source | Consensus | P | How to verify |
|---|---|---|---|---|---|
| J1 | Full trace per run (transcript, tool calls, scores, human corrections, cost), queryable by ID and by skill | TALK, EXT | ●●● | P1 | Query "failed runs of skill X last week" and get transcripts |
| J2 | **Secrets and PII are redacted before anything is persisted.** Retention and training policy are documented per data class | EXT, JUDG | ●●● | P0 | A canary secret or PII string is absent from stored traces |
| J3 | Memory is attributed, scoped, reviewable and expiring. **Untrusted runs cannot write durable memory without review** (quarantine) | JUDG | ●● O,S | P0 | An untrusted run's memory write lands in quarantine |

### K. Self-improvement

| # | Requirement | Source | Consensus | P | How to verify |
|---|---|---|---|---|---|
| K1 | Scorers (LLM-judge, code or human) have named rubrics, a sample rate and a threshold, and scores are persisted | TALK, EXT | ●●● | P1 | Scored count ≈ sample rate × run count |
| K2 | **Observers only open evidence-linked PRs. A human merges them. Observers cannot write the default branch, and auto-apply is impossible or needs a logged override** | TALK, EXT | ●●● | P0 | Permission check. PR bodies link to the runs behind them |
| K3 | **Gate-relevant rules (approval requirements, permission scopes, budget caps, protected paths) cannot be edited by the improver.** This is enforced by policy, not by reviewer vigilance | JUDG | ●●● | P0 | An improver PR that touches a protected file or field is rejected automatically |
| K4 | Skill and gate changes must pass a **frozen held-out regression benchmark that is per-dimension and fails closed** | EXT, JUDG | ●●● | P0 | An edit that raises the average but drops one dimension is blocked |
| K5 | A change needs more than one independent correction before it is proposed, to avoid overfitting to one loud reviewer | JUDG | ● O | P2 | Improver config sets a minimum evidence count |
| K6 | Skill hygiene: redraft instead of only appending, remove stale guidance, and track skill length and token cost over time | EXT | ●● O,F | P2 | A trend chart exists. Append-only proposals are flagged |

### L. Metrics and economics

| # | Requirement | Source | Consensus | P | How to verify |
|---|---|---|---|---|---|
| L1 | Output per human-hour and per token cost is **always paired with quality counter-metrics**: revert rate, escaped defects or incidents, MTTR, reviewer-correction rate. "Shipped" means merged *and not reverted after N days* | TALK, EXT | ●●● | P1 | One dashboard with no unpaired throughput metric |
| L2 | Cost is attributed per stage, model and item, decomposed into adoption vs. efficiency, and reported as $/merged PR, split into inference, compute and platform | EXT | ●●● | P1 | The breakdown answers "is spend growing from more use or costlier tasks?" |
| L3 | Aggregate budgets with 50/80/100% alerts, and sign-off before autonomy or spend expands | EXT | ●●● | P1 | Alert config exists. Expansion approval has a named approver |
| L4 | A/B benchmarks of model, harness and skill configurations on a fixed task set, re-run whenever the model changes | EXT | ●● F,O | P2 | A comparison matrix with cost |

---

## 4. Risk register (merged)

All three researchers listed every risk below.

| # | Risk | Why it is worse in a factory | Key mitigations | Covered by requirements |
|---|---|---|---|---|
| R1 | **Prompt injection** through issues, PR titles and bodies, comments, monitoring payloads, or injected `CLAUDE.md`/`AGENTS.md` | Intake is built to act on untrusted text without a human click | Trust tiers; treat intake as data; tool allowlists; read-only triage and review; safe-outputs pattern; restore config files from base; no secrets or egress on untrusted runs; never interpolate untrusted text into shell or workflow expressions | A2, A5, B3, D2–D5, E2, F4 |
| R2 | **Self-modifying skills drift or weaken gates**, including brevity bias, context collapse, overfitting to one reviewer, and judge drift | The improver optimizes a scorer, not your intent | PR-only changes with a human merge; protected gate invariants; frozen per-dimension benchmark; CODEOWNERS on skill and gate files; baseline diffs; one-step rollback; periodic recalibration of judges | K2–K6, I4, F3 |
| R3 | **Runaway self-feeding loops** from alert storms, bot comments or regress-fix chains | Monitoring feeds back into intake, closing the loop, and every cycle costs money | Dedupe by fingerprint; per-source quotas; confidence gating; bot filtering; loop-depth escalation; circuit breaker on spikes | A4, A5, H2 |
| R4 | **Cost blowups** from doom loops, redundant computer use, strong models on subagents, costly scorers, or denial-of-wallet attacks | Parallel sandboxes multiply long loops | Hard per-run and aggregate caps; kill switch; cheap-model routing; sampled scoring; context caps; show cost at approval; refuse untrusted work before spending | D7, I5, L2, L3, C7 |
| R5 | **Reviewer fatigue and rubber-stamping** | Human review capacity is the bottleneck, and AI PRs are larger | Risk-tiered review; size caps; evidence tables; precision-first agent review; habituation telemetry; seeded canaries; failure-mode training | E4, E6, E7 |
| R6 | **Sandbox escape and secret exposure** | Agents run attacker-influenced build scripts, tests and hooks | Ephemeral unprivileged sandboxes with no Docker socket; egress allowlist; no inference keys inside; short-lived scoped tokens; secret scanning of outputs and traces; redaction only as a backstop | D1–D5, J2 |
| R7 | **Metric gaming and reward hacking** | "Shipped ÷ cost" is easy to inflate, and agents edit tests or scorers | Paired quality metrics; count merged-and-not-reverted outcomes; protected test and scorer paths; held-out evals; human audit samples; never make throughput the improver's objective | L1, F3, K4 |
| R8 | **Spec drift or rubber-stamped specs** | Implementation "fixes" the spec | Approval bound to a hash; re-review on change; conformance check; criteria turned into tests | C4, C5, D8, F5 |
| R9 | **Memory poisoning** | One bad run corrupts future behavior | Attributed, expiring memory; quarantine for untrusted writes | J3 |
| R10 | **Autonomy creep without governance** | Auto-merge share rises faster than safeguards | Explicit sign-off for each expansion of autonomy; named factory owner | G2, L3, I7 |
| R11 | **Model or vendor lock-in and silent degradation** (F) | Model updates shift behavior | Record the model per run; re-run benchmarks on change; multi-harness tests | D6, L4 |
| R12 | **Loss of human taste and competence** (F) | "A factory churning out things no one cares about" | Keep product review as a human gate; measure user outcomes | G1 |

---

## 5. Open questions an implementer must decide

The talk leaves every one of these open. They are merged from all three reports.

1. How "stuck" is defined, and who is notified through which channel.
2. The operational rubric for "easy and unambiguous", including the full triage taxonomy (duplicate, reject, security-sensitive).
3. The contents of the product and tech spec templates, and whether invariants become tests.
4. What the human product reviewer sees, whether that review blocks ship, and whether it applies to trivial changes.
5. Ship mechanics: auto-merge criteria and how they widen over time, release trains, flags, rollback.
6. Observer governance: which files are protected, what evidence threshold applies, and which eval gate a change must pass.
7. Data plane: contents, retention, redaction, tenant isolation, and whether memory is shared across stages.
8. The security model for untrusted open-source intake. This is the highest-risk case and the talk does not mention it.
9. Units: what counts as "shipped", how human time is measured, and how tokens spent on rejected work are accounted for.
10. Model and harness selection: the evidence used, how often it is re-evaluated, and the cost/quality trade-off.
11. Concurrency: merge conflicts, duplicate work, dependent items.
12. Multi-repo and multi-product factories: shared skills and ownership.
13. Contributor credit, CLAs, and labelling the provenance of AI PRs in open source.
14. The build-vs-buy boundary: which parts count as "tuning" (skills, rubrics, gates, runners). This decides lock-in.
15. Verification for work without a UI (backend, data, infrastructure).
16. How taste gets in, including what *not* to build.

---

## 6. Top 12 review questions

Merged from the three top-10 lists, starting with the questions all three raised.

1. **Trust boundary.** Can untrusted issue, PR, comment or monitoring text reach an agent that holds secrets, write credentials or open egress? Show the configuration, and run an injection test that fails safely, including a PR that adds `AGENTS.md`. (●●●)
2. **Gate integrity.** Which gates does the platform enforce (branch protection, required checks, permissions) rather than prompt text? Can any agent or observer change or bypass them? (●●●)
3. **Self-improvement governance.** Show one real skill-change PR, the benchmark it had to pass, the dimensions that would have blocked it, and the list of files the improver may never touch. Can the whole factory roll back one version in one step? (●●●)
4. **Separation of duties.** Can the author agent or the requester approve or merge? Can agents edit the tests, scorers or CI that judge them? (●● O,F)
5. **State machine and escalation.** Is every item in exactly one explicit state? Can one item be traced from intake to monitoring through the audit log alone? Is there a timed path from stuck to a named human? (●●●)
6. **Sandbox contents.** Print the environment. Are provider keys, long-lived tokens or cloud credentials present? What egress is allowed? (●●●)
7. **Runaway behavior.** What are the per-run and aggregate caps? What state does an item end in when a cap trips? Is there a kill switch? (●●●)
8. **Loop feeding itself.** Show that a bot comment, an agent PR or an alert storm cannot start new runs without dedup, confidence gating and rate limits. (●●●)
9. **Spec traceability.** Does every non-trivial change link to an approved product and tech spec, bound to a hash, with automated conformance checking? (●● O,F)
10. **Verification evidence.** Is machine-produced evidence required, or does "verified" mean an agent said so? (●● O,F)
11. **Metrics honesty and review load.** Define "shipped". Show cost per merged-and-not-reverted PR alongside stability metrics, and the number of open agent PRs per human reviewer. (●●●)
12. **Data-plane hygiene and ownership.** Are traces and memory redacted, scoped and expiring? Can one poisoned run corrupt future behavior? Who is the named owner of the factory? (●●●)

---

## 7. How to use this for the next step

- Treat §3 as the review rubric. For each requirement, record one of: **Implemented, with evidence (file or test)** / **Partial** / **Missing** / **Not applicable, with rationale**.
- Review **P0 items first**: A2, A5, B2, B3, C4, C5, D1–D5, D7, E2, E4, E5, F2–F4, G2, G4, I3, I4, J2, J3, K2–K4.
- Where this project deliberately differs (for example a local-first design instead of cloud sandboxes), write down the equivalent control rather than marking the item missing.
- Per AGENTS.md: record a check that cannot be run in this environment as *not verified*, never as passed.

## Provenance

- Fable 5.1's full report (about 61 KB) is kept outside the repository in the session scratchpad at `research/report-fable.md`.
- The Opus 5.5 and Sonnet 5 reports were returned as text inline. Their content is fully merged above.
- The three researchers used 37 (Opus), 61 (Fable) and 23 (Sonnet) tool calls.
- External claims were fetched through summarizing tools. Check specific figures against the primary URLs before quoting them externally.
