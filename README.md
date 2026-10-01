# Software Factory

A Python package that installs a supervised software factory into an existing repository or a new folder. It includes planning, task execution records, checks, independent review, native client instructions, model planning, JEV model selection when enabled, and optional claim/source assessments.

Python 3.11+ and [uv](https://docs.astral.sh/uv/) are required. Git is required for initialization and mission evidence. The factory itself does not require Node; your product's checks may use any language or toolchain.

## Install and use

Install the local release:

```sh
uv tool install dist/software_factory-<version>-py3-none-any.whl
software-factory init /path/to/project --profile codex --dry-run
software-factory init /path/to/project --profile codex
cd /path/to/project
software-factory doctor
```

Choose `claude`, `codex`, `copilot`, or a comma-separated combination. Inspect existing changes before choosing `--allow-dirty`. The target must be the repository root. For a plain folder, `--git-init` explicitly creates Git metadata without a commit.

The installer keeps its runtime, locked dependencies, canonical instructions and documentation inside `.factory/`. It adds managed sections to shared instruction files and exports the selected client's skills and agents. Existing product source, README, package manifests and CI remain under your control. Conflicting owned files stop the operation before it applies changes.

For another developer or a fresh clone, the checked-in project runtime is sufficient:

```sh
uv sync --locked --no-dev --project .factory
uv run --locked --project .factory software-factory doctor
```

`auth`, `init`, `upgrade`, `uninstall`, `recover`, `version` and `--help` run in the global CLI you invoke. `doctor` and `inspect` fall back to it when the project runtime is missing or the project is not installed or is uninstalled. All other commands run in the project's pinned runtime. `--skip-sync` installs files only; finish runtime setup before using the factory.

## Configure the product

You do not edit setup by hand: the agent proposes the project's checks, setup commands and ignore rules, shows you the exact change, and you approve it with one line (`approve S-0001 setup` in Claude Code chat, or `software-factory setup apply --proposal S-0001` in your terminal); the factory writes the files, re-renders, commits exactly those files and moves the open mission onto that commit. To do it yourself instead, replace the deliberately failing `configure-me` check with your real test command: `software-factory checks --add tests -- uv run pytest` writes it into `factory.json`, re-renders and prints the commit line (or edit `factory.json` yourself). Do this before the first mission, also on an empty repository: `factory.json` is a protected path and cannot change once a mission is past PROPOSED. A mission is not created while factory setup has uncommitted changes or stale exports, and `software-factory mission rebase --mission ID` moves a PROPOSED mission past a setup commit you make afterwards, keeping its request and answers. For example, a Python product might use:

```json
"checks": [
  {"id": "tests", "command": ["uv", "run", "--locked", "pytest"], "cwd": ".", "required": true, "timeout_seconds": 600}
]
```

After any `factory.json` edit (checks, enforcement, limits), run `software-factory render`: `factory.lock.json` pins the `factory.json` hash, and `doctor` and the gate report stale exports until it is refreshed. `software-factory inspect` suggests commands without running them. Set `owners.maintainer` (required: the gate fails without it, because it is what blocks self-review) and `owners.reviewer`, review the generated configuration, and establish a normal Git baseline before creating missions. Checks use argv arrays, without shell expansion. They inherit your environment minus `TYPESAFE_API_KEY` unless you set `"check_env": {"mode": "allowlist"}` or give a check its own `"env"` list of variable names; see [factory setup](src/software_factory/data/docs/runbooks/factory-setup.md).

Open the selected native client and start with `factory-blueprint` for planning or `factory-build` for implementation. Claude Code and Copilot use `/factory-build`; native Codex uses `$factory-build`. The factory prepares instructions and deterministic workflow tools; your authenticated coding client runs the agent work.

Every new mission starts from the user's verbatim request: `software-factory mission create --id ID --title TITLE --kind feature --request-file PATH` copies it byte for byte into the mission record (`--request-file` is required for new missions). Before any specification, the factory maps the affected code, interviews you in rounds (grounded questions with suggested defaults) and records an assessment of what it understood, the blockers, its concerns about the request (with evidence and a recommended alternative), the risks and its assumptions; you approve scope only after seeing it. Clarifications, acceptance criteria quoting the request and a plan with an architecture diagram are recorded through the same CLI before scope is accepted; `patch` missions follow a smaller lane.

Independent tasks run at the same time in lanes: the orchestrator opens a Git worktree per ready task (`mission lane-open`), briefs one implementer per lane, and integrates each finished lane (`mission lane-integrate`, owned paths only) before verifying it; `mission lanes` shows the waves and the critical path. `software-factory serve` opens the Mission Deck, a read-only view of this repository's missions (and those in its other worktrees) at `http://127.0.0.1:8765/?token=...`: galaxy, orbit and floor 3D views, lanes, the request → criteria trace, the assessment, documents, the event log and, on request, the live gate. It serves only GET requests on 127.0.0.1 with a per-session token and refreshes as records change; approvals still happen only in your chat or terminal. With `"ci": {"verify": "gh"}` in `factory.json`, `mission ci-result` success is checked against the GitHub Actions run through your `gh` CLI (completed, successful, for exactly the candidate commit). A request that comes from a contributor or an unknown author is created with `--source contributor|anonymous`: it takes the feature lane, briefs present it as untrusted data, and its risk tier is high. `software-factory mission halt --reason TEXT` is a kill switch: nothing new starts until you run `mission unhalt` in your terminal. Missions with no change for `limits.stale_hours` (default 24) are flagged stale by `mission list` and `doctor`, and an optional `notify.command` (argv, no shell) runs when a mission becomes BLOCKED or the factory is halted. `/factory-onboard` drafts it from the repository and an interview, `/factory-retro` turns a finished mission into lessons, and `software-factory crew import --from ~/crew` brings over an existing Crew profile and project notes. Project knowledge (purpose, users, what must not break, rules and recipes) lives in `.factory/crew/` and changes only with your consent: agents propose the exact text with `software-factory crew propose`, and you save it by replying `approve P-0001 crew` in Claude Code chat or with `software-factory crew apply --proposal P-0001` in your terminal; each save is recorded in a hash-chained ledger (see `.factory/docs/runbooks/project-knowledge.md`). For every kind of mission but patch, options come before the plan: the planner writes at least two genuinely different approaches (`options.md`), a reviewer in a separate context scores them against every criterion (`grading.md`), and scope approval waits until that grading is current; the plan builds the winner unless you choose otherwise. After a mission reaches READY_PR, the factory offers a retro when something is worth learning (repairs, blocking findings, course changes, a new kind of work): lessons drawn only from the mission's records, each backed by evidence, which you approve one by one into project rules or a reusable recipe. Every change to a mission is appended to a hash-chained `events.jsonl` that `mission history` shows and the gate checks, so an edit made outside the CLI is visible. Several missions run in parallel when each has its own worktree: `software-factory mission create --worktree --id M-2 --title ... --request-file PATH` creates `<repo>.missions/M-2` on branch `factory/M-2`; open a client there for that mission, and `mission list --all` shows them all. Tasks of different missions may not touch the same files at the same time. The complete sequence is: intake and model checkpoint → specification → task plan → implementation → recorded task results → configured checks → independent review → local readiness gate. Mission states move PROPOSED → PLANNED (`mission accept-scope`) → IMPLEMENTING → VERIFYING → REVIEWING → READY_PR through explicit `mission transition` and `mission task-transition` commands (listed in the orchestrator role), and `software-factory mission template --kind task|result|review|decision|criteria` prints a minimal valid JSON skeleton for each record. Resume, repair budgets, handoffs, CI and delivery records are included. Your own approvals are recorded by you, never by the agent. In Claude Code, reply in chat with a line `approve M-0001 scope` (or `exception`, `merge`, `release`): a UserPromptSubmit hook the factory adds to `.claude/settings.json` records it from your message before the model sees it, bound to what you approved. Anywhere, `software-factory mission approve --mission ID --kind scope|exception|merge|release --reference TEXT` does the same in an interactive terminal after you type the mission ID. `mission decision` refuses those kinds, and the Claude orchestrator guard denies `mission approve` and `mission ci-result`. Set `"approvals": {"chat": false}` in `factory.json` to require the terminal. `READY_PR` means local evidence is consistent (local-unattested); it does not create a PR, merge or deploy. Evidence JSON, decisions, reviews and `mission ci-result` URLs/conclusions are caller-supplied and unauthenticated, and a CI URL is not checked. Authoritative assurance needs branch protection and remote CI that re-runs `software-factory checks --require-clean` (or the product checks) itself.

Installed details live in `.factory/docs/runbooks/factory-setup.md`, `prompts.md`, `runtime-contract.md`, and `resume-and-switch.md`. Use `software-factory --help` and each subcommand's `--help` for CLI arguments.

## Constitution

Every factory session follows `.factory/CONSTITUTION.md` (2.0.0), copied into the managed AGENTS.md section. It ranks below the host's instruction hierarchy, organization controls and the user's current authorization, and above roles, skills and briefs; it grants no permission. The [enforcement map](src/software_factory/data/docs/runbooks/constitution-enforcement.md) (installed as `.factory/docs/runbooks/constitution-enforcement.md`) maps each rule to the gate check, guard or tool restriction that enforces it, or marks it instruction-only. Commit an upgrade that changes the constitution on its own, outside any product mission's diff; `upgrade` then lists pre-merge missions under `missions_needing_constitution_reconcile` without blocking. Reconcile each with an `exception` decision for the new constitution hash, which you record yourself with `mission approve`, and `mission accept-scope`, then re-verify and re-review.

## Enforcement per client

Factory roles are instructions first. Claude Code enforcement is on by default; `factory.json` holds the flag (setting it to false and running `software-factory render` turns it off):

```json
"enforcement": {"claude_orchestrator_agent": true}
```

| Client | What applies |
| --- | --- |
| Claude Code | `claude_orchestrator_agent` (default true) adds a PreToolUse guard to `.claude/settings.json` for every main-session tool call (subagent calls pass) and exports `.claude/agents/factory-orchestrator.md`. Start that agent with `claude --agent factory-orchestrator` in a trusted workspace: it may only spawn the four factory specialists (the `Agent(...)` allowlist applies only to `--agent` sessions) and has Read, Glob, Grep and Bash. The guard runs `.factory/hooks/orchestrator_guard.py` with the project runtime (falling back to `python3`, 3.11+), which denies Edit/Write/MultiEdit/NotebookEdit, spawning anything other than the four factory specialists, unknown tools and any shell command outside a read-only allowlist (software-factory CLI with stdin from `< file` or a heredoc, read-only git, ls/cat/head/tail/wc/grep/rg/find). Setup and admin commands (`init`, `upgrade`, `uninstall`, `recover`, `render`, `auth`) and `--root` are denied too: a human runs setup. Launch or guard errors deny; a hook timeout does not block. Project hooks are skipped in untrusted folders and `-p` sessions. |
| Copilot | The `factory` orchestrator agent has no `edit` tool set; it keeps `execute`, so avoiding shell writes relies on instructions. No hooks are generated: Copilot's `preToolUse` input carries no agent identity and repository hooks (which VS Code also loads) apply to every agent, so an orchestrator guard would also block implementers. |
| Codex | Instructions only; the factory generates no Codex hooks. |

`software-factory doctor` reports the layer in effect per selected profile under `enforcement`. These are local guardrails: mission records remain local-unattested, and live client behaviour has not been exercised by this build.

## JEV is included

The Python adapter, canonical skill, input schema, rubric and operational guide ship together. New installations start with `jev.enabled: false` and `jev.claim_mode: "shadow"`. Installation and status do not call the provider.

```sh
software-factory semantic status
software-factory semantic example
software-factory semantic check --input .factory/local/claims.json --no-network
```

Run `software-factory auth login` once in your terminal to save the TypeSafe API key with hidden input, then use `software-factory auth status` to inspect availability without making a provider request. All JEV commands automatically use the user credential store; agent commands need no key flags or special launcher. `TYPESAFE_API_KEY` remains an explicit override for CI. Existing projects must upgrade their pinned `.factory` runtime to 0.2.8 or later. See [authentication](src/software_factory/data/docs/runbooks/authentication.md) for storage, rotation, logout and permissions. Then configure `jev` in `factory.json` and regenerate instructions with `software-factory render`.

| Setting | Model selector |
| --- | --- |
| `jev.enabled: true` | JEV chooses among eligible models. Abstention, or a choice below `jev.min_confidence` (default 0.6), leaves that assignment unresolved in a written plan for a human to decide; missing credentials, provider errors or timeouts exit 2 with no plan written. Never falls back to factory-models. |
| `jev.enabled: false` | factory-models chooses using its task assessment and preference policy. |

Both paths use `software-factory models plan`. The plan records the selector and decision; validation works offline. See `.factory/docs/jev-routing.md` for the selection contract. When `jev.enabled` is true, the task objective and any research `strengths` text are sent verbatim to TypeSafe with the eligible model descriptions (keep secrets and private paths out of them); repository source is not scanned. JEV-enabled planning handles the number of candidates documented in `jev-routing.md`. Native client controls execute the selected coding model. Requests use TypeSafe's HTTP API directly, with at most two retries on rate-limit, overload and server errors inside the configured deadline. Each evaluated claim is labelled `accepted` or, below `jev.claim_accept_confidence` (default 0.8, taken from TypeSafe's citation-check guidance rather than calibrated on your material), `needs_review` for a human to confirm. Both labels are advisory and never gate; shadow mode withholds them.

