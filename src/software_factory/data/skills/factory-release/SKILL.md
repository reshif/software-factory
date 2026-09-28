---
name: factory-release
description: Prepare and verify delivery evidence for an authorized factory mission using its configured existing deployment process. Does not provision a platform or grant release approval.
---

# Coordinate a configured release

## Inputs and preconditions

The constitution in AGENTS.md applies; read [the constitution](../../../.factory/CONSTITUTION.md) only if it is not in your context. Read the delivery configuration, mission decisions and [delivery runbook](../../../.factory/docs/runbooks/delivery-and-recovery.md). Delivery is disabled in the default profile. Do not improvise credentials, environment names or deployment commands.

## Procedure

Confirm the mission is MERGED: that requires a successful `ci-result` recorded from the work branch (not the trunk) for the committed candidate, a merge decision bound to its fingerprint and `merge_ref` naming a commit that is reachable from the trunk recorded by `ci-result` (a remote-tracking ref whenever a remote exists), is newer than the mission base and contains the candidate. Fetch the trunk first. Local refs and records are forgeable by anyone with shell access, and `ci-result` URLs/conclusions, decisions and reviews are caller-supplied and unchecked; branch protection and required remote CI that re-runs the checks itself remain the authoritative controls. Confirm the configured pipeline. Prepare [the release record](../../../.factory/templates/release.md) with artifact identity, changes, staging checks, decision reference, observation requirements and [recovery plan](../../../.factory/templates/recovery.md).

Inspect existing pipeline evidence and actual external state. Once enabled delivery has the required artifact/staging references, `uv run --locked --project .factory software-factory packet --mission ID --kind release` prepares `release-packet.md` without executing commands or overwriting the authored release plan. An agent-written record of approval is not authorization. Reuse explicit authorization already given for the exact action, and request a concrete decision only when missing. Before any external mutation, verify target, artifact and authorized scope.

Use the configured delivery system to stage or promote only when authorized and available. Bind release evidence to the same artifact and record actual references; DEPLOYING also requires `recovery_ref`. Record the observation as `observation: {ref, status}`. Only a healthy observation reaches DELIVERED; an unhealthy one leads to factory-recover. Local state transitions describe external progress; they do not execute or prove a deployment.

## Outputs and verification

Delivery and observation references, an accurate release record and a recovery path. Report DELIVERED only when all configured evidence exists. If this repository has no delivery integration, prepare a handoff and state that delivery is unconfigured.

## Failure behavior

On failed staging, stale artifact, missing decision or unavailable pipeline, stop dependent release actions and return to the orchestrator with evidence. Use factory-recover only for an authorized recovery operation; do not create a new deployment stack to bypass the missing integration.
