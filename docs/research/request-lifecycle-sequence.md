# Request Lifecycle (0.3.2)

How one user request moves through software-factory 0.3.2, from intake to merge.

**Example request.** *"Add rate limiting to the login endpoint in src/auth/login.py"*. It touches auth code, so it also exercises the 0.3.2 sensitive-path and approval rules.

**Participants**

| Participant | Role |
|---|---|
| 🧑 **User** | Types commands in their own terminal and approves |
| 🤖 **Orchestrator** | The coding client (Claude Code / Codex / Copilot) following the factory role. Coordinates only and never edits files |
| 🛡️ **Guard** | Opt-in Claude PreToolUse hook. Denies the orchestrator file edits, `mission approve` and `mission ci-result` |
| 👷 **Planner / Implementer / Verifier / Reviewer** | Specialists that return text. Only the implementer edits files |
| ⚙️ **CLI** | `software-factory`: deterministic records, checks and refusals |
| 📁 **Records** | `.factory/missions/M-0001/` (request, spec, plan, evidence, decisions, reviews) |

Diagrams: **⓪** one-page flow · **①** intake to approved scope · **②** implement, verify and review · **③** gate, CI and merge · **④** holds and resets.

---

## ⓪ One-page flow

**How to read it**
- **Boxes:** grey boxes are mission states.
- **Colored fills:**
  - 🟩 green are checks the CLI enforces.
  - 🟨 yellow are steps only you can do.
  - 🟥 red are refusals and holds.
  - 🟦 blue is agent work.
- **Arrows:**
  - Solid arrows are the normal path.
  - Dotted arrows loop back or wait for you.