Two further advisory JEV helpers are available, both off unless `jev.enabled` is true and both shadow-first under `jev.claim_mode`:

- `software-factory semantic verify-claims --mission ID --input .factory/local/claims-verify.json` checks an implementer's key claims against the repository diff since the mission base. Cited paths with no changes yield `no_evidence` without a request; excerpts are secret-masked (including home-directory paths) and bounded. See `.factory/docs/runbooks/semantic-assistance.md`.
- `software-factory triage --mission ID --revision REV` suggests why failed checks in registered evidence failed (flaky, environment, test defect, product defect, configuration). It sends only redacted, hash-verified log tails and leaves anything below `jev.triage_accept_confidence` (default 0.6) unclassified for a human. See `.factory/docs/runbooks/check-triage.md`.

Neither helper changes mission state, repair budgets, evidence or the gate; treat their output as prompts to inspect, never as approval.

TypeSafe's official agent skill (`typesafe-ai/skills`) helps a coding agent write code that calls TypeSafe. It is optional and only useful when your product itself uses TypeSafe; the factory does not need or install it.

`jev.claim_mode` independently controls the claim/source helper. It sends only explicitly supplied eligible claims/excerpts, with bounded requests, deadlines and private caching. Its shadow/advisory and ordinary-fallback behavior is documented in `.factory/docs/runbooks/semantic-assistance.md`. JEV judgments never replace checks or independent review.

