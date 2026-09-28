# Software Factory: Today vs. Target (Visual Review)

This is a companion to [software-factory-gap-assessment.md](software-factory-gap-assessment.md).

- **Grounding.** The states, commands and file paths shown here come from the code at `15f4b87`: `data/workflow.json`, `workflow.py`, `checks.py`, `rendering.py` and `installation.py`.
- **What "target" means.** The target diagrams show the factory after the roadmap in that assessment. The `F-n` labels refer to its findings.

**Color legend** (used in every diagram)

| Color | Meaning |
|---|---|
| 🟩 green | Strong today; keep as is |
| 🟥 red | Gap today |
| 🟧 amber | Fix planned for **Now**, before tagging 0.3.2 |
| 🟦 blue | Fix planned for **Next** |
| 🟪 purple | Planned for **Later** |
| ⬜ grey | Not applicable to a local-first design |

---

## 1. Today: who can write what

What to look for:
- The **thick red arrows** show where the orchestrator agent can record the approvals that are supposed to constrain it (F-1).
- The **dotted red arrows** show where secrets and network access leak out (F-3).

```mermaid
flowchart TB
    classDef ok fill:#d3f9d8,stroke:#2b8a3e,color:#0b3d17
    classDef gap fill:#ffe3e3,stroke:#c92a2a,color:#5c0a0a
    classDef ext fill:#f1f3f5,stroke:#868e96,color:#343a40,stroke-dasharray:4 3
    classDef human fill:#fff3bf,stroke:#e67700,color:#5c3b00

    U["👤 Human operator<br/>approves in chat"]:::human

    subgraph CLIENT["Native coding client session · Claude Code / Codex / Copilot"]
        O["Orchestrator agent<br/>guard: opt-in, Claude only<br/>(default OFF)"]:::gap
        PL["Planner<br/>read + WebFetch/WebSearch"]:::gap
        IM["Implementer<br/>edit + shell"]
        VE["Verifier<br/>shell"]
        RV["Reviewer<br/>read + WebFetch/WebSearch"]:::gap
    end

    subgraph ENGINE["software-factory CLI · deterministic Python"]
        SM["State machine<br/>workflow.json, illegal moves rejected"]:::ok
        FP["Candidate fingerprint<br/>content + governance + Git view + log hashes"]:::ok
        RK["Risk tier<br/>sensitive_paths = 3 globs<br/>no skip/xfail detection"]:::gap
        CK["Check runner<br/>host subprocess<br/>full env minus TYPESAFE_API_KEY"]:::gap
        GT["Readiness gate"]:::ok
    end

    subgraph REC[".factory/missions/ID · committed records"]
        REQ["request.md verbatim<br/>+ hash chain"]:::ok
        SPEC["spec · plan · criteria<br/>criteria must quote request"]:::ok
        DEC["decisions: scope · exception · merge<br/>reference = free text"]:::gap
        REV["reviews<br/>author = free text<br/>owners.maintainer = null"]:::gap
        CIR["ci-result<br/>URL + conclusion unchecked"]:::gap
    end

    ENV[("Developer env<br/>AWS_*, GH_TOKEN, ANTHROPIC_API_KEY")]:::gap
    NET(("Internet")):::gap
    EXT["GitHub · CI · branch protection<br/>outside the factory, not verified"]:::ext

    U -- "request + approval (chat)" --> O
    O --> PL & IM & VE & RV
    O -- "CLI calls" --> SM
    O == "❌ records own scope approval" ==> DEC
    O == "❌ records reviews" ==> REV
    O == "❌ records CI success" ==> CIR
    VE --> CK
    CK -. "❌ inherits secrets" .-> ENV
    PL -. "❌ open egress" .-> NET
    RV -. "❌ open egress" .-> NET
    REQ --> FP
    SPEC --> FP
    FP --> GT
    SM --> GT
    RK --> GT
    DEC --> GT
    REV --> GT
    CIR --> GT
    GT -- "READY_PR = local-unattested" --> EXT
```

