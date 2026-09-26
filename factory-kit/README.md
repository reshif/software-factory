# factory-kit

The factory's data layer: **policies** the controller enforces, **schemas** for every contract, and **examples**. Agents, skills and templates will be added in later phases.

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
│   └── evidence-bundle.schema.json
└── examples/                  # validated against the schemas in CI/tests
```

**Change control (kit gate, §13.3):**

| Change | Required approval |
|---|---|
| `policies/**` or evals | Two humans, including security |
| Anything else in this repo | Factory owner + one other |
| Any change at all | The controller tests must pass (`cd factory-controller && uv run pytest`) |

Agents can never edit this directory. It is AC8 in `floor.yaml`.

Section references (§) point to [docs/software-factory-final-draft.md](../docs/software-factory-final-draft.md).