Provider behavior is covered by mocked tests. Paid inference, account availability and quality calibration have not been established by this build.

## Upgrade and remove

```sh
software-factory upgrade /path/to/project --dry-run
software-factory upgrade /path/to/project --allow-dirty
software-factory uninstall --root /path/to/project --dry-run
software-factory recover --root /path/to/project
```

Install the desired release into the uv tool environment before upgrading; `upgrade` refuses a downgrade unless `--allow-downgrade` is given. Changed historical schemas or conflicting customizations require deliberate reconciliation. Interrupted file operations keep a private journal that `doctor` reports; until recovery only `auth`, `doctor`, `inspect`, `status`, `recover`, `version` and `--help` run, and other commands are refused. Inspect `recover`, then use `recover --apply` after the prior process has stopped. Deleting a generated export relinquishes it: `init` and `upgrade` leave it absent and record it as relinquished in `factory.lock.json` (`doctor` only reports it and never writes); `software-factory render` recreates deleted exports. A relinquished file that exists is yours and is never overwritten; `doctor` reports a relinquished entry-point skill (factory-build, factory-blueprint, factory-resume, factory-status) as the error `entry_skill_replaced` when other content occupies it, or the warning `entry_skill_missing` when absent. To restore it, delete the file and run `software-factory render`. Editing a generated export blocks `render`, `upgrade` and `init` until you restore it (`git checkout -- PATH`) or delete it; uninstall gives up ownership of edited exports.

Uninstall removes only unchanged owned content. It keeps edited exports and edited factory sections. For an edited section in AGENTS.md, CLAUDE.md or .github/copilot-instructions.md it removes only the two factory marker lines, leaving your text as ordinary content; a later `init` adds a fresh managed section. A `.codex/config.toml` key you changed becomes yours; if it conflicts with what the factory needs, `init` refuses and names the key to change or remove. It retains product configuration, mission history, private records, ignores and provenance. See `.factory/docs/runbooks/upgrading.md` for details.

## Develop and build

```sh
uv sync --locked
uv run pytest -q
uv run ruff check .
uv build --no-sources
```

`src/software_factory/data/` contains the bundled factory assets, including JEV. `docs/implementation.md` records the scope and `docs/release-process.md` the release checks. The original Node checkout is preserved.

This is a private local release. No registry publication or redistribution license is implied. Current execution evidence covers Linux/Python 3.11 and 3.14; Python 3.11 is the declared minimum. macOS, Windows and interactive Claude/Codex/Copilot discovery need their own validation before claiming operational compatibility. Codex metadata discovery currently depends on POSIX pipe polling. Generated-file tests cover all seven profile combinations.
