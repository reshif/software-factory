# Software Factory Gap Assessment

**Scope.** This assessment compares the Python/uv software factory (`/home/reshif/build/software-factory`, branch `feat/0.3.2-hardening`) against the reference model in [software-factory-reference-model.md](software-factory-reference-model.md).

**Date.** 2026-09-28.

**How it was produced**
- Three auditors worked independently: Claude Opus 5.5, Claude Fable 5.1 and Claude Sonnet 5.
- Each read the code, docs and tests, and ran the suite in its own `/tmp` copy. None of them modified the repository.
- The orchestrator (Opus 5.5) merged their reports. Every headline finding, and every point where the auditors disagreed, was re-checked against the current HEAD, `15f4b87`.

**Snapshot drift.** The auditors worked at `804441e`. While they ran, another session landed the WP2 commits (`8f333ab`, `15f4b87`). Findings that WP2 fixed are marked **Resolved at HEAD**. Nothing below claims more than what was verified.

**Agreement markers**

| Marker | Meaning |
|---|---|
| ●●● | All three auditors agree |
| ●● | Two agree |
| ● | Only one auditor raised it |
| O / F / S | Opus / Fable / Sonnet |
| **Verified @HEAD** | The orchestrator re-checked it against `15f4b87` |

---

## 1. Bottom line

**What it is.**
- The project is a **local-first governance and evidence CLI**, not a cloud software factory. All three auditors agree.
- The user's own coding client (Claude Code, Codex or Copilot) runs the agents. The Python code enforces the mission state machine, the hash-bound evidence, the traceability from request to criteria to evidence, the detection of weakened checks, and protected factory paths.
- The project describes itself this way: "There is no factory server, database, autonomous agent scheduler, or deployment executor" (`docs/architecture.md:7`).

**Where it is strong.** On binding and traceability it is stronger than most real factories in the external research. The code quality is high:
- 993 tests passed at `804441e`; 1,153 pass at HEAD (`15f4b87`), with 427 subtests.
- Lint is clean.
- The build works.

It is also unusually honest. Records carry `trust: local-unattested`, and the enforcement map has a "Not enforced" column.

**The central weakness.** Every human gate the research marks P0 can be recorded by the agent it is meant to constrain. This covers scope approval, independent review, exceptions, and CI and merge evidence.
- The approver is a free-text `reference` field.
- `owners.maintainer` defaults to `null`, which disables the self-review check.
- The orchestrator guard is **opt-in**. Even when it is enabled, it still **allows** the orchestrator to run `mission decision`, `mission review` and `mission ci-result`. (Verified @HEAD)

**The second problem.** The branch's documentation still promises behavior that the code does not deliver. WP2 closed part of this gap but not all of it (see §4).

**Scale of the gap against the rubric.** Roughly 7–9 of the 65 requirements are fully met. Of the 27 P0 items, Fable's tally is:
- 3 implemented
- 3 met by a defensible local equivalent
- 15 partial
- 6 missing

Most of the missing ones are cloud-plane controls the design deliberately leaves out. That is expected for a supervised local tool. The P0 gaps that sit *within* its own scope are not expected, and they are the priority.

---

## 2. Where the auditors disagreed, and the orchestrator's ruling

Sonnet scored more generously than Opus and Fable. Each disagreement was re-checked against HEAD.

| Req | Sonnet | Opus / Fable | Ruling (verified @HEAD) |
|---|---|---|---|
| E5 no self-approval | Implemented | Partial | **Partial.** The check at `workflow.py:2205,2410` exists, but `owners.maintainer` is `None` by default (`installation.py:61`), which disables it. Opus reached READY_PR with a single actor |
| C4 named human approval | Implemented | Partial | **Partial.** The approval is bound to a hash, but the approver identity is free text and the orchestrator can record it |
| E4 risk policy → human | Implemented | Partial | **Partial.** Higher risk only adds more *agent* review kinds (`workflow.py:1244-1251`). Nothing ever requires a human |
| K3 improver cannot edit gates | Implemented | Partial | **Partial.** Protected paths need a maintenance mission, but the agent can declare that mission itself with `--kind` |
| D3 secrets | Implemented (local-equivalent) | Partial | **Partial.** Only `TYPESAFE_API_KEY` is stripped (`checks.py:150`). Every other environment variable, including cloud and provider keys, reaches checks |
| B1–B4 triage | Missing ("no triage stage") | Partial / N/A-eq | **Partial.** `--kind` lanes and blocking on open ambiguities are the local equivalent. S is right that the `triage` command covers failed checks, not work items |
| D5 config injection | N/A | Partial | **Partial.** The PR-checkout case does not apply, but a local branch or pasted change can still add `.mcp.json` and the gate won't notice (see F-4) |
| Top risk | Sandbox and secrets (Critical) | Self-attestable gates, docs vs. code | **Self-attestable gates is the top risk**, because it hollows out every P0 human gate. Sandbox and egress are High |

