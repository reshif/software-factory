---
name: write-recovery-plan
description: Write or check a recovery plan covering both code and data impact, for a mission mandate, an H2 release packet, or a mid-flight incident. Use whenever a plan needs a concrete answer to "how do we undo this" before it can be approved.
---

# Write a recovery plan

Every mandate requires a recovery plan (final draft §6.1), and every release
packet needs one an approver can act on under pressure (§10). A recovery plan
that says "we'll figure it out" is not one.

## Answer these, concretely

1. **How does the code revert?** Name the mechanism: revert the merge commit,
   disable the feature flag (name it), or roll back to the prior digest. Say
   which one, not "as needed."
2. **What is the data impact?** One of:
   - `none` — no schema or stored-data change.
   - A described, reversible change — e.g. an additive migration with a
     forward-fix plan (AC5, final draft §6.2), where the rollback is "stop
     writing the new column; nothing to undo."
   - A described, **irreversible** change — call this out loudly; it changes
     the action class to AC7 and needs an HX exception, not a quiet mention.
3. **What triggers using the plan?** For a release: the observation window
   (final draft §6.4) going unhealthy. For a mission: verification exhausting
   its repair budget. Name the actual signal, not "if something goes wrong."
4. **Who or what executes it, and does it need approval?** Killing a flag or
   running an **already-approved** recovery plan never needs a new human
   approval (§6.4) — but going outside that approved plan does, and routes to
   HX.
5. **How long does recovery take?** A rough number the approver can compare
   against the observation window.

## Checklist

- [ ] The plan names a mechanism, not an intention.
- [ ] Data impact is explicitly `none` or explicitly described — never
      omitted.
- [ ] If data impact is irreversible, the plan says so and flags AC7/HX
      rather than downplaying it.
- [ ] The trigger for using the plan is a concrete, observable signal.
- [ ] Recovery time is stated, even as a rough estimate.

## Where it's used

- `recovery_plan` in the architect's H1 output (final draft §4).
- `recovery: {plan, data_impact}` in a mission mandate
  (`factory-kit/schemas/mandate.schema.json`).
- The "Recovery" section of every decision packet (`build-decision-packet`).