```mermaid
flowchart TB
    classDef state fill:#f1f3f5,stroke:#495057,color:#212529,font-weight:bold
    classDef agent fill:#e7f5ff,stroke:#1c7ed6,color:#0b2e4f
    classDef check fill:#d3f9d8,stroke:#2b8a3e,color:#0b3d17
    classDef human fill:#fff3bf,stroke:#e67700,color:#5c3b00
    classDef stop fill:#ffe3e3,stroke:#c92a2a,color:#5c0a0a
    classDef done fill:#343a40,stroke:#212529,color:#ffffff

    REQ(["🧑 Request<br/>'Add rate limiting to the login endpoint'"]):::human
    SEC{"Secret in the request?"}:::check
    REFUSE1["Refused: remove the secret"]:::stop
    PROPOSED["PROPOSED<br/>request.md stored verbatim + hash chain"]:::state

    PRE{"Agent config changed?<br/>AGENTS.md, .mcp.json, .claude/"}:::check
    REFUSE2["Brief refused<br/>revert, or use a maintenance mission"]:::stop
    CTX["👷 Planner: context map"]:::agent
    CLAR["🤖 One set of clarifying questions<br/>answers stored verbatim"]:::agent
    SPEC["👷 Planner: spec · criteria · plan · recovery"]:::agent
    QUOTE{"Every criterion quotes<br/>the request exactly?"}:::check

    APPROVE_S["🧑 mission approve --kind scope<br/>type the mission ID in your terminal"]:::human
    AGENT_NO["Agent tries to approve<br/>⛔ CLI refuses · guard denies"]:::stop
    PLANNED["PLANNED<br/>spec, criteria, context, plan hashes bound"]:::state

    IMPL["IMPLEMENTING<br/>👷 Implementer edits owned paths only"]:::state
    VERIFY{"verify<br/>checks pass, bound to the code fingerprint?"}:::check
    BUDGET{"Repair budget left?"}:::check
    RESULTS["🤖 Record results<br/>evidence per criterion"]:::agent
    REVIEWING["REVIEWING"]:::state
    RISK{"Risk from the real diff<br/>sensitive path · skip/xfail · test config ·<br/>deleted tests · protected path"}:::check
    REV_LOW["👷 code review<br/>(+ acceptance in feature lane)"]:::agent
    REV_HIGH["👷 code + acceptance + adversarial reviews"]:::agent
    APPROVE_E["🧑 mission approve --kind exception<br/>(sensitive or check change)"]:::human

    GATE{"Gate<br/>fingerprint unchanged · every AC passes ·<br/>approvals match hashes · no protected path ·<br/>maintainer set · reviewer ≠ maintainer"}:::check
    READY["READY_PR<br/>local evidence only + PR packet"]:::state

    CI["🧑 Open PR, CI runs<br/>🧑 mission ci-result"]:::human
    CIOK{"CI passed?"}:::check
    MERGE["🧑 Merge PR<br/>🧑 mission approve --kind merge"]:::human
    MERGECHK{"Real merge commit on trunk<br/>contains the candidate?"}:::check
    MERGED(["MERGED"]):::done
    DELIVERY["Optional delivery (off by default)<br/>🧑 release approval → DEPLOYING → OBSERVING → DELIVERED"]:::state

    BLOCKED["BLOCKED / PAUSED<br/>reason + next step recorded"]:::stop
    RESUME["🧑 answers or fixes<br/>mission resume --resolution"]:::human

    REQ --> SEC
    SEC -- "yes" --> REFUSE1
    SEC -- "no" --> PROPOSED --> PRE
    PRE -- "yes" --> REFUSE2
    PRE -- "no" --> CTX --> CLAR --> SPEC --> QUOTE
    QUOTE -. "no: planner revises" .-> SPEC
    QUOTE -- "yes" --> APPROVE_S
    AGENT_NO -. "cannot bypass" .-> APPROVE_S
    APPROVE_S --> PLANNED --> IMPL --> VERIFY
    VERIFY -- "fail" --> BUDGET
    BUDGET -- "yes: next attempt" --> IMPL
    BUDGET -- "no" --> BLOCKED
    VERIFY -- "pass" --> RESULTS --> REVIEWING --> RISK
    RISK -- "low" --> REV_LOW --> GATE
    RISK -- "high" --> REV_HIGH --> APPROVE_E --> GATE
    REV_LOW -. "findings" .-> IMPL
    REV_HIGH -. "findings" .-> IMPL
    GATE -. "fail: rework" .-> IMPL
    GATE -- "pass" --> READY --> CI --> CIOK
    CIOK -. "no: back to work" .-> IMPL
    CIOK -- "yes" --> MERGE --> MERGECHK
    MERGECHK -- "yes" --> MERGED --> DELIVERY
    CLAR -. "open question" .-> BLOCKED
    BLOCKED --> RESUME
    RESUME -. "back to previous state" .-> IMPL
    SPEC -. "spec changed after approval:<br/>scope reset, approve again" .-> APPROVE_S
```

---

**Colors:** 🟨 yellow bands are human-only actions. 🟥 red bands are refusals added in 0.3.2.

---

## ① Intake → approved scope

```mermaid
sequenceDiagram
    autonumber
    actor U as 🧑 User
    participant O as 🤖 Orchestrator
    participant G as 🛡️ Guard
    participant P as 👷 Planner
    participant C as ⚙️ CLI
    participant R as 📁 Records

    U->>O: /factory-build "Add rate limiting to the login endpoint..."
    O->>C: mission create --kind feature --request-file - (verbatim)
    C->>C: scan request for secrets (0.3.2)
    alt request holds a token or key
        rect rgb(255, 227, 227)
        C-->>O: refused: looks like a secret
        end
    else clean
        C->>R: request.md byte for byte + hash chain
        C-->>O: M-0001 PROPOSED
    end

    O->>C: mission brief --kind context
    C->>C: governance preflight: AGENTS.md, .mcp.json, .claude/ vs base (0.3.2)
    alt agent config differs from base
        rect rgb(255, 227, 227)
        C-->>O: refused: revert or use a maintenance mission
        end
    else unchanged
        C-->>O: context brief
    end
    O->>P: context brief
    P-->>O: codebase map, affected files, open questions
    O->>C: mission record-doc --doc context
    C->>R: context.md

    O->>U: one set of clarifying questions
    U-->>O: "per IP, 5 failures per minute"
    O->>C: mission clarify (verbatim)
    C->>R: clarification + new chain head

    O->>C: mission brief --kind plan
    O->>P: plan brief
    P-->>O: spec.md, criteria (AC-n quoting the request), plan.md with mermaid, recovery.md
    O->>C: record-doc spec / plan, mission criteria
    C->>C: every AC excerpt must appear verbatim in the request
    C->>R: spec.md, plan.md, criteria

    O->>U: here is the spec. Please approve it in your terminal
    O->>C: mission decision (kind scope)
    rect rgb(255, 227, 227)
    C-->>O: refused: scope is the user's approval, run mission approve
    end
    O->>G: Bash: mission approve ...
    rect rgb(255, 227, 227)
    G-->>O: denied: records the user's own approval
    end

    rect rgb(255, 243, 191)
    U->>C: software-factory mission approve --mission M-0001 --kind scope --reference "chat"
    C->>U: binds to spec.md sha256 ... type the mission ID
    U->>C: M-0001
    C->>R: decision D-SCOPE-xxxxxxxx
    end

    O->>C: mission accept-scope
    C->>C: bind spec, criteria, context, plan hashes
    C->>R: M-0001 PLANNED
    O->>C: mission task-add T-1 (owned src/auth/**, tests/**, checks, AC-1..n)
```

