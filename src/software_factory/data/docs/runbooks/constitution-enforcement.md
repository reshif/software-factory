# Constitution enforcement map

The [constitution](../../CONSTITUTION.md) (2.0.0) states obligations; this runbook records what, if anything, enforces each one in this release. Cite rules by title: numbers change between versions.

Enforcement layers, strongest first:

- **Gate/CLI:** the state tool refuses a command, or `software-factory gate` (and every transition to READY_PR) fails with a reason. These are deterministic local checks over editable, unattested records: they catch mistakes and shortcuts, not a determined forger (see Honest records).
- **Claude guard (opt-in):** with `enforcement.claude_orchestrator_agent` and `claude --agent factory-orchestrator`, the PreToolUse hook `.factory/hooks/orchestrator_guard.py` denies file-editing tools, non-specialist agents, setup/admin CLI commands and shell commands outside a read-only allowlist. Off by default; skipped in untrusted workspaces and `-p` sessions; live client behaviour not run by this build.
- **Tool restriction:** exported agent tool lists. Claude and Copilot specialists get no agent tool; Claude and Copilot read roles (planner, reviewer) get no edit or shell tool; Codex read roles run with `sandbox_mode = "read-only"`; Copilot's `factory` agent has no edit tool but keeps `execute`.
- **Instruction/review only:** the rule is stated in the constitution, roles, skills and generated briefs, and an independent review may catch a breach. Nothing mechanical stops it.

"Partial" below means some cases are checked and others are not; the gap column names what is not.

## Never

| # | Rule | Enforced by | Concrete check | Not enforced |
| --- | --- | --- | --- | --- |
| 1 | Invented acceptance or approvals | Gate/CLI (partial) | `mission decision` requires a nonempty `reference`; scope decisions must match the current spec.md hash, exclusion decisions the current request chain head, sensitive-path/check-change `exception` decisions the candidate fingerprint; accept-scope and the gate refuse without them. Open ambiguities block accept-scope. | Who wrote the reference. Records authenticate no one; an agent can type a false reference. Instruction/review only. |
| 2 | Weakened tests, checks or criteria | Gate/CLI (partial) | Gate: a baseline-required or task-named check removed, its argv/cwd changed, or required→optional needs an `exception` decision bound to the fingerprint. Risk tier high (adding acceptance + adversarial review) for deleted test files, net-removed test lines, removed assertion lines, changed check scripts, protected paths. Criteria are hashed at accept-scope; any change resets scope; excerpts must match the verbatim request. | A weakened assertion that adds lines, an edited test outside configured `test_paths`, or a criterion reworded before first acceptance. Reviewers are asked to look for test weakening. |
| 3 | Overstated outcomes | Gate/CLI (partial) | A check passes only with status `pass`, exit 0, no signal and untruncated output; timed-out, failed, interrupted or missing required checks fail `verify`, task DONE and the gate; `e2e`/`manual` routes need their own evidence; READY_PR requires a passing gate. | What an agent says in chat or a handoff, and "not run" labelling of live behaviour. Instruction only. |
| 4 | Overwriting others' work | CLI (partial); Claude guard | `init`/`upgrade` stop on conflicting local edits and preserve deliberate changes; `uninstall` removes only unchanged owned content; the gate rejects changed paths outside task `owned_paths`. The guard denies destructive commands for the orchestrator. | Destructive Git or external operations by a specialist or an unguarded orchestrator. Instruction only. |
| 5 | Secrets in records or prompts | Partial | Raw check logs stay in `.factory/local/` (must be ignored by a shared `.gitignore`, never tracked); evidence stores only their paths and hashes. JEV/semantic prompts and triage output pass through `redaction.redact` (best-effort denylist). | Secrets typed into requests, context, spec, results, reviews, handoffs or packets are not scanned. Instruction/review only. |
| 6 | Product work changing factory controls | Gate/CLI | A non-maintenance mission whose candidate touches a protected path (constitution, instruction files, `factory.json`, `.factory/` controls, `.claude/`, `.codex/`, `.agents/`, `.github/`, `.vscode/`, plus policy `protected_paths`) fails the gate; any protected change raises risk to high; stale generated exports fail the gate (`render --check`); a changed constitution hash blocks every mission until reconciled. | Independence of the maintenance review (see Independent review). |

