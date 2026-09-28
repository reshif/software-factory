# Resume a mission or change vendor

Mission records survive a client session; the factory does not keep executing after the session stops. One active orchestrator owns a mission at a time. Switching the profile does not transfer a live conversation or start the new vendor automatically.

## Before a planned stop

Let active commands finish or cancel them through the actual client, and record their outcome. Do not leave an unknown writer running. Inspect Git status, update the mission with actual progress and create a handoff:

```sh
uv run --locked --project .factory software-factory mission status --mission M-0001
uv run --locked --project .factory software-factory packet --mission M-0001 --kind handoff
```

Include uncommitted product/user work, active operations, actual revision, remaining findings and the next useful action. Record PAUSED only when the user actually requests a pause; an unavailable dependency is BLOCKED. A handoff alone does not require either transition if work will continue in another client.

## Switch profiles

Inspect generated-file drift and preserve any deliberate edits through the maintenance process. Render the new active profile with `uv run --locked --project .factory software-factory render --profile copilot` (or `claude`/`codex`, or a comma-separated list such as `claude,codex,copilot` to keep several clients usable in the same workspace), then run doctor. The renderer owns only its manifested files; never delete entire `.claude`, `.codex` or `.github` directories as cleanup.

A profile switch changes `factory.json` and rendered instruction files, which are protected factory paths, so it needs a maintenance mission; the gate does not accept a profile switch inside a product mission. These paths remain in the candidate fingerprint, so the switch requires fresh checks, results and review. For any mission kind, removing a baseline-required or task-named check, changing its command or cwd, or making it optional requires an `exception` decision bound to the current fingerprint, which the user records with `software-factory mission approve --mission ID --kind exception --reference TEXT`; the PR packet lists these check-definition changes. Install and commit a consistent factory before starting product missions.

Open a fresh conversation in the selected client/harness. Confirm constitution and skill discovery using [the smoke tests](../vendor-smoke-tests.md). Provide the mission ID and ask it to use `factory-start` and read the handoff. Do not assume conversation history, a previous agent ID or permissions transfer between products.

