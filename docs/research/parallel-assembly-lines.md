# Parallel Assembly Lines: Design

This is a proposal; none of it is implemented yet.

**Problem.** Today one implementer works one task at a time:

- `limits.parallel_writers` is fixed at `1`.
- `task-transition` refuses a second RUNNING task anywhere in the workspace.

**Principle.** There is **no worker pool and no fixed lane count**:

- The **orchestrator decides on the fly** how many lanes to open, which tasks to start, and whether to split, hold or re-order work, based on the task graph and on what is happening.
- The **CLI only enforces invariants**, so a decision can be fast without being unsafe.

| The orchestrator decides (judgment) | The CLI enforces (deterministic) |
|---|---|
| How many lanes to open right now | A lane starts only when its dependencies are integrated |
| Which ready task goes first (critical path, risk, size) | Running lanes never own overlapping paths |
| Splitting a big task into narrower lanes, or merging small ones | A split keeps the same criteria, so scope is unchanged |
| Retry, investigate, or block a failing lane | A lane may integrate only its own owned paths |
| When to ask for reviews | Repair budget per lane; gate on the integrated result |
| Holding a lane back (too risky, too uncertain) | Optional circuit breaker `limits.lane_fuse` against runaway spend (a fuse, not a pool) |

---

## 1. The orchestrator's decision loop

```mermaid
flowchart TB
    classDef cli fill:#e7f5ff,stroke:#1c7ed6,color:#0b2e4f
    classDef orch fill:#fff4e6,stroke:#e8590c,color:#5c2a00
    classDef lane fill:#f3f0ff,stroke:#7048e8,color:#2b1a66
    classDef human fill:#fff3bf,stroke:#e67700,color:#5c3b00
    classDef ok fill:#d3f9d8,stroke:#2b8a3e,color:#0b3d17
    classDef stop fill:#ffe3e3,stroke:#c92a2a,color:#5c0a0a

    START(["🧑 Scope approved<br/>tasks · depends_on · owned_paths"]):::human
    READY["⚙️ CLI: mission ready<br/>tasks whose dependencies are integrated<br/>and whose paths are free"]:::cli
    DECIDE{"🤖 Orchestrator decides now<br/>critical path · size · risk · failures so far"}:::orch
    START --> READY --> DECIDE

    DECIDE -- "split a big task" --> SPLIT["⚙️ CLI: task split<br/>narrower owned paths, same criteria"]:::cli
    SPLIT --> READY
    DECIDE -- "hold back (risky or uncertain)" --> HOLD["stays queued<br/>re-evaluated on the next event"]:::orch
    DECIDE -- "open lanes (any number)" --> OPEN["⚙️ CLI: mission lane open T-n<br/>worktree + branch from integration head<br/>refuses overlapping paths"]:::cli

    subgraph LANES["Lanes run side by side: one worktree, one implementer each"]
        direction LR
        L1["👷 T-1"]:::lane
        L2["👷 T-2"]:::lane
        L3["👷 T-3"]:::lane
    end
    OPEN --> L1 & L2 & L3
    L1 & L2 & L3 --> VER["⚙️ verify --task T-n<br/>evidence bound to that lane's worktree"]:::cli
    VER --> RES{"green?"}:::orch

    RES -- "yes" --> INT["⚙️ CLI: mission integrate T-n<br/>owned paths only · merge into integration branch<br/>smoke check after every merge"]:::cli
    INT -- "merged: an event" --> READY
    INT -- "conflict or smoke failure" --> FIX
    RES -- "no" --> FIX{"🤖 Orchestrator on a failed lane<br/>other lanes keep going"}:::orch
    FIX -- "retry (budget left)" --> OPEN
    FIX -- "investigate" --> DIAG["👷 verifier or planner diagnoses"]:::lane
    DIAG --> FIX
    FIX -- "stop this lane" --> BLK["T-n BLOCKED<br/>only this lane stops"]:::stop
    BLK --> ASK(["🧑 you answer one question<br/>mission resume"]):::human
    ASK --> READY

    DECIDE -- "all tasks integrated" --> FINAL["⚙️ full verify on the integration branch<br/>👷 reviews: code per lane + acceptance and adversarial on the whole<br/>⚙️ readiness gate"]:::cli
    FINAL --> PR(["READY_PR"]):::ok
```

---

## 2. One mission over time, with lanes opened on the fly