---

## 3. Consolidated findings, ranked

### Critical

**F-1. Human gates can be self-attested by the orchestrator agent.** ●● O,F (S noted it for F3 exceptions). **Verified @HEAD**
- **Evidence**
  - `record_decision` needs only a non-empty `reference` (`workflow.py:2344-2358`).
  - `owners.maintainer: None` by default (`installation.py:61`).
  - `enforcement.claude_orchestrator_agent: False` by default (`installation.py:70`).
  - A HEAD guard probe *allows* `mission decision --input`, `mission review --input` and `mission ci-result`.
  - Opus's probe reached READY_PR with a single actor.
- **Risks:** R8, R10, R5. **Requirements:** C4, E4, E5, F2, F3.
- **Fix**
  1. Have the guard deny agents `decision` for the kinds `scope`, `exception`, `merge` and `release`, and deny `ci-result`.
  2. Add a human-only `software-factory approve` command. It should require a TTY and typed confirmation, and optionally a Git or SSH signature over `subject_hash`. The gate then accepts only attested approvals for those kinds.
  3. Make an unset `owners.maintainer` a gate error, not a doctor warning.
  4. Turn on the Claude orchestrator enforcement by default.
- **Effort:** M.

### High

**F-2. The docs still promise behavior the code lacks.** ●● O,F. **Verified @HEAD**

WP2 resolved:
- path and option allowlists in the guard
- `AskUserQuestion` and `TodoWrite` in the orchestrator tools
- the claim that Copilot's `factory` agent has no web access

Still open at HEAD:

| Claim | Where it is claimed | What the code does |
|---|---|---|
| `mission brief --kind plan` / `--kind verify` | `skills/factory-plan/SKILL.md:10`, `skills/factory-review/SKILL.md:16`, `prompts/factory-blueprint.md:17`, `docs/architecture.md:19`, roles | `BRIEF_KINDS = ("context","research","code","acceptance","adversarial")` (`workflow.py:140`). **The installed workflow fails at the planning step** (Fable reproduced "invalid choice: 'plan'") |
| Accept-scope moves `base_commit` forward and records `base_history` after a constitution change | `constitution-enforcement.md:86-92`, `upgrading.md:27`, `factory-maintenance.md:17` | Not in any `.py` file (WP3 has not landed). After an upgrade, in-flight missions have **no working reconciliation path** |
| ~~The orchestrator opens with a self-test (Bash `true` must be denied)~~ | `constitution-enforcement.md:47`, `vendor-behavior.md:70` | **Correction:** this was already implemented in WP2 as `GUARD_SELF_TEST` in `rendering.py` ("First run Bash `true`. The guard must deny it…"). The orchestrator's search for "self-test" missed it |

- **Risks:** R2. The enforcement map is the project's trust document.
- **Fix:** before tagging 0.3.2, either land the `plan`/`verify` brief kinds, WP3 and the self-test, or remove the claims.
- **Effort:** M.

**F-3. No isolation, egress control or environment scoping for checks and agents.** ●●● (S rated this Critical). **Verified @HEAD**
- **Evidence**
  - `checks.py:147-153` passes the full environment minus a single key.
  - The Claude reviewer and planner get `WebFetch, WebSearch` (`rendering.py:502`).
  - The Copilot read roles get `web` (`rendering.py:527`).
  - The runbook says "does not provide operating-system process containment".
- **Risks:** R6, R1. **Requirements:** D1–D3, E2, F4.
- **Fix**
  1. Make `checks[].env` an allowlist that denies by default.
  2. Remove web tools from the reviewer; make them opt-in for the planner.
  3. Add an optional `runner` (podman or bwrap with `--network none`).
  4. State the threat model in the README: trusted operator, trusted repo, untrusted model output.
- **Effort:** M–L.

**F-4. Agent-config injection paths are unprotected, and injection is detected only at gate time.** ●● O,F. **Verified @HEAD**
- **Evidence**
  - `PROTECTED_FLOOR` (`workflow.py:74-105`) has only root-level `.claude/**`, `.codex/**` and `.agents/**`.
  - It has **no** `.mcp.json`, nested `**/.claude/**`, `GEMINI.md` or `.cursor/**`.
  - Opus's probe with `.mcp.json` plus `src/.claude/settings.json` (granting `Bash(*)`) passed at risk `low`.
  - Even protected files are caught only after the session has loaded them.