---

## 2. Target: who can write what (after Now + Next)

The key change is that **approvals move out of the agent's reach**.
- Only a human, at a TTY or with a signature, can record the approval kinds (scope, exception, merge, release, and human review).
- The guard is on by default, and it denies those writes to agents.
- Checks run with an environment allowlist, optionally with no network.

```mermaid
flowchart TB
    classDef ok fill:#d3f9d8,stroke:#2b8a3e,color:#0b3d17
    classDef now fill:#fff4e6,stroke:#e8590c,color:#5c2a00
    classDef next fill:#e7f5ff,stroke:#1c7ed6,color:#0b2e4f
    classDef human fill:#fff3bf,stroke:#e67700,color:#5c3b00
    classDef ext fill:#f1f3f5,stroke:#868e96,color:#343a40,stroke-dasharray:4 3

    U["👤 Human operator"]:::human
    AP["software-factory approve<br/>TTY + typed confirm · optional Git/SSH signature<br/>F-1"]:::next

    subgraph CLIENT["Native coding client session"]
        O["Orchestrator agent<br/>guard ON by default · F-1<br/>denied: decision(scope/exception/merge/release), ci-result"]:::now
        PL["Planner<br/>read · web opt-in · F-3"]:::next
        IM["Implementer<br/>edit + shell"]
        VE["Verifier<br/>shell"]
        RV["Reviewer<br/>read only · no web · F-3"]:::next
    end

    subgraph ENGINE["software-factory CLI"]
        PF["Governance preflight<br/>refuse brief if AGENTS.md/.mcp.json/.claude differ from base · F-4"]:::now
        SM["State machine"]:::ok
        FP["Candidate fingerprint"]:::ok
        RK["Risk tier<br/>broad sensitive_paths · skip/xfail detection<br/>sensitive ⇒ feature lane · F-5"]:::now
        CK["Check runner<br/>env allowlist · optional bwrap/podman --network none · F-3"]:::next
        SS["Secret scanner on every record input · F-6"]:::now
        EVT["events.jsonl<br/>hash-chained, actor + session · F-7"]:::next
        GT["Readiness gate<br/>verifies chain, attestation, floor"]:::ok
    end

    subgraph REC[".factory/missions/ID"]
        DEC["decisions<br/>approval kinds need attestation"]:::next
        REV["reviews<br/>maintainer required · F-1"]:::now
        CIR["ci-result<br/>optional gh run view verification · F-9"]:::next
    end

    GH["GitHub · CI · branch protection<br/>doctor checks it when gh is authenticated · F-9"]:::ext

    U -- "types approval" --> AP
    AP -- "attested decision" --> DEC
    U -- "request" --> O
    O --> PL & IM & VE & RV
    O -- "allowed CLI calls" --> PF
    PF --> SM
    O -. "⛔ denied" .-> DEC
    O -. "⛔ denied" .-> CIR
    RV -- "structured review" --> REV
    VE --> CK
    CK --> FP
    SS --> DEC & REV
    SM --> EVT
    EVT --> GT
    FP --> GT
    RK --> GT
    DEC --> GT
    REV --> GT
    CIR --> GT
    GT -- "READY_PR" --> GH
    GH -- "verified CI + merge commit" --> CIR
```

---

## 3. Mission lifecycle with every gate (target)

This is the real state machine from `workflow.json`, with the check at each transition.
- 🟩 gates already work today.
- 🟧 and 🟦 gates are what the roadmap adds.