```mermaid
sequenceDiagram
    autonumber
    actor U as 🧑 You
    participant O as 🤖 Orchestrator
    participant C as ⚙️ CLI
    participant A as 👷 Implementer A
    participant B as 👷 Implementer B
    participant D as 👷 Implementer C
    participant R as 👷 Reviewer

    U->>O: approve M-0003 scope
    O->>C: mission ready
    C-->>O: T-0 contract (everything else depends on it)
    O->>C: lane open T-0
    O->>A: brief T-0 (define the limiter interface)
    A-->>O: done
    O->>C: verify --task T-0, then integrate T-0
    O->>C: mission ready
    C-->>O: T-1, T-2, T-3 ready · paths disjoint

    Note over O: Decides 3 lanes now (independent, small, low risk)
    par lane T-1
        O->>A: brief T-1 limiter core (worktree T-1)
    and lane T-2
        O->>B: brief T-2 429 response (worktree T-2)
    and lane T-3
        O->>D: brief T-3 config + docs (worktree T-3)
    end

    D-->>O: T-3 done
    O->>C: verify --task T-3 ✓ → integrate T-3 ✓ (smoke check passes)
    B-->>O: T-2 done
    O->>C: verify --task T-2 ✗ (one check fails)
    Note over O: Retries only T-2, T-1 keeps working
    O->>B: repair brief T-2 (attempt 2)
    par code review while building
        O->>R: review T-3 (already integrated)
    and
        A-->>O: T-1 done
    end
    O->>C: verify --task T-1 ✓ → integrate T-1 ✓
    B-->>O: T-2 fixed
    O->>C: verify --task T-2 ✓ → integrate T-2 ✓
    O->>C: mission ready
    C-->>O: T-4 wiring (was waiting on T-1 and T-2)
    Note over O: T-4 touches shared registry files, so it runs alone
    O->>A: brief T-4 wiring
    A-->>O: done
    O->>C: integrate T-4 → full verify → gate
    O->>R: acceptance + adversarial review on the integrated result
    O-->>U: READY_PR · packet · run mission ci-result when CI is green
```

---

## 3. The time it saves

The same six tasks, of about 20 minutes each, run first in sequence and then as assembly lines.

```mermaid
gantt
    title Same mission: one line vs. lanes opened on the fly
    dateFormat HH:mm
    axisFormat %H:%M

    section Today (one implementer)
    T-0 contract          :s0, 10:00, 15m
    T-1 limiter core      :s1, after s0, 25m
    T-2 429 response      :s2, after s1, 20m
    T-3 config + docs     :s3, after s2, 15m
    T-4 wiring            :s4, after s3, 15m
    Reviews + gate        :s5, after s4, 15m

    section Assembly lines
    T-0 contract          :p0, 10:00, 15m
    T-1 limiter core      :p1, after p0, 25m
    T-2 429 (1st try)     :crit, p2, after p0, 20m
    T-2 repair            :p2b, after p2, 10m
    T-3 config + docs     :p3, after p0, 15m
    Review T-3 early      :p3r, after p3, 10m
    T-4 wiring            :p4, after p1, 15m
    Final reviews + gate  :p5, after p4, 10m
```

- **Today:** about **105 minutes**.
- **Assembly lines:** about **65 minutes**, and the gap grows with more independent tasks.

---

## 4. Many missions at once

Missions run in parallel too, each on its own integration branch. The orchestrator decides which to advance. The CLI only refuses to start a mission whose owned paths overlap an active mission's.

```mermaid
flowchart LR
    classDef m fill:#e7f5ff,stroke:#1c7ed6,color:#0b2e4f
    classDef q fill:#f1f3f5,stroke:#868e96,color:#343a40,stroke-dasharray:4 3
    classDef orch fill:#fff4e6,stroke:#e8590c,color:#5c2a00

    O{"🤖 Orchestrator<br/>picks what to advance next"}:::orch
    subgraph WS["Workspace"]
        M3["M-0003 rate limiting<br/>branch factory/M-0003<br/>3 lanes open"]:::m
        M4["M-0004 export CSV<br/>branch factory/M-0004<br/>1 lane open"]:::m
        M5["M-0005 auth refactor<br/>queued: overlaps src/auth/** with M-0003"]:::q
    end
    O --> M3 & M4
    O -. "starts when M-0003 releases src/auth/**" .-> M5
    M3 --> PR3(["PR"]) 
    M4 --> PR4(["PR"])
```

---

## 5. What the planner must do so lanes are possible

1. **Contract first.** A small T-0 fixes any shared interface, so later lanes build against it, not against each other.
2. **Disjoint ownership.** Tasks that could run together never share a file, and each owns its own tests.
3. **Shared hot spots in one wiring task.** Registries, `__init__.py`, lockfiles and dependency changes go in a single serial task.
4. **`## Lanes` in `plan.md`.** It lists the dependency graph, the critical path and the likely lanes. It guides the orchestrator; it doesn't bind it.

`accept-scope` checks the graph: no dependency cycles, every changed file owned, and no overlap between tasks that could run at the same time.

## 6. New CLI surface

All of these commands are deterministic, and only the orchestrator runs them.

| Command | Purpose |
|---|---|
| `mission ready` | Tasks that could start now, with why the others must wait |
| `mission lane open T-n` | Create the worktree and branch and write the brief. Refused when paths overlap a running lane |
| `verify --mission M --task T-n` | Checks in that lane's worktree; evidence bound to it |
| `mission integrate T-n` | Owned-paths-only merge, then a smoke check on the integration branch, then DONE |
| `mission task split T-n` | Split into narrower lanes with the same criteria (no scope change) |
| `mission lane close T-n` | Abandon a lane and remove its worktree; the task returns to TODO |

**Unchanged:** your approvals, the assessment, scope binding, and one gate on the integrated result. The rule "one writer per workspace" becomes "one writer per worktree", and mission records stay serialized by the CLI's state lock.