---

## ② Implement → verify → review

```mermaid
sequenceDiagram
    autonumber
    actor U as 🧑 User
    participant O as 🤖 Orchestrator
    participant I as 👷 Implementer
    participant V as 👷 Verifier
    participant RV as 👷 Reviewer
    participant C as ⚙️ CLI
    participant R as 📁 Records

    O->>C: mission transition --to IMPLEMENTING
    O->>C: task-transition T-1 --to RUNNING (attempt 1 of budget)
    O->>C: mission brief --task T-1
    C->>C: governance preflight again (0.3.2)
    O->>I: task brief
    I->>I: edit src/auth/login.py and tests
    I-->>O: provisional report (not a result, not verified)

    O->>C: task-transition T-1 --to VERIFYING
    O->>C: verify --mission M-0001 --revision R-1
    C->>C: run configured checks: argv only, timeouts, output caps
    C->>C: watch the tree for unexpected writes
    C->>R: evidence/R-1/checks.json bound to candidate fingerprint

    alt a check fails
        C-->>O: fail
        O->>C: task-transition T-1 --to RUNNING (attempt 2)
        Note over O,C: budget exhausted ⇒ task and mission BLOCKED
    else all checks pass
        C-->>O: pass
    end

    opt e2e / manual criteria
        O->>C: mission brief --kind verify
        O->>V: verify brief
        V-->>O: observed evidence (text)
    end

    O->>C: mission record-result (AC-1 → check:tests ...)
    C->>R: result bound to fingerprint
    O->>C: task-transition T-1 --to DONE
    O->>C: transition --to VERIFYING, then --to REVIEWING

    O->>C: mission risk
    C->>C: inspect the real diff
    Note right of C: src/auth/** is sensitive ⇒ HIGH (0.3.2 defaults)<br/>added skip/xfail or conftest.py ⇒ HIGH (0.3.2)<br/>deleted tests / removed asserts ⇒ HIGH
    C-->>O: tier high: needs code + acceptance + adversarial

    loop each required review kind
        O->>C: mission brief --kind code | acceptance | adversarial
        O->>RV: review brief (read-only reviewer)
        RV-->>O: verdict per AC + findings
        O->>C: mission review (author, brief_hash, verdicts)
        C->>C: author must differ from owners.maintainer
        C->>R: review bound to fingerprint
    end

    rect rgb(255, 243, 191)
    Note over U,C: sensitive path ⇒ the gate needs an exception only the user records
    U->>C: software-factory mission approve --mission M-0001 --kind exception --reference "reviewed auth change"
    C->>U: binds to candidate fingerprint ... type the mission ID
    U->>C: M-0001
    C->>R: decision D-EXCEPTION-xxxxxxxx
    end
```

---

## ③ Gate → READY_PR → CI → merge

