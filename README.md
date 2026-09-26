# Software Factory

Design and implementation of an AI software factory. Autonomous agents do the engineering work, and humans only approve decisions: mission (H1), merge (HM), release (H2) and exceptions (HX).

## Repository

| Path | What it is |
|---|---|
| [docs/software-factory-final-draft.md](docs/software-factory-final-draft.md) | **The design** (Revision 2): approval model, architecture, security floor, roadmap. Code cites it as `§N`. |
| [docs/build/phase2-build-spec.md](docs/build/phase2-build-spec.md) | The build spec the modules were implemented against: ownership, per-module requirements, agent output formats |
| [factory-controller/](factory-controller/README.md) | **The factory itself** (Python): deterministic core, state store (memory/Postgres), GitHub App integration, Claude Agent SDK runtime, Docker sandbox, LiteLLM budgets, approval inbox, verification/release/telemetry, the mission pipeline, service, worker, `factory` CLI, demo, and docker-compose deployment |
| [factory-kit/](factory-kit/README.md) | Policies (gate table, autonomy levels, security floor), JSON Schemas, the Claude Code plugin (agents, skills, hooks), guidance renderer, golden-path product template, holdouts-repo template, reusable workflows |
| [phase0-experiment/](phase0-experiment/README.md) | Two-week go/no-go experiment kit to copy into a pilot repo |
| [docs/software-factory-critique.md](docs/software-factory-critique.md) · [docs/software-factory-research.md](docs/software-factory-research.md) · [docs/claude-software-factory.md](docs/claude-software-factory.md) · [docs/codex-software-factory.md](docs/codex-software-factory.md) | Critique, research and earlier designs |

## Quick start (local, no accounts needed)

```bash
cd factory-controller
uv sync
uv run pytest                          # full suite (Postgres/Docker tests skip unless available)
uv run factory demo --scenario all     # every end-to-end scenario on fakes, with readable timelines
uv run factory demo --serve            # run the service locally and approve in your browser
```

## Status

| Phase | State |
|---|---|
| 0: Experiment | Kit ready; needs a pilot repo and named approvers |
| 1: Define | Design, policies, schemas and all 13 walkthroughs implemented as tests |
| 2: Prove the loop | **Built**: full mission lifecycle, production adapters and deployment, verified by three red-team passes and an end-to-end review. Not yet run against real GitHub/Anthropic. |
| 3+ | Deferred per design §2.2 (parallel workers, Temporal, mutation testing, SBOM/SLSA, fleet lane, kit gate) |