```mermaid
flowchart TB
    classDef st fill:#f8f9fa,stroke:#495057,color:#212529
    classDef ok fill:#d3f9d8,stroke:#2b8a3e,color:#0b3d17
    classDef now fill:#fff4e6,stroke:#e8590c,color:#5c2a00
    classDef next fill:#e7f5ff,stroke:#1c7ed6,color:#0b2e4f
    classDef hold fill:#ffe3e3,stroke:#c92a2a,color:#5c0a0a
    classDef term fill:#343a40,stroke:#212529,color:#ffffff

    IN(["mission create<br/>verbatim request"]):::st
    G0["🚦 Intake gate<br/>• trust tier source → brief framing F-10<br/>• secret scan F-6"]:::next
    PROPOSED["PROPOSED"]:::st
    G1["🚦 accept-scope<br/>🟩 context.md authored<br/>🟩 criteria quote request<br/>🟩 no open ambiguity<br/>🟩 scope bound to spec hash<br/>🟦 attested human approval F-1<br/>🟧 sensitive paths ⇒ feature lane F-5<br/>🟧 governance preflight F-4"]
    PLANNED["PLANNED"]:::st
    IMPL["IMPLEMENTING<br/>tasks TODO→RUNNING<br/>owned_paths enforced 🟩"]:::st
    G2["🚦 verify<br/>🟩 checks bound by hash<br/>🟩 CandidateMonitor detects writes<br/>🟦 env allowlist / no network F-3<br/>🟦 max_task_minutes F-8"]
    VERIFYING["VERIFYING"]:::st
    REVIEWING["REVIEWING<br/>code · acceptance · adversarial"]:::st
    G3["🚦 Readiness gate<br/>🟩 fingerprint unchanged<br/>🟩 every AC evidenced + pass<br/>🟩 check-weakening ⇒ exception<br/>🟧 extended protected floor F-4<br/>🟧 skip/xfail detection F-5<br/>🟧 maintainer ≠ reviewer required F-1<br/>🟦 high risk ⇒ attested human review F-1<br/>🟦 events chain intact F-7"]
    READY["READY_PR<br/>local evidence only"]:::st
    G4["🚦 merge<br/>🟩 real merge commit on trunk<br/>🟦 CI verified via gh F-9<br/>🟦 attested merge decision F-1"]
    MERGED["MERGED"]:::st
    STAGING["STAGING"]:::st
    AWAIT["AWAITING_RELEASE"]:::st
    DEPLOY["DEPLOYING"]:::st
    OBS["OBSERVING<br/>human-entered observation"]:::st
    DELIVERED(["DELIVERED"]):::term
    RECOVERING["RECOVERING"]:::hold
    RECOVERED(["RECOVERED<br/>→ follow_up_mission"]):::term
    BLOCKED["BLOCKED / PAUSED<br/>reason required 🟩<br/>🟦 stale_hours ⇒ notify owner F-8"]:::hold
    CANCELED(["CANCELED"]):::term
    HALT[".factory/local/HALT<br/>kill switch F-8"]:::next

    IN --> G0 --> PROPOSED --> G1 --> PLANNED --> IMPL --> G2 --> VERIFYING
    VERIFYING -- "fail · repair budget" --> IMPL
    VERIFYING --> REVIEWING --> G3 --> READY
    REVIEWING -- "findings" --> IMPL
    READY -- "CI failed" --> IMPL
    READY --> G4 --> MERGED --> STAGING --> AWAIT --> DEPLOY --> OBS --> DELIVERED
    DEPLOY -- "unhealthy" --> RECOVERING
    OBS -- "unhealthy" --> RECOVERING --> RECOVERED
    IMPL -. "budget exhausted" .-> BLOCKED
    PROPOSED & PLANNED & IMPL & REVIEWING -. "hold" .-> BLOCKED
    BLOCKED -.-> CANCELED
    HALT -. "stops new runs" .-> IMPL

    class G1,G2,G3,G4 ok
```

---

## 4. Scope approval: today vs. target (F-1)

This is the single most important change. Today, a "human approval" is just a JSON record that anyone, including the agent, can write.