```mermaid
sequenceDiagram
    autonumber
    actor U as 🧑 User
    participant O as 🤖 Orchestrator
    participant G as 🛡️ Guard
    participant C as ⚙️ CLI
    participant R as 📁 Records
    participant GH as GitHub / CI

    O->>C: record-doc --doc recovery
    O->>C: software-factory gate --mission M-0001
    C->>C: fingerprint unchanged since verify and reviews?
    C->>C: every AC evidenced with a pass verdict?
    C->>C: scope matches spec hash, exception matches fingerprint?
    C->>C: no protected path (floor now includes .mcp.json, nested .claude/) touched?
    C->>C: owners.maintainer set? (0.3.2)
    alt any reason
        C-->>O: fail + exact reasons
        O->>C: transition --to IMPLEMENTING (rework)
    else pass
        C-->>O: pass
        O->>C: transition --to READY_PR
        O->>C: packet (request → criterion → evidence → verdict)
        C->>R: M-0001 READY_PR (local evidence only)
    end

    O->>U: ready. Open the PR, then record CI with this exact command
    O->>G: Bash: mission ci-result ...
    rect rgb(255, 227, 227)
    G-->>O: denied: the user records CI results
    end

    rect rgb(255, 243, 191)
    U->>GH: push branch, open PR
    GH-->>U: CI run result
    U->>C: software-factory mission ci-result --url ... --head SHA --conclusion success
    C->>C: head must equal the committed, gated candidate
    C->>R: delivery.ci_ref
    end

    alt CI failed
        U->>C: ci-result --conclusion failure --reason "..."
        C->>R: READY_PR → IMPLEMENTING
    else CI passed and PR merged
        rect rgb(255, 243, 191)
        U->>GH: merge PR
        U->>C: software-factory mission approve --mission M-0001 --kind merge --reference MERGE_SHA
        C->>U: type the mission ID
        U->>C: M-0001
        end
        O->>C: transition --to MERGED
        C->>C: merge commit exists on recorded trunk and contains the candidate
        C->>R: M-0001 MERGED
    end

    opt delivery enabled (off by default)
        Note over U,R: STAGING → AWAITING_RELEASE → DEPLOYING (needs the user's release approval) → OBSERVING → DELIVERED<br/>unhealthy ⇒ RECOVERING → RECOVERED → follow-up mission. The factory never deploys.
    end
```

---

## ④ Holds and scope resets (can happen at any step)

```mermaid
sequenceDiagram
    autonumber
    actor U as 🧑 User
    participant O as 🤖 Orchestrator
    participant C as ⚙️ CLI
    participant R as 📁 Records

    alt an open question the agent must not guess
        O->>C: mission block --reason "Need threshold" --next "Ask user"
        C->>R: BLOCKED (reason + next step)
        O->>U: one concrete question
        U-->>O: answer
        O->>C: mission resume --to <previous> --resolution "User chose 5/min"
    else spec or criteria change after approval
        O->>C: record-doc spec / mission criteria
        C->>R: scope reset: tasks back to TODO, attempts kept
        rect rgb(255, 243, 191)
        U->>C: mission approve --kind scope (new spec hash)
        end
        O->>C: mission accept-scope again
    else constitution changed
        C-->>O: "Constitution changed" on the next transition
        O->>C: mission block --reason "Constitution changed"
        rect rgb(255, 243, 191)
        U->>C: mission approve --kind exception --subject-hash NEW_CONSTITUTION --id D-CONST-xxxxxxxx
        end
        O->>C: mission accept-scope (base advances, recorded in base_history)
        C->>R: back to PLANNED, re-verify and re-review
    end
```

---

## What these diagrams do not show (limits in 0.3.2)

- **The terminal check is not authentication.** Codex and Copilot sessions follow instructions only, and a pseudo-terminal (for example `script`) passes the check. The Claude guard denies it. Records remain local-unattested.
- **Checks are not sandboxed.** They run on the host without an environment allowlist or network restriction.
- **No automatic time limits.** There are no cost budgets and no time-based stuck alerts. Holds are recorded, but nothing notifies anyone.
