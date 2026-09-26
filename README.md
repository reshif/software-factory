# Software Factory

Design and implementation of an AI software factory. Autonomous agents do the engineering work, and humans only approve decisions.

## Repository

| Path | What it is |
|---|---|
| [docs/software-factory-final-draft.md](docs/software-factory-final-draft.md) | **The current design** (Revision 2): decision request, approval model, architecture, security, roadmap |
| [factory-kit/](factory-kit/README.md) | Policies (gate table, autonomy levels, security floor), JSON Schemas, examples |
| [factory-controller/](factory-controller/README.md) | Deterministic core in Python: gate resolution, coverage, single-use approvals, mission state machine, intents. Includes the **13 Phase 1 walkthroughs as tests**. |
| [phase0-experiment/](phase0-experiment/README.md) | 2-week go/no-go experiment kit to copy into a pilot repo |
| [docs/software-factory-critique.md](docs/software-factory-critique.md) | Three-model critique (Fable, Opus 5.5, Sonnet 5) that Revision 2 addresses |
| [docs/software-factory-research.md](docs/software-factory-research.md) | Research: 55 years of software-factory thinking, portfolio patterns, failures, economics |
| [docs/claude-software-factory.md](docs/claude-software-factory.md) | v3 portfolio/modular design; the reference for later phases |
| [docs/codex-software-factory.md](docs/codex-software-factory.md) | Alternative design from a Codex session |

## Quick start

```bash
cd factory-controller
uv sync
uv run pytest -v
```

## Status

| Phase | State |
|---|---|
| 0: Experiment | Kit ready; needs a pilot repo and named approvers |
| 1: Define | Design drafted; **policies, schemas and walkthroughs implemented** (11 of 13 automated; 2 are Phase 2 infrastructure checks) |
| 2: Prove the loop | Not started. Gated on the Phase 0 result. |