## Authority and intent

| # | Rule | Enforced by | Concrete check | Not enforced |
| --- | --- | --- | --- | --- |
| 7 | Authority | Claude guard; host | The guard refuses `init`, `upgrade`, `uninstall`, `recover`, `render`, `auth` and `--root` for the orchestrator. Runtime permissions come from the host client. | Everything else; the factory grants and checks no permission. |
| 8 | The request is the contract | Gate/CLI | `mission create` requires `--request-file` (verbatim, hashed); clarifications extend a hash chain; every criterion quotes an exact request or clarification excerpt; exclusions need a decision bound to the chain head; accept-scope binds criteria hash and chain; the gate requires every AC mapped to a task, evidenced and judged `pass`. | Whether the stored request is really the user's words; legacy (pre-0.3.0) missions carry no request. |
| 9 | Context first | Gate/CLI (partial) | accept-scope refuses an absent, template-only or heading-only `context.md`, and any `open` ambiguity. | Whether the context is accurate, and whether questions were asked together up front. |
| 10 | Escalate instead of guessing | CLI (partial) | Starting a task past its repair budget moves task and mission to BLOCKED; PAUSED/BLOCKED/CANCELED need `--reason`; resume needs `--resolution` and refuses still-exhausted tasks; high-risk changes demand adversarial review. | Stopping for conflicts or unauthorized actions and asking one question. Instruction only. |

## Evidence

| # | Rule | Enforced by | Concrete check | Not enforced |
| --- | --- | --- | --- | --- |
| 11 | Claims are testimony | Gate/CLI | Task DONE and the gate need a result bound to the current fingerprint and attempt that lists the current `verify` `checks.json`; `check:<id>` citations must name checks that passed in that run; route `check` criteria need one of their own checks; `evidence:<path>` must be an existing file outside `.git/`, `.factory/local/` and mission records. | `note:` evidence and review prose are still testimony; the orchestrator must inspect them. |
| 12 | Binding | Gate/CLI | Evidence, results and reviews carry the candidate fingerprint (content plus governing inputs); evidence also binds spec hash, HEAD, base, check argv/cwd/required and log hashes; reviews bind `brief_hash` (required for acceptance); decisions bind spec, request chain or fingerprint; missions bind constitution, spec and criteria hashes; `mission status` shows `live_gate` from READY_PR on. | Records edited by hand after the fact (see Honest records). |
| 13 | Advice is not verification | Gate/CLI (by construction) | The gate reads only verify evidence, results, reviews and decisions; semantic/JEV output is marked `advisory_only` and never consulted; model plans only bind which assignment ran. | Someone recording advice as a review or `note:`. Review only. |
| 14 | Honest records | Gate/CLI (partial) | Outputs carry `trust: local-unattested`; READY_PR is a local gate; MERGED needs a successful `ci-result` for the committed candidate from a work branch plus an existing merge commit on the recorded trunk containing it; delivery states need delivery enabled, references, a release decision for the artifact digest and a healthy observation. | CI URLs, conclusions, decisions, reviews and observations are caller-supplied and unchecked; branch protection and remote CI are the real controls. |

## Roles and review

| # | Rule | Enforced by | Concrete check | Not enforced |
| --- | --- | --- | --- | --- |
| 15 | The orchestrator never produces | Claude guard (opt-in); Copilot tool restriction | Guard denies Edit/Write/MultiEdit/NotebookEdit, Skill, unknown tools, output redirection and non-read-only shell. Copilot `factory` agent has no edit tool. | Codex (instructions only), Claude without the opt-in agent, Copilot shell writes, and "stop if specialists cannot be spawned". |
| 16 | Bounded specialists | Tool restriction; gate (partial) | Claude and Copilot specialists have no agent tool (Copilot `agents: []`); read roles have no edit tool; briefs say "Do not start nested agents" and "Do not edit mission records". Gate rejects changed paths outside `owned_paths`, result files outside the task's paths, and unrecognized files under `.factory/missions/`. | Codex nesting; implementer/verifier edits to mission records through their edit or shell tools; working from anything other than the brief. |
| 17 | One writer | CLI | Only one task may be RUNNING/VERIFYING per workspace, across all missions; accept-scope refuses while a task is active; mission writes take `.factory/local/state.lock`. | One orchestrator per mission, and separate workspaces for parallel writers. Instruction only. |
| 18 | Independent review | Gate/CLI (partial) | Required kinds by lane and risk (code; + acceptance for feature lane; + adversarial at high risk); reviews only in REVIEWING/READY_PR; `author` required and must differ from `owners.maintainer`; every AC needs a `pass` verdict; blocking findings need ids and a later resolution with a reason; current `brief_hash`. | A separate context, reading code before the report, and honest reporting of missing independence: the author field is self-reported. Instruction only. |

