# Scaling factory work

Use one root installation per monorepo, with check-specific relative working directories. The factory is stack-independent: configure Python, JavaScript, Go, Rust or other product commands explicitly.

One orchestrator owns a mission and one writer owns a workspace. Parallel code writers need isolated workspaces, disjoint contracts and an integration owner. Worktrees separate edits; they are not security sandboxes. State locks live in each worktree's ignored `.factory/local/` and fail fast without waiting, so they do not coordinate across worktrees: two worktrees can create the same mission ID, and keeping one orchestrator per mission across worktrees is operator discipline. Each worktree needs its own `uv sync --locked --no-dev --project .factory`. Pass specs, constitutional identity, revision, allowed paths, check IDs and result format to each specialist.

Persist task results through `software-factory mission record-result` or `record-results`. Reconcile handoff/state/Git after interruption. Attempt limits and unresolved findings remain in force. Registry extensions are reviewed factory-maintenance changes and need schemas, generation tests and native-client smoke checks.
