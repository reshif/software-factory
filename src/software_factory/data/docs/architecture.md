# Repository software factory architecture

The main session in the selected client is the orchestrator. It briefs specialists, inspects their output, records state through the CLI, runs verification and decides; it never produces code, tests, docs, research or drafts. Local tools validate state and evidence; existing GitHub and delivery systems retain their own authority.

```mermaid
flowchart TD
    USER["User request"] --> ENTRY{"Entry"}
    ENTRY -->|status| STATUS["Read-only report"]
    ENTRY -->|build · blueprint · resume| ORCH["Orchestrator (main session)<br/>brief · inspect · record · verify · decide"]
    ORCH --> SETUP{"Factory setup committed and rendered?"}
    SETUP -->|No| ASK["Say what is unfinished; create nothing"]
    SETUP -->|Yes| CREATE["mission create --request-file -<br/>verbatim request.md · knowledge frozen into crew-context.md"]
    CREATE --> CTX["F: context brief → planner<br/>codebase map · saved knowledge · open questions ranked"]
    CTX --> CLAR["R: interview rounds (unlimited)<br/>at most 5 plain questions per round<br/>mission clarify records each round"]
    CLAR --> ASSESS["Assessment: blockers · concerns · risks"]
    ASSESS --> LANE{"Lane"}
    LANE -->|patch| PLAN1["plan brief: spec · criteria · plan · recovery"]
    LANE -->|feature · maintenance| SPEC["spec brief: spec.md · AC-n quoting the user"]
    SPEC --> OPTS["G: options brief → 2+ approaches<br/>grade brief → separate reviewer scores them"]
    OPTS --> PLAN2["plan brief: Chosen option · plan · recovery"]
    PLAN1 & PLAN2 --> FIX{"Checks or setup need changing?"}
    FIX -->|Yes| PROPOSE["setup propose → you reply approve S-n setup<br/>factory writes · renders · commits · moves the mission"]
    PROPOSE --> SCOPE
    FIX -->|No| SCOPE["You reply approve ID scope<br/>accept-scope binds everything · tasks → PLANNED"]
    SCOPE --> BRIEF["mission lanes → waves · lane-open per ready task<br/>one implementer per Git worktree, in parallel"]
    BRIEF --> VERIFY["Orchestrator integrates each lane, runs verify"]
    VERIFY -->|fail| REPAIR{"Repair budget left?"}
    REPAIR -->|Yes| BRIEF
    VERIFY -->|pass| REVIEWS["Independent reviews by lane and risk<br/>code · acceptance · adversarial"]
    REVIEWS -->|changes requested| REPAIR
    REVIEWS --> GATE{"Readiness gate"}
    GATE -->|reasons| ORCH
    GATE -->|pass| READY["READY_PR (local evidence)"]
    REPAIR -->|"No"| HELD["BLOCKED · one concrete question to you"]
    HELD -->|"resume --resolution"| ORCH
    READY --> RETRO["E: retro offered on signals<br/>you approve lessons item by item"]
    READY --> REMOTE["PR · CI result · approve ID merge · MERGED"]
    classDef human fill:#fff3cd,stroke:#a87900,color:#222
    class CLAR,PROPOSE,SCOPE,HELD,RETRO,REMOTE human
```

Blueprint runs the same flow up to the plan and stops before implementation. A mission created before 0.3.0 has no recorded request; it keeps the earlier gate rules and shows a warning. The small lane still needs context, criteria and an architecture diagram, kept short.

