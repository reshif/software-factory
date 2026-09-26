# Software Factory: project guide

A monorepo for designing and building an AI software factory. Autonomous agents do the engineering work, and humans approve decisions. The source of truth for behavior is `docs/software-factory-final-draft.md` (Revision 2). Code and docs reference its sections as `§N`.

## Layout

- `docs/`: design docs. The final draft is authoritative; the others are research, critique and earlier designs.
- `factory-kit/`: policies (gate table, autonomy levels, security floor), JSON Schemas, examples. Data only.
- `factory-controller/`: Python package, the deterministic core (gate resolution, coverage, approvals, state machine, intents).
- `phase0-experiment/`: a kit to copy into a pilot repo for the 2-week go/no-go experiment. Not run from here.

## Commands

```bash
cd factory-controller && uv sync && uv run pytest     # must pass before any commit
```

## Rules

- **Policy changes are data changes.** Edit `factory-kit/policies/*.yaml`, never hard-code gate values in Python. Changing policies requires bumping `policy_version` in all three policy files. It also needs a note in the PR explaining the safety impact.
- **Strictest wins.** Nothing may loosen a gate below the table. Overrides only tighten, and relaxations never touch AC5–AC8 or protected paths.
- **Fail closed.** Missing or unknown evidence is a failure; a timeout never approves.
- Keep the walkthrough tests in `factory-controller/tests/walkthroughs/` in sync with §19.3 of the final draft.
- When behavior changes, update the final draft section it implements in the same commit.
- Python ≥ 3.11, standard library + PyYAML only in `src/`. Test-only deps go in the `dev` group.