```mermaid
sequenceDiagram
    autonumber
    actor H as Human
    participant O as Orchestrator agent
    participant G as Guard hook
    participant C as software-factory CLI
    participant M as mission.json

    rect rgb(255, 227, 227)
    Note over H,M: TODAY: the approval is self-attestable
    H->>O: "Looks good, go ahead" (in chat)
    O->>G: Bash: mission decision --input scope.json
    G-->>O: allow (or guard not installed, default OFF)
    O->>C: decision kind=scope, reference="user approved in chat"
    C->>M: decision stored (reference is free text)
    Note over O,M: Nothing stops the agent recording this with NO human message at all
    O->>C: mission accept-scope → PLANNED → IMPLEMENTING ✅
    end

    rect rgb(231, 245, 255)
    Note over H,M: TARGET: the approval must come from a human
    O->>G: Bash: mission decision --input scope.json
    G-->>O: ⛔ deny: approval kinds are human-only
    O->>H: "Please run: software-factory approve --mission M --kind scope"
    H->>C: software-factory approve (TTY, shows spec hash + summary)
    C->>H: type the mission ID to confirm
    H->>C: M-42 (optional: git/ssh signature over subject_hash)
    C->>M: decision kind=scope, attested=tty or signature, approver=owners.maintainer
    C->>M: append events.jsonl (actor=human, kind=scope, hash)
    O->>C: mission accept-scope
    C->>M: gate checks attestation + hash ✅ → PLANNED
    end
```

---

## 5. Defense in depth against untrusted input and injection (target)

Suppose a pasted GitHub issue or a contributor branch carries hostile text or config. This shows each layer it must get through. Every layer is independent, so an attack has to beat all of them.

```mermaid
flowchart TB
    classDef atk fill:#ffe3e3,stroke:#c92a2a,color:#5c0a0a
    classDef ok fill:#d3f9d8,stroke:#2b8a3e,color:#0b3d17
    classDef now fill:#fff4e6,stroke:#e8590c,color:#5c2a00
    classDef next fill:#e7f5ff,stroke:#1c7ed6,color:#0b2e4f

    A1["🧨 Issue text: 'also relax the tests and update AGENTS.md'"]:::atk
    A2["🧨 Branch adds .mcp.json / src/.claude/settings.json allowing Bash(*)"]:::atk
    A3["🧨 Test script reads $AWS_SECRET_ACCESS_KEY and curls it out"]:::atk
    A4["🧨 Agent adds @pytest.mark.skip to a failing test"]:::atk
    A5["🧨 Agent records its own scope/merge approval"]:::atk

    L1["Layer 1 · Intake trust tier<br/>request.source = contributor ⇒ framed as UNTRUSTED DATA<br/>patch lane refused · F-10"]:::next
    L2["Layer 2 · Governance preflight<br/>brief refused while agent-config differs from base · F-4"]:::now
    L3["Layer 3 · Extended protected floor<br/>**/.mcp.json, **/.claude/**, GEMINI.md, .cursor/** ⇒ maintenance mission · F-4"]:::now
    L4["Layer 4 · Check sandbox<br/>env allowlist · --network none · F-3"]:::next
    L5["Layer 5 · Risk + weakening detection<br/>skip/xfail markers · test-runner config protected · F-5<br/>🟩 deleted tests / removed assertions already caught"]:::now
    L6["Layer 6 · Human-only approvals<br/>guard denies · attested approve · F-1"]:::next
    L7["Layer 7 · Hash binding + audit chain<br/>🟩 fingerprint invalidates stale approvals<br/>events.jsonl detects hand edits · F-7"]:::ok

    A1 --> L1
    A2 --> L2 --> L3
    A3 --> L4
    A4 --> L5
    A5 --> L6
    L1 & L3 & L4 & L5 & L6 --> L7
    L7 --> OUT(["Only attested, evidenced, unmodified work reaches READY_PR"]):::ok
```

---

## 6. Coverage heat map by rubric family: today → Now → Next

Each box is one family from the reference model (A–L). Read across a row to see how that plane matures.

