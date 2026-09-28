# Set up the Python factory

Requires Python 3.11+, uv, Git (initialization and missions), and the selected authenticated native client. Node is not a factory dependency.

Install the release wheel with `uv tool install /path/to/software_factory-<version>-py3-none-any.whl`. Then:

```sh
software-factory init /path/to/project --profile claude --dry-run
software-factory init /path/to/project --profile claude
cd /path/to/project
software-factory doctor
```

Choose `codex`, `copilot` or a comma-separated profile list as needed. Existing files and user instruction text are preserved. `--allow-dirty` explicitly allows adoption alongside existing changes. `--skip-sync` installs files without creating the Python environment; hydrate later with `uv sync --locked --no-dev --project .factory`.

Replace the failing configure-me check in factory.json with actual required product commands. Commands are argv arrays with relative working directories; detection only suggests commands. The factory does not run shell expansion. Keep a required check until meaningful tests are configured. After any `factory.json` edit (checks, enforcement, limits and other settings), run `software-factory render`: `factory.lock.json` pins the `factory.json` hash, so `doctor`, `render --check` and the gate report stale exports until it is refreshed. Setup commands (init, upgrade, uninstall, recover, render, auth) and `factory.json` edits are run by a human, not by the orchestrator. Three `factory.json` values are informational: nothing reads `completion_target` (the gate always evaluates READY_PR), `limits.parallel_writers` must stay 1 (one writer is always enforced), and `evidence_exclude` accepts only the built-in metadata exclusions: other entries are rejected, and removing entries has no effect. Set `owners.maintainer` and `owners.reviewer`; without a maintainer, self-review is not detected, so use a separate reviewer context.

Initialization requires Git. For a plain folder, `init --git-init` creates Git metadata; it never commits. Review and commit through normal project workflow before mission execution or JEV assessment.

The installed project owns an exact local Python runtime and uv.lock under .factory. Normal commands dispatch there. Teammates (and each additional worktree) run `uv sync --locked --no-dev --project .factory`, then `uv run --locked --project .factory software-factory doctor`; no global install is required.

Use the native factory-blueprint/build/resume/status prompt. Or inspect deterministic commands with `software-factory mission --help`, `software-factory verify --help` and `software-factory gate --help`. Status is read-only. A build stores the verbatim request (`mission create --request-file`), gathers context, asks ambiguities up front, records AC-n criteria and an architecture diagram, then implements from generated briefs; verification and the review kinds required by the lane (`--kind patch` is the small lane) and `mission risk` come before READY_PR. READY_PR means local evidence is consistent, not attested; remote PR, CI, merge and delivery require their own actual evidence.

New installations use `model_selection.mode: "inherit"`, so startup records one inherited line instead of planning models. Optional keys: `limits.high_risk_lines` (default 400) sets the diff size that makes `mission risk` high, and `enforcement.claude_orchestrator_agent` (default false) exports the opt-in Claude orchestrator agent and guard hook described in [vendor behavior](vendor-behavior.md#enforcement-per-client). JEV ships OFF, with claim assessment in shadow mode. See [model selection](../jev-routing.md) and [claim assessment](semantic-assistance.md). Installation and diagnostics never make inference requests.