## Craft

| # | Rule | Enforced by | Concrete check | Not enforced |
| --- | --- | --- | --- | --- |
| 19 | Deliberate repair | CLI (partial) | Attempts count on every RUNNING; a failed verify after RUNNING sets `repair_required`; the budget (`limits.repair_attempts`) blocks; transitions follow `workflow.json`; CANCELED writes a handoff packet; READY_PR needs authored `recovery.md` and `## Risks`. | Diagnosing before retrying, and an up-to-date `handoff.md`. Instruction only. |
| 20 | Proportionate, complete change | Gate/CLI (partial) | Diff size over `limits.high_risk_lines` raises risk and review depth; every AC needs evidence and verdicts; changed paths must be owned. | Minimality and model/effort choice. Review only. |

## Amending the constitution

1. Open a maintenance mission (`mission create --kind maintenance`); only that kind may change protected paths. Record the user's authorization as its scope decision.
2. Classify the change: **MAJOR** removes or redefines an obligation (including renumbering that changes what a cited number means), **MINOR** adds one, **PATCH** changes wording only. Update `Version` and `Last amended`.
3. Record the impact in the mission (spec or plan via `mission record-doc`): changed rules, affected roles, skills, prompts, templates and exports, this enforcement table, and how in-flight missions will be reconciled.
4. Update this table and every document that cites a changed rule by title; run `software-factory render` so exports carry the new constitution hash.
5. Obtain an independent review (protected paths make the mission high risk: code, acceptance and adversarial). The maintenance mission reconciles its own constitution hash as below before READY_PR.

## Reconciling in-flight missions

The constitution change itself is a factory-control change (*Never* rule on factory controls). It lands through a maintenance mission, or on the trunk outside any product mission's diff, for example a `software-factory upgrade` committed on its own. If `.factory/CONSTITUTION.md` changes inside a product mission's candidate, the gate reports "Protected factory path requires a maintenance mission: .factory/CONSTITUTION.md"; that is intended, and reconciliation does not clear it. Reconciliation only rebinds a mission to a constitution that has already landed.

Any pre-merge mission accepted under a different constitution fails its transitions and the gate ("Constitution changed …"), and those errors print the exact commands below. `upgrade` never blocks on this: its report lists each such mission under `missions_needing_constitution_reconcile` as `{id, state, from_version, to_version}`, with a `constitution_reconcile_note`. For each mission, of any kind:

1. Block it: `software-factory mission block --mission ID --reason "Constitution changed; reconciling" --next "Reconcile and re-accept scope"` (active tasks become BLOCKED).
2. Ask the user whether the mission continues under the new constitution, and record only the answer they actually gave, as an `exception` decision bound to the exact SHA-256 of the new `.factory/CONSTITUTION.md`:

   ```sh
   uv run --locked --project .factory software-factory mission decision --mission ID --input - <<'EOF'
   {"id": "D-CONST-<first 8 hex of the hash>", "kind": "exception", "subject_hash": "<exact new constitution sha256>", "reference": "<who approved the constitution change, and where>"}
   EOF
   ```

   A decision for any other hash is rejected. If the user does not want to continue, cancel the mission with a reason instead.
3. Run `software-factory mission accept-scope --mission ID`. The scope decision must still match spec.md; if the spec changed, record the user's new acceptance first. The mission returns to PLANNED with tasks TODO; attempts and history are kept, and the mission records the new `constitution_version`.
4. Implement as needed, then a fresh `verify` run, new results and fresh reviews of the required kinds before the gate. Earlier evidence does not carry over.

Missions already MERGED or later keep the evidence they were judged on; do not relabel them.
