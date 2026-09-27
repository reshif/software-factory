# Mission handoff

Authored context for the next session. `software-factory packet --kind handoff` writes generated facts (branch, HEAD, working tree, blockers, gate reasons) to `handoff-packet.md` and never overwrites this file.

## Current position

Record mission ID, accepted outcome, current state, actual branch/HEAD and working-tree changes. Identify the selected vendor/profile and whether any operations are still running.

## Completed and verified

List completed tasks and their evidence references. Distinguish focused checks, captured verification and separate review. Describe relevant existing user changes to preserve.

## Decisions and blockers

Record material decisions with their references, unresolved findings and necessary external inputs. Include attempts already made and configured limits.

## Next action

Describe the next useful bounded task and its prerequisite. A resumed session must reconcile actual repository/external state before repeating actions. Read only the relevant context; a full transcript is unnecessary.