```mermaid
flowchart LR
    classDef gap fill:#ffe3e3,stroke:#c92a2a,color:#5c0a0a
    classDef part fill:#fff4e6,stroke:#e8590c,color:#5c2a00
    classDef good fill:#d3f9d8,stroke:#2b8a3e,color:#0b3d17
    classDef na fill:#f1f3f5,stroke:#adb5bd,color:#495057

    subgraph T["TODAY @15f4b87"]
        direction TB
        tA["A Intake<br/>single channel, no trust tier"]:::gap
        tB["B Triage<br/>lanes are caller-chosen"]:::part
        tC["C Spec + approval<br/>hash-bound, identity is free text"]:::part
        tD["D Execution<br/>no sandbox/egress/env scope"]:::gap
        tE["E Review<br/>agent-only, maintainer null"]:::part
        tF["F Verify + CI<br/>local strong, CI unchecked"]:::part
        tG["G Ship + stuck<br/>never merges; no notify"]:::part
        tH["H Monitor<br/>manual observation only"]:::na
        tI["I Control plane<br/>state machine; no event log"]:::part
        tJ["J Data<br/>records not secret-scanned"]:::part
        tK["K Self-improve<br/>none; safe by absence"]:::na
        tL["L Metrics<br/>none"]:::gap
    end

    subgraph N["AFTER NOW (0.3.2)"]
        direction TB
        nA["A · unchanged"]:::gap
        nB["B · sensitive ⇒ feature lane"]:::part
        nC["C · maintainer required"]:::part
        nD["D · floor + preflight"]:::part
        nE["E · guard denies self-review"]:::part
        nF["F · skip detection"]:::part
        nG["G · unchanged"]:::part
        nH["H · n/a"]:::na
        nI["I · docs = code"]:::part
        nJ["J · secret scan"]:::good
        nK["K · n/a"]:::na
        nL["L · none"]:::gap
    end

    subgraph X["AFTER NEXT"]
        direction TB
        xA["A · trust tier"]:::part
        xB["B · lanes enforced"]:::good
        xC["C · attested approval"]:::good
        xD["D · env allowlist + runner"]:::good
        xE["E · human review at high risk"]:::good
        xF["F · gh-verified CI"]:::good
        xG["G · stale + notify + HALT"]:::good
        xH["H · Later"]:::na
        xI["I · events.jsonl"]:::good
        xJ["J · scanned"]:::good
        xK["K · Later: benchmark first"]:::na
        xL["L · Later: report"]:::part
    end

    tA --> nA --> xA
    tB --> nB --> xB
    tC --> nC --> xC
    tD --> nD --> xD
    tE --> nE --> xE
    tF --> nF --> xF
    tG --> nG --> xG
    tH --> nH --> xH
    tI --> nI --> xI
    tJ --> nJ --> xJ
    tK --> nK --> xK
    tL --> nL --> xL
```

---

## 7. Roadmap dependencies: what must land before what

Arrows mean "must come first". Two dependencies matter most:
- **F-1** is split into a quick guard fix (Now) and the attested `approve` command (Next).
- **No self-improvement loop may land before the regression benchmark (K4)** exists.

