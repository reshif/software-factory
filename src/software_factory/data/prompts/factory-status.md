---
name: factory-status
description: Inspect and report a software-factory mission's actual progress and evidence without changing files, mission state or starting work.
argument-hint: "Mission ID, for example M-0001"
default-prompt: "Use $factory-status to inspect the mission I identify and report progress without changing work."
---

# Report status without changing work

Use the supplied mission ID or an unambiguous current mission context. If several missions could match, list their IDs and ask the user to choose; do not silently select one. Resolve the repository root. The constitution in AGENTS.md applies; read `.factory/CONSTITUTION.md` only if it is not in your context. Read `factory.json`, the relevant mission record, specification, handoff, results, checks and reviews.

This is a read-only request. Do not call the start/resume workflow, create or transition a mission, edit files, render profiles, launch verification or implementation, restart operations, or write a status report artifact. Use read-only inspection and the state tool's status command as needed. Inspect current Git identity and available evidence rather than trusting a saved status label.

Summarize the accepted outcome, completed criteria supported by evidence, remaining work, active or unknown operations, blockers and the next useful action. Distinguish recorded state from current readiness. Identify stale candidate fingerprints, missing logs, missing independent review and unperformed checks when observable; say what could not be determined without execution.

Return the status in chat with relevant existing paths. Do not claim a new successful verification, authenticated approval, remote CI result or delivery. The instruction to remain read-only is a task boundary, not a new runtime sandbox or permission setting.

Report any recorded model assignment, requested/observed identity and unknowns. Do not refresh catalogs, create model plans or change selection during this read-only status request.