The [resume prompt](prompts.md#resume-or-switch-clients) supplies these instructions consistently for Claude, Codex and Copilot. Use the existing mission ID after switching; a vendor change is not a new product request.

## Reconcile before resuming

Inspect the actual branch, HEAD, working-tree changes, task status and available evidence. Compare them to the saved mission. Identify unfinished or duplicated work before writing. Check actual remote state before repeating a push, merge, deployment or migration.

Re-run checks after changes to relevant candidate content, specification or factory controls. A prior passing report cannot be copied forward to a different fingerprint. Re-examine the affected code in a separate reviewer context after changes.

Captured evidence references ignored local logs. A resumed session on the same machine can inspect matching logs; another machine without them must rerun verification. Do not commit raw logs or copy sensitive transcripts merely to preserve a prior pass. Task result fingerprints and independent review must match the final candidate as well.

For PAUSED/BLOCKED work use the state tool's `resume --mission ID --to STATE` only after its cause is resolved and prerequisites are reconciled. Do not use resume to skip verification or an approval-dependent external stage.

Resume normally names the recorded previous state. Resuming READY_PR reruns the gate before restoring readiness. If pre-merge evidence is stale, explicitly resume to IMPLEMENTING, reopen affected DONE tasks to RUNNING and repair within their original attempt budgets. Reopening a task resets its transitive dependents to TODO. If accepted scope changed while paused or blocked, record the user's words with `mission clarify` (and new criteria with `mission criteria` when they change). On a held mission these commands keep the hold and its blockers: they reset tasks and scope as usual and set the state to return to (`previous_state`) to PROPOSED. A hold ends only through `mission resume --mission ID --to PROPOSED --resolution TEXT`, or through `accept-scope` when every open blocker is the constitution-change block (the [reconcile path](constitution-enforcement.md#reconciling-in-flight-missions)). After resuming, record the user's actual authorization and run `accept-scope`; this returns pre-merge work to PLANNED for revalidation. Briefs under `.factory/local/briefs/` are regenerated from records, so a new session runs `mission brief` again rather than reusing an old brief. Ordinary verification refuses to start while the mission is PAUSED/BLOCKED; the explicit post-merge reconciliation path below can refresh checks while keeping the hold in place. State changes do not automatically interrupt commands already running in a vendor terminal; cancel those through the actual client before recording the handoff.

New PAUSED records save the active task's RUNNING/VERIFYING phase. Resume restores that phase without spending a repair attempt, after checking that another mission has not taken the workspace writer slot. It does not restart an external command. Tasks that were already BLOCKED remain blocked; an explicit retry through RUNNING still spends an attempt. Scope reconciliation clears saved phases. Older PAUSED records without `suspended_tasks` cannot prove their former phase and retain the existing BLOCKED/retry behavior; the tool does not infer a phase from missing history.

## Restore post-merge verification

New CI recordings pin the accepted verification reference and its content hash. Completed task results keep that original link. A later full check run for the exact CI candidate supplies current logs and health: a passing rerun can restore readiness, while the latest failed run blocks forward delivery. Never rewrite completed results or accepted evidence to point at a new run.

When a PAUSED/BLOCKED post-merge mission has lost local logs, keep it held during reconciliation. A human runs these commands: the Claude orchestrator guard denies `--root` and `verify --candidate-root`. Inspect `delivery.ci_ref.head_sha`, the recorded configuration and mission contracts, then create a separate checkout of that exact commit. For example, replace the placeholders with inspected values:

```sh
git worktree add --detach /tmp/factory-ci-checkout CI_HEAD_SHA
uv run --locked --project .factory software-factory verify --root . --mission M-0001 --revision R-RECONCILE-001 --candidate-root /tmp/factory-ci-checkout --reconcile-postmerge --resolution "Re-execute accepted candidate checks after local log loss"
uv run --locked --project .factory software-factory mission status --mission M-0001
```

The command uses the original mission's specification and model contracts, runs the candidate checkout's configured setup and checks, and writes fresh evidence/logs under the original root. The candidate must reproduce the recorded fingerprint, including original governing inputs. Install the required runtime first; do not alter candidate configuration to get a pass. Keep both workspaces free of other writers during verification. A different post-merge HEAD is rejected before registering a run.

`--reconcile-postmerge` requires a held post-merge mission and concrete `--resolution`; it does not resume the mission or execute delivery. Once the cause is resolved and existing user authorization permits continuation, explicitly use `software-factory mission resume --mission M-0001 --to MERGED --resolution "Fresh CI-candidate checks restored"`, substituting the actual recorded previous state. For an active post-merge mission, use `--candidate-root` without the reconciliation flag. Missing or altered accepted evidence itself remains a blocker; fresh logs cannot replace its provenance. Candidate fingerprints normalize file permissions to Git's executable/non-executable modes, so checkout umasks do not change candidate identity.

## Interrupted state updates

State writes use local locking and atomic replacement. If a lock error remains after a crash, inspect whether the recorded owning process is still alive before recovery. `uv run --locked --project .factory software-factory mission recover-lock` rejects a live owner and recovers only when the recorded process is absent on this host. Do not delete active locks to run a second writer.

Rendering uses the same journaled file transactions as `init` and `upgrade`. If a hard interruption leaves the recovery journal (`doctor` reports `interrupted_transaction`), stop dependent work and confirm the recorded process has exited. Preview with `software-factory recover`, then run `software-factory recover --apply`; it restores only writes whose current bytes still match the operation. There is no force-overwrite recovery command; do not remove the lock while its writer is alive. Run `software-factory render --check` and doctor after recovery.

File-based coordination is not distributed scheduling or exactly-once external execution. A human/session must reconcile uncertain remote operations. Keep that uncertainty explicit in the handoff until resolved.