```mermaid
flowchart LR
    classDef now fill:#fff4e6,stroke:#e8590c,color:#5c2a00
    classDef next fill:#e7f5ff,stroke:#1c7ed6,color:#0b2e4f
    classDef later fill:#f3f0ff,stroke:#7048e8,color:#2b1a66
    classDef gate fill:#212529,stroke:#000,color:#fff

    subgraph NOW["NOW · blocks the 0.3.2 tag"]
        direction TB
        F2["F-2 Docs = code<br/>brief --kind plan/verify · WP3 base_history · self-test"]:::now
        F1a["F-1a Guard denies approval-kind decision + ci-result<br/>owners.maintainer required"]:::now
        F4["F-4 Extend PROTECTED_FLOOR + preflight"]:::now
        F5["F-5 Broaden sensitive_paths · skip/xfail · lane rule"]:::now
        F6["F-6 Secret-scan record inputs"]:::now
        F14["F-14 verification.md + version bump"]:::now
    end

    TAG{{"🏷️ tag 0.3.2"}}:::gate

    subgraph NEXT["NEXT"]
        direction TB
        F1b["F-1b software-factory approve<br/>TTY / signature attestation"]:::next
        F1c["F-1c Claude enforcement ON by default"]:::next
        F7["F-7 events.jsonl hash chain"]:::next
        F3a["F-3a checks env allowlist · reviewer no web"]:::next
        F3b["F-3b optional sandbox runner"]:::next
        F8["F-8 stale_hours · notify hook · HALT"]:::next
        F9["F-9 gh-verified CI + branch protection"]:::next
        F10["F-10 request trust tier"]:::next
    end

    subgraph LATER["LATER"]
        direction TB
        K4["K4 selftest: frozen replay benchmark<br/>fails closed per scenario"]:::later
        L1["F-12 report: lead time · repairs · gate failures"]:::later
        IMP["Governed improver<br/>PR-only · protected gate fields"]:::later
        UI["F-15 UI evidence convention"]:::later
    end

    F2 & F1a & F4 & F5 & F6 & F14 --> TAG
    TAG --> F1b & F3a & F7 & F8 & F9 & F10
    F1a --> F1b --> F1c
    F3a --> F3b
    F4 --> F10
    F7 --> L1
    F1b --> IMP
    F7 --> IMP
    K4 ==> IMP
    L1 --> IMP
```

---

## 8. Later: what a governed self-improvement loop would look like

This is optional and must not be built until §7's prerequisites exist. It maps the talk's "observer agent" onto this project's own controls, so an improvement can never weaken a gate.

```mermaid
flowchart TB
    classDef ok fill:#d3f9d8,stroke:#2b8a3e,color:#0b3d17
    classDef later fill:#f3f0ff,stroke:#7048e8,color:#2b1a66
    classDef human fill:#fff3bf,stroke:#e67700,color:#5c3b00
    classDef stop fill:#ffe3e3,stroke:#c92a2a,color:#5c0a0a

    RUNS["Completed missions<br/>events.jsonl · reviews · gate reasons · repair attempts"]:::ok
    SIG["Signals<br/>human corrections to reviews · reopened missions<br/>repeated gate failures · repair-budget hits"]:::later
    OBS["Observer run (scheduled, read-only)<br/>≥ N independent corrections before proposing"]:::later
    PROP["Proposal = maintenance mission<br/>diff to .factory/skills/** or roles/**"]:::later
    LOCK{"Touches protected gate fields?<br/>required_decisions · protected_paths · limits · gate code"}
    DENY["Rejected automatically<br/>needs human-signed exception"]:::stop
    BENCH{"K4 selftest<br/>every scenario passes?<br/>no dimension regresses"}
    FAIL["Rejected with benchmark diff"]:::stop
    REVIEW["code + acceptance + adversarial reviews"]:::ok
    HUMAN["👤 attested human approve"]:::human
    MERGE["Merged via normal Git flow<br/>render --check · lockfile updated"]:::ok
    ROLL["One-step rollback<br/>upgrade --to previous"]:::later

    RUNS --> SIG --> OBS --> PROP --> LOCK
    LOCK -- "yes" --> DENY
    LOCK -- "no" --> BENCH
    BENCH -- "no" --> FAIL
    BENCH -- "yes" --> REVIEW --> HUMAN --> MERGE
    MERGE -. "regression observed" .-> ROLL
    MERGE --> RUNS
```

---

### How to view

- **VS Code.** Open this file and press `Ctrl+Shift+V` for Markdown preview. You need a Mermaid-capable preview, such as the built-in support in recent versions or the "Markdown Preview Mermaid Support" extension.
- **GitHub.** Mermaid renders natively.
