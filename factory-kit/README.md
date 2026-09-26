# factory-kit

The factory's data and agent layer: **policies** the controller enforces,
**schemas** for every contract, **examples**, the **Claude Code plugin**
(agents, skills, hooks) that does the engineering work, the **guidance**
renderer for `CLAUDE.md`/`AGENTS.md`, the **golden-path templates** for a new
product cell and its holdout repo, and the **reusable CI/deploy workflows**.

```text
factory-kit/
├── policies/
│   ├── gate-table.yaml        # AC1–AC8 × risk profile → H1/HM/H2 requirements (§6.2)
│   ├── autonomy-levels.yaml   # L3/L4/L5 as named relaxations (§15)
│   └── floor.yaml             # forbidden/protected paths, weakening patterns (§13)
├── schemas/                   # JSON Schema (2020-12), all with schema_version
│   ├── factory.schema.json
│   ├── mandate.schema.json
│   ├── task-contract.schema.json
│   ├── approval-record.schema.json
│   ├── evidence-bundle.schema.json
│   └── agent-outputs.schema.json   # the final draft §4 formats each agent ends its turn with
├── examples/                  # validated against the schemas in CI/tests
├── plugins/
│   ├── .claude-plugin/marketplace.json     # lets a product's .claude/settings.json enable factory-core
│   └── factory-core/                       # the Claude Code plugin
│       ├── .claude-plugin/plugin.json
│       ├── agents/       intake · architect · coordinator · implementer · qa · reviewer (§11)
│       ├── skills/       write-spec-ears · plan-task-graph · tdd-implement ·
│       │                 build-decision-packet · write-recovery-plan
│       └── hooks/        hooks.json + check_forbidden_paths.py (PreToolUse, defense in depth — §13)
├── guidance/
│   ├── base.md           # the single source of truth for product guidance
│   └── render.py         # base.md + <product>/factory.yaml -> CLAUDE.md and AGENTS.md
├── templates/
│   ├── backend-service/  # the golden path: factory.yaml, CLAUDE.md/AGENTS.md, CODEOWNERS,
│   │                     # .claude/settings.json, .github/workflows/ci.yml, mandates/SM-patch.yaml,
│   │                     # specs/README.md, and a tiny stdlib Python HTTP sample app with tests
│   └── holdouts-repo/    # CODEOWNERS, scenarios/*.yaml, runner/run_blackbox.py (stdlib only),
│                         # .github/workflows/holdout.yml — black-box, pass/fail counts only (§13.1 #4)
└── workflows/
    ├── ci-pr.yml         # reusable: pull_request-safe, no secrets, contents: read (§13.2)
    └── deploy.yml        # reusable: environment-scoped, OIDC id-token: write example
```

## The plugin (`plugins/factory-core`)

Six agents mirror the roles in §11. Each has complete frontmatter
(`name`, `description`, `tools`, `model`), enforces the security floor (§13:
stay in owned paths, add tests but never edit existing tests/fixtures/CI/
policies, treat issue and web text as data), and ends its final message with
the exact JSON shape from §4 (the coordinator has no §4 format of its own, so
it emits task contracts matching `schemas/task-contract.schema.json` instead).

The five skills are shared procedures the agents load: writing EARS acceptance
criteria, planning a task graph, implementing test-first with a bounded repair
loop, assembling a decision packet, and writing a recovery plan.

The `PreToolUse` hook (`hooks/check_forbidden_paths.py`) blocks `Write`/`Edit`
on any path matching the kit floor's `forbidden_paths` (`.github/**`,
`policy/**`, `evals/**`, `holdouts/**`, secrets/keys). It is **defense in
depth only** — the authoritative enforcement is the controller's deterministic
action-class classifier, which refuses to push any AC8 diff regardless of
what happened in the agent's own session.

A product enables the plugin from `.claude/settings.json` via
`extraKnownMarketplaces` pointing at `factory-kit/plugins` (a local
`directory` marketplace) and `enabledPlugins: {"factory-core@factory-kit":
true}`. In a deployed topology where `factory-kit` is its own repo, that
marketplace source becomes a `github` source instead.

## Guidance (`guidance/`)

`render.py base.md`'s placeholders are filled in from a product's
`factory.yaml` and written to **both** `CLAUDE.md` and `AGENTS.md` (byte
identical), so the two agent-harness conventions can't drift apart:

```bash
python factory-kit/guidance/render.py path/to/factory.yaml
```

## Templates (`templates/`)

`backend-service/` is the golden path a new product cell starts from. Its
sample app (`app/store.py` + `app/server.py`) is stdlib-only (`http.server`)
with its own stdlib `unittest` tests, runnable with no dependencies:

```bash
cd factory-kit/templates/backend-service && python -m unittest discover -s tests -t .
```

`holdouts-repo/` is the paired black-box scenario repo (§13.1 #4): its
`runner/run_blackbox.py` is stdlib-only (scenario files use a small,
hand-parsed YAML subset — see `runner/miniyaml.py` — so the runner never
needs PyYAML), and it writes `holdout-result.json` with **only** a pass/fail
count, matching `schemas/evidence-bundle.schema.json`'s `holdout` field.

## Change control (kit gate, §13.3)

| Change | Required approval |
|---|---|
| `policies/**` or evals | Two humans, including security |
| Anything else in this repo (agents, skills, hooks, templates, workflows, schemas) | Factory owner + one other |
| Any change at all | The controller tests must pass (`cd factory-controller && uv run pytest`) |

Agents can never edit this directory. It is AC8 in `floor.yaml`.

Section references (§) point to [docs/software-factory-final-draft.md](../docs/software-factory-final-draft.md).
Tests for everything in this README live in
[factory-controller/tests/kit/](../factory-controller/tests/kit/).