- **Risks:** R1. **Requirement:** D5.
- **Fix**
  1. Extend the floor to cover these paths.
  2. `mission brief` and `mission create` refuse, outside maintenance missions, when governance files differ from `base_commit`.
  3. `doctor` reports dirty governance files as an error.
- **Effort:** S.

**F-5. The risk classifier misses common sensitive code and additive test weakening.** ●● O,F. **Verified @HEAD**
- **Evidence**
  - The default `sensitive_paths` is only `src/auth/**`, `migrations/**` and `tests/fixtures/**` (`policy.json:31-35`).
  - Opus's probe of `src/security/auth.py` gated at risk `low` in the patch lane.
  - Nothing in `workflow.py` detects `skip` or `xfail`; the `@pytest.mark.skip` probe passed at risk `low`.
  - The lane (`--kind`) is chosen by the caller.
- **Risks:** R5, R7. **Requirements:** B2, E4, F3.
- **Fix**
  1. Broaden the defaults: `**/*auth*/**`, `**/security/**`, `**/billing/**`, `**/payment*/**`, IaC, Dockerfiles.
  2. Refuse the patch lane when sensitive paths change.
  3. Detect added skip, xfail and ignore markers in every ecosystem.
  4. Protect test-runner configuration (`conftest.py`, `[tool.pytest]`, coverage config).
- **Effort:** S–M.

### Medium

**F-6. Mission records are never scanned for secrets.** ●●● (S rated this Critical). **Verified @HEAD**
- **Evidence:** `redact` is used only in `semantic`, `triage`, `routing` and `models`. It is not used in `workflow.py` or `checks.py`. Missions are meant to be committed, so a pasted key ends up in Git history.
- **Requirement:** J2. **Fix:** run `redact()` as a detector on every `record-doc`, `clarify`, `review`, `record-result` and `decision` input, and refuse on a match unless `--allow-masked` is given. **Effort:** S.

**F-7. No append-only audit log.** ●●●
- **Evidence:** `mission.json` is overwritten in place; `transition_mission` keeps only `previous_state`.
- **Requirement:** I3. **Fix:** a hash-chained `events.jsonl` recording actor, session, command, from, to and record hashes; the gate verifies the chain; add a `mission history` command. **Effort:** M.

**F-8. No agent budgets, time-based stuck detection, notification or kill switch.** ●●●
- **Evidence:** `limits` covers only repair attempts, timeouts and output caps.
- **Requirements:** D7, G4, I5. **Risk:** R4.
- **Fix:** `limits.stale_hours` and `limits.max_task_minutes`; `status --stale` naming the owner; a notify hook (argv) that runs on BLOCKED; a `.factory/local/HALT` file the CLI honors. **Effort:** S–M.

**F-9. The CI result is caller-supplied and never checked.** ●●●
- **Evidence:** `workflow.py:2733-2738`; the README says so.
- **Requirement:** F2. **Fix:** an optional `gh run view --json` verification; a check in `doctor` for branch protection and the required check name; a loud UNATTESTED banner in gate output. **Effort:** M.

**F-10. No trust tier on intake.** ●●●
- **Evidence:** the request schema has no source or trust field.
- **Requirement:** A2. **Risk:** R1.
- **Fix:** `request.source` and `request.trust`. Untrusted requests are framed in briefs as untrusted data and are refused the patch lane. **Effort:** S.

**F-11. Codex and Copilot exports have instructions only.** ●● O,F
- **Evidence:** Codex has `tool_allowlists: false` and no hook equivalent. A fresh `doctor` reports `layer: instructions`.
- **Fix:** say plainly in the docs that these profiles are unenforced, and make `doctor` louder about it. **Effort:** S.

### Low / Later

- **F-12. No self-improvement loop, scorers, regression benchmark or metrics.** ●●● This is by design. **Do not add an improver until K4 exists:** a `software-factory selftest` that replays 5–10 fixed scenario missions and fails closed per scenario. Metrics could start with a `report` command covering lead time, repair attempts, gate-failure reasons and rework. (K1, K4, L1–L4.) **Effort:** L.
- **F-13. The `triage` name collides with the research's meaning.** ● S. The `triage` command classifies *failed checks*, not work items. Clarify this in the docs.
- **F-14. `owners.reviewer` is unused configuration** (●● O,F). `docs/verification.md` is stale: it still describes 0.2.6 with 303 tests, and the package version is 0.3.1 on a 0.3.2 branch (●● O,F).
- **F-15. UI changes never require visual evidence** (F1), and product review never shows a running artifact (G1). ●●●

