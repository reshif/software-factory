---
name: factory-status
description: Inspect and report a software-factory mission's actual progress and evidence without changing files, mission state or starting work.
argument-hint: "Mission ID, for example M-0001"
default-prompt: "Use $factory-status to inspect the mission I identify and report progress without changing work."
---

# Report status without changing work

Use the supplied mission ID or an unambiguous current context. If several missions match, list their IDs and ask; do not silently select one. The constitution in AGENTS.md applies. Read `factory.json` and the mission's records.

This is read-only. Do not start or resume the workflow, create or transition a mission, edit files, render profiles, generate briefs, run verification or implementation, restart operations or write a report artifact. Use `software-factory mission status --mission ID`, `software-factory gate --mission ID`, `software-factory mission risk --mission ID` and read-only Git inspection. Trust current Git identity over a saved label.

Report from the gate's `lane`, `risk`, `required_reviews`, `criteria_trace` and `warnings`: lane and risk tier with reasons; each acceptance criterion with its mapped task, recorded evidence and latest review verdict; review kinds present versus required; open ambiguities; remaining work, active or unknown operations, blockers and the next useful action. Distinguish recorded state from current readiness (`live_gate`). Name stale fingerprints, missing logs, missing reviews and unperformed checks; say what cannot be determined without execution. Report any recorded model assignment and unknowns without refreshing catalogs or plans.

Answer in chat with relevant paths. Do not claim new verification, authenticated approval, remote CI or delivery. Remaining read-only is a task boundary, not a sandbox.