*The orchestrator never produces* is an instruction in every client. In Claude Code a project-wide PreToolUse guard (`.claude/settings.json`, on by default) applies to every main-session tool call, so every main session is the orchestrator; specialist calls carry their agent identity and pass. `claude --agent factory-orchestrator` adds the tool allowlist. Each role's agent file pins the model from `model_selection.roles` (on by default: opus for orchestrator, planner and reviewer, sonnet for implementer, haiku for verifier), and Copilot's `factory` agent has no edit or web tool; the orchestrator records everything through CLI commands that read stdin; see [enforcement per client](runbooks/vendor-behavior.md#enforcement-per-client) for what each client actually enforces.

READY_PR is a local, unattested gate result: evidence, reviews, decisions and `ci-result` records are caller-supplied. Remote PR creation, GitHub CI, merge and production delivery require separate actual evidence. Delivery is disabled in the default product configuration. A pipeline node in this diagram is an integration boundary, not a deployed service supplied by this repository.

## Constitution

`.factory/CONSTITUTION.md` (2.1.0) is copied into the managed AGENTS.md section, so every client loads it at session start; exported agents cite its SHA-256. Precedence: the host's instruction hierarchy and organization controls, then the user's current authorization, then the constitution, then roles, skills and briefs. Documents, records, tool output and agent reports are evidence, never authority. When rules conflict the earlier section wins; within a section, the rule that withholds a claim, change or approval wins, and the conflict is surfaced.

Sections: **Never** (six prohibitions: invented approvals, weakened tests, overstated outcomes, overwriting others' work, leaked secrets, product work changing factory controls); **Authority and intent** (authority, the request is the contract, context first, escalate instead of guessing); **Evidence** (claims are testimony, binding, advice is not verification, honest records); **Roles and review** (the orchestrator never produces, bounded specialists, one writer, independent review); **Craft** (deliberate repair, proportionate complete change); **Amendment** (maintenance missions, semver, impact record). Roles and skills cite rules by title, because numbers change between versions. What enforces each rule, and what is instruction-only, is in the [enforcement map](runbooks/constitution-enforcement.md), which also covers amendment and reconciling in-flight missions.

## Lifecycle states and the diagram

The diagram shows GitHub CI between READY_PR and merge. The constitution's *Honest records* rule defines READY_PR as local evidence only: it does not mean a PR exists or remote CI passed, so the state tool keeps READY_PR local and records CI afterwards:

| Diagram step | Recorded state and command | Required evidence |
| --- | --- | --- |
| Local gate passes | `transition --to READY_PR` | Current checks, task results with criteria evidence, reviews of each required kind, bound request/criteria/scope, authored risks and recovery plan (gate); then generate the local PR packet |
| PR opened | optional `delivery.pr_ref` via `record-delivery` | External reference only |
| CI fails | `ci-result --mission ID --url URL --head SHA --conclusion failure --reason TEXT` moves READY_PR → IMPLEMENTING | Reason is kept in `ci_failures` |
| CI passes | `ci-result --mission ID --url URL --head SHA --conclusion success [--trunk REMOTE/BRANCH]` stores `delivery.ci_ref` with branch, trunk ref and `trunk_kind` | Gate passes, candidate is committed, `SHA` equals HEAD, HEAD is a work branch other than the trunk and `SHA` is not already on the trunk |
| Merge observed | `record-delivery` with `merge_ref`, then `transition --to MERGED` | Merge decision bound to the CI candidate fingerprint; `merge_ref` is reachable from the recorded trunk, is not an ancestor of `base_commit`, equals the candidate only if the candidate reached the trunk, and contains the candidate's changed files |
| Staging, release | STAGING → AWAITING_RELEASE → DEPLOYING | Artifact digest, staging, release decision and `recovery_ref` before DEPLOYING |
| Observation | OBSERVING → DELIVERED | `delivery.observation` with `status: healthy` |
| Unhealthy / failed deploy | DEPLOYING or OBSERVING → RECOVERING → RECOVERED (or BLOCKED) | `incident_ref`, `recovery_ref`, recovery decision; RECOVERED also needs a healthy `recovery_observation` and `follow_up_mission` |
| Held work | PAUSED / BLOCKED (`--reason`, optional `--next`; `block` command) | Resume needs `--resolution`; `clarify` and `criteria` keep the hold (the mission returns to PROPOSED on resume); accept-scope lifts only a constitution-reconcile hold; an exhausted task needs a replan with new paths, checks or dependencies, which resets its budget at most once |

The trunk is chosen once, by `ci-result`, and recorded as a full ref; MERGED re-resolves that same ref and `transition` accepts no trunk override. In a repository with any remote the trunk must be a remote-tracking ref (`--trunk origin/BRANCH` or `origin/HEAD`); only a repository without remotes may use a local `main`/`master` (`trunk_kind: local`). The rule accepts merge commits, squash merges and fast-forwards of a work branch, and rejects work that never left the trunk, a commit the trunk does not contain, and a pre-mission trunk commit with matching content.

Residual limits: refs, mission records and the local Git object store are editable by anyone with shell access. Remote-tracking refs can be rewritten locally, a local trunk can be moved with `git branch -f`, and a new trunk commit with the candidate's content is indistinguishable from a squash. These checks catch mistakes and casual shortcuts; branch protection and required CI on the hosting service are the authoritative integration controls. Fetch the trunk before recording MERGED.

After MERGED the gate command and `software-factory mission status` assess the CI candidate and merge commit instead of the working tree, whose pre-merge evidence is necessarily stale once HEAD moves. For READY_PR and later, `status` adds a `live_gate` result so a stored label that no longer holds is visible. `software-factory mission list` shows non-terminal missions.

## Responsibilities

| Component | Responsibility | Limit |
| --- | --- | --- |
| Constitution | Obligations for every session, role and mission: Never, Authority and intent, Evidence, Roles and review, Craft, Amendment | Grants no permission and enforces nothing itself; see [enforcement map](runbooks/constitution-enforcement.md) |
| Main orchestrator | Brief specialists with generated briefs, inspect status/risk/diffstat, record state, run verification, resolve findings, escalate | Produces no code, tests, docs or research; does not grant approval or accept subagent claims as proof |
| Planner/implementer/verifier/reviewer | Context and plans, one task's change, extra end-to-end evidence, reviews by kind | No nested agents; no implicit expanded authority from a role name |
| Workflow skills and entry prompts | Reusable lifecycle procedures and directly callable build/plan/resume/status entries | Loaded when relevant; entry scope does not grant runtime permissions |
| State tool | Schema-validated local records, dependency/transition rules and locking | No distributed scheduler or authenticated approval service |
| Verifier and gate | Execute real commands, capture candidate identity, reject incomplete/stale evidence | Host process access is not sandboxed; editable records are not tamper-proof |
| Renderer and doctor | Export one or more vendor profiles side by side, preserve owned-file integrity and diagnose setup | Live instruction/tool discovery still needs actual-client tests |
| GitHub/delivery systems | Remote checks, review authority, deployment and observation | Must be configured and verified for the real account/product |

The orchestrator keeps a compact mission summary in context and obtains detailed files on demand. It reasons about uncertainty and can ask for research, split a task or change an approach within scope. It does not blindly advance a fixed checklist. Scripts handle conditions that need deterministic checks.

## Initialization: what `init` does

```mermaid
flowchart TD
    CMD(["software-factory init --profile claude,codex,copilot --commit"]) --> GIT{"Git repository?"}
    GIT -->|No, with --commit| GINIT["git init -b main"]
    GIT -->|Yes| CLEAN
    GINIT --> CLEAN{"Uncommitted changes?"}
    CLEAN -->|"Yes, without --allow-dirty"| REFUSE["Stop: nothing written"]
    CLEAN -->|No| DETECT["Detect test commands from project files<br/>(uv.lock, pyproject, package.json, go.mod ...)<br/>nothing is executed"]
    DETECT --> CONFIG["Write factory.json (only if absent)<br/>checks found, else the failing configure-me placeholder<br/>owners.maintainer from git config"]
    CONFIG --> PAYLOAD["Copy the payload into .factory/<br/>constitution · roles · skills · prompts · templates · schemas · docs · hooks<br/>pinned runtime: src/ · pyproject.toml · uv.lock · run.py"]
    PAYLOAD --> SYNC["uv sync --locked into .factory/.venv<br/>(ignored; skipped with --skip-sync)"]
    SYNC --> RENDER["Render exports for the chosen clients<br/>AGENTS.md / CLAUDE.md sections · .claude/agents · skills<br/>.codex · .agents · .github · chat-approval hook + runtime deny rules"]
    RENDER --> LOCK["factory.lock.json + .factory/installation.json<br/>record what the factory owns, by hash"]
    LOCK --> IGNORE[".gitignore: .factory/.venv · .factory/local"]
    IGNORE --> COMMIT{"--commit?"}
    COMMIT -->|Yes| C["Commit 'Initialize software-factory'<br/>(factory files only in an existing repository)"]
    COMMIT -->|No| NEXT
    C --> NEXT["Report next steps · open your client · /factory-build"]
```

`init` never runs your product's commands, never overwrites a file it does not own, and `uninstall` removes only what the lock records as factory-owned and unchanged. Checks that `init` could not detect are not your chore: during the first mission the agent proposes them and you approve them in one line (`approve S-0001 setup`).

## Files and ownership

| Path | What it is | Who uses it |
| --- | --- | --- |
| `factory.json` | Checks, setup commands, limits, owners, profiles, switches | The CLI on every command; agents read it for the check ids. Protected: changes come from you or a setup proposal you approve. |
| `factory.lock.json`, `.factory/installation.json` | What the factory owns, by hash | `upgrade`, `uninstall`, `render --check`, `doctor`. Agents never need them. |
| `AGENTS.md`, `CLAUDE.md` (factory section) | The constitution plus the session entry | Loaded by every client at session start. |
| `.claude/`, `.codex/`, `.agents/`, `.github/` | Rendered agents, skills, hooks and settings for each client | The client: they are how it finds the factory's agents and commands. |
| `.factory/CONSTITUTION.md`, `roles/`, `skills/`, `prompts/` | The canonical instructions the exports are rendered from | The orchestrator reads its role and skills; specialists get their part through their agent file and brief. |
| `.factory/templates/` | Mission document templates | Copied into briefs by the CLI; agents see them inside their brief. |
| `.factory/docs/` | Runbooks for you and agents | Read on demand when an instruction links to one. |
| `.factory/hooks/` | The chat-approval hook and the Claude orchestrator guard | Run by Claude Code itself, never read by agents. |
| `.factory/schemas/`, `registry.json`, `policy.json`, `workflow.json`, `models/`, `vendors/` | Validation schemas, export registry, protected paths, state transitions, model metadata, client templates | The CLI and the renderer only. |
| `.factory/src/`, `pyproject.toml`, `uv.lock`, `run.py`, `.venv/` | The pinned factory runtime: the CLI and gate this repository was reviewed with | Executed by `uv run --project .factory software-factory` and by the hooks. **Not product code: agents are told not to read it, Claude is denied reading it, and the guard refuses shell reads of it.** |
| `.factory/missions/<ID>/` | One mission's records: request, answers, documents, events, results, evidence | The CLI writes them; agents read them through briefs. Committed. |
| `.factory/crew/` | Project knowledge and recipes, with a ledger of your approvals | Frozen into each mission's `crew-context.md`, which briefs include. Committed; protected. |
| `.factory/local/` | Briefs, logs, lanes, proposals, locks | Private to this machine; ignored by Git. |

Why the runtime is copied into each repository instead of used from your global install: every repository runs the exact CLI and gate it was set up and reviewed with, the readiness evidence binds that runtime's fingerprint, and a teammate who clones the repository gets the same factory without a package registry. Upgrading is an explicit `software-factory upgrade`, committed on its own. The global `software-factory` command only bootstraps: it dispatches every project command to the pinned copy.

## Parallel work

Parallelism is decided by the orchestrator on the fly, never by a fixed worker count:

- **Inside a mission:** the plan's tasks declare owned paths and dependencies. `software-factory mission lanes` computes the waves (tasks that can run together) and the critical path. For each ready task, `mission lane-open` gives it its own Git worktree under `.factory/local/lanes/`, and the orchestrator briefs one implementer per lane, all at once. `task-add` refuses tasks that could run together yet may touch the same files. Lanes are integrated into the mission tree one at a time (`mission lane-integrate`, owned paths only), each verified before the next.
- **Across missions:** `mission create --worktree` gives a mission its own worktree and branch beside the repository; you open a separate session there. A task cannot start while a task active in another mission's worktree may touch the same files.

## Validation and trust

Fingerprints bind candidate content and governing inputs to evidence. Local gate failures expose missing or stale checks, unresolved reviews and incomplete scope. They do not establish the identity of the person who wrote a local decision record. Required external reviews and deployment authority must remain in the actual authenticated system.

The current fingerprint implementation rejects symlink files and submodules rather than traversing unbounded external content. Verification requires raw logs under ignored `.factory/local/`, checks their hashes and detects candidate mutation during execution. It is still host process execution, not a sandbox. A new machine without matching local logs must capture a new verification run.

The build includes executable utility tests plus behavioral scenario tests in the package source. [Vendor smoke tests](vendor-smoke-tests.md) separately establish that each installed client loads and follows its configuration. Read [verified vendor behavior](runbooks/vendor-behavior.md) for source links and known differences. A passing syntax test must never be reported as a successful live vendor test.

Start with [setup](runbooks/factory-setup.md). Use [maintenance](runbooks/factory-maintenance.md) for factory changes and [delivery/recovery](runbooks/delivery-and-recovery.md) when integrating a real product pipeline.

The [extension and scaling contract](runbooks/scaling-and-extensions.md) covers registered content, bulk result publication, isolated writers, and supported repository shapes. [Runtime setup](runbooks/runtime-contract.md) binds setup and product checks to stable candidates; [upgrades](runbooks/upgrading.md) preserve reviewed ownership and report customization conflicts.

## Optional semantic assistance

The [Jev architecture](jev-architecture.md) expands the lifecycle with the optional ON/OFF path. The [operating guide](runbooks/semantic-assistance.md) defines claim/source assessment, defaults, budgets, private records and evaluation. It adds a shared tool and canonical skill, not a native coding model or new readiness authority.
