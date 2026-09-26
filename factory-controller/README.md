# factory-controller

The deterministic core of the software factory. It enforces the rules; agents only reason.

**Status:** Phase 1. These are pure-Python modules with in-memory stores, and the walkthroughs run as tests. Phase 2 adds the Postgres state store, GitHub App identities, the approval inbox, the sandbox runner and the LLM gateway, keeping the same semantics.

```text
src/factory/
├── globs.py                     # ** glob matching for path rules
├── policy/
│   ├── loader.py                # loads + validates factory-kit/policies (version pinning, never-relax)
│   ├── requirements.py          # gate requirements and strictest-wins ordering
│   └── action_classes.py        # AC1–AC8 classification of the ACTUAL diff (§6.1, §9.2, §13)
└── controller/
    ├── gate_resolver.py         # one gate table + precedence → H1/HM/H2 per change (§6.2)
    ├── coverage.py              # deterministic standing-mandate coverage, pre-run and on diff (§6.1)
    ├── approvals.py             # quorum, single-use consume (CAS + fencing), rollback invalidation (§6.3, §10)
    ├── mission_fsm.py           # mission state machine with timeouts and global events (§14.1)
    ├── intents.py               # write-ahead intents + receipts, crash reconciliation (§10)
    └── approval_budget.py       # reviewer capacity as the WIP limit (§6.5)
tests/
├── test_*.py                    # unit tests per module
└── walkthroughs/                # the 13 Phase 1 acceptance scenarios (§19.3)
```

## Run

```bash
uv sync
uv run pytest            # all tests
uv run pytest tests/walkthroughs -v
```

Policies are read from `../factory-kit` by default. Set `FACTORY_KIT_DIR` to test an alternative kit, for example a proposed policy change.

## Example

```python
from factory.policy import load_policy
from factory.policy.action_classes import FileChange, classify
from factory.controller.gate_resolver import resolve

policy = load_policy()
diff = [FileChange("src/auth/session.py", "modified", added_lines=12, removed_lines=3)]
c = classify(diff, policy.floor)                       # -> AC6 (protected path)
gates = resolve(policy, action_class=c.action_class, risk_profile="standard",
                protected_touched=bool(c.protected_touched))
print(gates.h1, gates.hm, gates.h2, gates.rules)       # 1+sec 1+sec 1 (...)
```

Section references (§) point to [docs/software-factory-final-draft.md](../docs/software-factory-final-draft.md).
