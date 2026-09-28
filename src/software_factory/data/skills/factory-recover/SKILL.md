---
name: factory-recover
description: Coordinate an authorized recovery for a factory-delivered change using its recorded trigger, existing pipeline and code/data recovery plan.
---

# Recover an observed delivery failure

## Inputs and preconditions

The constitution in AGENTS.md applies; read [the constitution](../../../.factory/CONSTITUTION.md) only if it is not in your context. Read the release/artifact identity, observed failure, recovery decision and [delivery/recovery runbook](../../../.factory/docs/runbooks/delivery-and-recovery.md). Identify current external state before repeating any operation.

## Procedure

Record `incident_ref` and a `recovery` decision (reference equal to `recovery_ref`; `subject_hash` is the 64 hex characters of `artifact_digest` without its `sha256:` prefix), then transition DEPLOYING or OBSERVING to RECOVERING. Compare the observation to the recorded recovery trigger. Determine whether the approved action covers code rollback, flag disablement, data repair or a forward fix. Do not assume code rollback reverses a migration.

Use [the recovery template](../../../.factory/templates/recovery.md) to capture the exact operation, target, consequences and verification. Once enabled delivery has an artifact and recovery reference, `uv run --locked --project .factory software-factory packet --mission ID --kind recovery` prepares a separate `recovery-packet.md`; it does not run recovery. Execute only the existing configured action within the actual authorization; a standing recovery decision may already supply it. If new destructive data work or expanded access is required, present the concrete decision before dependent action.

Verify recovery in the target environment using the configured health and user-flow checks. Record operation IDs, actual outcome, remaining risks and necessary repair follow-up. RECOVERED requires a healthy `recovery_observation` and a `follow_up_mission` ID; if recovery is not verified, `software-factory mission block` with the reason. Preserve failure evidence before cleaning temporary artifacts.

## Outputs and verification

An accurate recovery record with external evidence and next work. Clearly distinguish attempted recovery from verified recovery. Do not mark the original feature DELIVERED merely because a rollback restored prior service.

## Failure behavior

If the recovery command, decision or external state is unknown, stop dependent mutation, preserve the current situation and report the smallest needed input. Do not invent credentials, issue blind repeated deployments or silently run destructive migrations.