---

## 4. Strengths all three auditors confirmed ●●●

- **Candidate binding.**
  - The fingerprint covers content, the governance snapshot, the Git view (index, excludes, attributes, filter and textconv config), check program hashes and log hashes.
  - Any change invalidates evidence and approvals.
  - Adversarial tests back this up (`tests/test_evidence_032.py`).
- **Traceability from request to criterion to evidence to verdict.**
  - The request is stored verbatim with a hash chain.
  - Criteria must quote it exactly.
  - Every criterion needs evidence and a `pass` verdict.
  - A traceability table goes into the PR packet.
- **Detection of weakened checks and tampered tests.** Changed argv, a required check made optional, deleted tests, net-removed lines and removed assertions all raise risk or need an exception.
- **Protected factory controls.**
  - Policy is the union with the *baseline*, so a mission cannot unprotect itself.
  - A change to the constitution blocks every in-flight mission.
  - Stale exports fail the gate.
- **It never merges or deploys.** MERGED needs a real merge commit reachable from trunk that contains the reviewed content.
- **Advisory AI never gates.** JEV runs in shadow mode by default, its outbound text is redacted, and the gate never reads it.
- **Honest labelling.** `trust: local-unattested`, `live_behavior: not_run`, and a "Not enforced" column in the enforcement map.
- **Engineering quality.** 1,153 tests pass at HEAD (993 at the audited snapshot), lint and format are clean, and the build and isolated install work.

---

## 5. Roadmap

### Now (block the 0.3.2 tag)

1. **F-2** Reconcile docs and code: add `mission brief --kind plan|verify` (or fix every skill, role, prompt and doc that uses them); land WP3 (base rebase and `base_history`) or remove its claims; add the orchestrator self-test or remove that claim.
2. **F-1** Guard denies agents the approval-kind `decision` and `ci-result`; `owners.maintainer` is required, as a gate error.
3. **F-4** Extend `PROTECTED_FLOOR` with `**/.mcp.json`, `**/.claude/**`, `**/.codex/**`, `**/.agents/**`, `**/GEMINI.md` and `.cursor/**`.
4. **F-5** Broaden `sensitive_paths`, refuse the patch lane on sensitive changes, and detect skip and xfail markers.
5. **F-6** Scan all record inputs for secrets.
6. **F-14** Refresh `docs/verification.md` and bump the version.

### Next

7. **F-1** Attested human `approve` command. Claude orchestrator enforcement on by default.
8. **F-3** Environment allowlist for checks; no web tools for the reviewer; optional container runner; threat model in the README.
9. **F-7** Hash-chained `events.jsonl` that the gate verifies.
10. **F-8** Stale detection, notify hook and a HALT kill switch.
11. **F-9** Optional `gh`-verified CI and branch-protection checks.
12. **F-10** Trust tier on the request.

### Later

13. **F-12** Replay benchmark (K4) → `report` metrics → only then consider an observer or improver whose proposals are PR-only.
14. **F-15** A convention for UI evidence; preview links for product review.
15. Cost and usage capture, wherever the host clients expose it.

---

## 6. Verification record

| Check | Result |
|---|---|
| All three auditors @`804441e` (their own `/tmp` copies) | `uv sync --locked` OK. `pytest`: **993 passed, 427 subtests** (×3). `ruff check`: clean. `uv build`: wheel and sdist 0.3.1 |
| Orchestrator @`15f4b87` (scratch copy) | `pytest`: **1153 passed, 427 subtests** (249.6 s). `ruff check`: clean |
| Guard probes @HEAD (scratch `init --profile claude`, well-formed PreToolUse payload) | **Deny:** `models discover`, `verify --candidate-root`, `cat` with an absolute or `..` path, `true`. **Allow:** `mission status`, `git status`, **`mission decision`, `mission review`, `mission ci-result`** |
| Gate probes (Opus, @`804441e`) | `.mcp.json` + nested `.claude`, `src/security/auth.py`, and `@pytest.mark.skip` all passed at risk `low`. A single actor reached READY_PR. No code touched by these probes changed in WP2 (verified: `PROTECTED_FLOOR`, `policy.json`, no skip detection) |
| Not verified | Live Claude, Codex and Copilot sessions and hook firing; remote CI and branch protection; macOS and Windows; live paid JEV |
