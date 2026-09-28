# Local release verification

## 0.3.2 (local candidate, 2026-09-28)

Version 0.3.2 is built and verified locally on branch `feat/0.3.2-hardening`. It adds the WP1–WP6 hardening work packages, plus fixes for the gaps found by the independent gap assessment of 2026-09-28:

- **F-4:** agent configuration is protected at any depth, and product briefs are refused while it differs from the mission base.
- **F-5:** the sensitive defaults are broader, and skip/xfail markers and test-runner configuration changes raise the risk tier.
- **F-6:** mission inputs that hold an obvious secret are refused.
- **F-1:** the user records their own approvals through `mission approve`, and `owners.maintainer` is required.

This is local artifact readiness only. It makes no claim about publication, remote CI, a PR, a merge or deployment. The globally installed CLI was not replaced.

### Results

| Check | Result |
|---|---|
| `uv sync --locked`, and `uv lock --check --offline` for the root and runtime locks | OK |
| `uv run pytest -q` (Python 3.14.4) | 1231 passed, 427 subtests passed |
| `uv run ruff check .`, `uv run ruff format --check .` | Clean |
| `uv build --no-sources` | `software_factory-0.3.2-py3-none-any.whl` and `.tar.gz` |
| `uv run python scripts/release_smoke.py <wheel>` | All stages pass (see below) |
| Manual smoke on an isolated wheel install (`UV_TOOL_DIR` in a scratch directory, `init --profile claude`) | See below |

The release smoke script checks that the wheel's bytes match the source and that no Node runtime is available. Its stages covered:

- hydration, dispatch, profiles, preservation, links and reinstall
- upgrade no-op, relinquish, downgrade refusal, crash recovery, drift and reinstall
- the JEV toggle and offline plan validation (mocked provider)
- persistent auth: pinned lookup, preservation and logout
- the orchestrator agent export and pinned guard (live client behaviour not run)

The script's orchestrator-export check compared tool names by substring, so `TodoWrite`, which WP2 added, failed its "no Write" assertion. It now compares whole tool names and still rejects `Edit`, `Write`, `MultiEdit` and `NotebookEdit`.

The manual smoke run confirmed each of the following:

- `mission create` refuses a request containing a GitHub token.
- `mission decision` refuses a `scope` record and names `mission approve`.
- `mission approve` refuses without a terminal.
- `mission approve` refuses when the typed ID is wrong.
- `mission approve` records `D-MERGE-…` when the typed ID matches (the terminal was supplied through `script`).
- The gate reports an unset `owners.maintainer`.
- `.mcp.json` blocks `mission brief`.
- The installed guard denies `mission approve` and `mission ci-result` and allows `mission status`.

### Not verified in 0.3.2

- Live Claude Code, Codex and Copilot sessions, and hook firing in a real client.
- Remote CI, branch protection, publication.
- macOS/Windows (file-monitor behaviour is simulated only).
- Python 3.11 in this round.
- Live JEV quality.

[verification.json](verification.json) is still the 0.2.6 record; it was not regenerated for 0.3.2.

### Known limits of the 0.3.2 approval split

The interactive-terminal check is not authentication. An agent with an unrestricted shell can supply a pseudo-terminal, for example with `script`, as this smoke run did. The Claude orchestrator guard denies that, but Codex and Copilot sessions are instruction-only. Records stay local-unattested, and `mission.json` can still be hand-edited until a hash-chained event log exists. The enforcement map records both limits.

## 0.2.6 record (historical)

Version 0.2.6 is built, verified and installed locally as the uv-managed `software-factory` CLI. It fixes the defects found by the independent 0.2.0 review. Version 0.2.6 adopts TypeSafe's official guidance: bounded retries on transient provider errors within the deadline, a model-selection confidence gate (`jev.min_confidence`, default 0.6: below it the assignment is unresolved for a human), and a claim-review threshold (`jev.claim_accept_confidence`, default 0.8: below it a claim is `needs_review`). Version 0.2.5 corrected a stale JEV deadline comment in `routing.py` (no behaviour change; saved model plans must be regenerated because the routing implementation hash changed). Version 0.2.4 superseded 0.2.3 with an uninstall/reinstall fix and documentation corrected against the code. Version 0.2.1 was an internal candidate and was never released. This is local artifact readiness only; no remote release or operational mission-state claim is made.

### Results

- Python 3.14.4: 303 tests and 115 subtests passed.
- Python 3.11.16: 303 tests and 115 subtests passed.
- Ruff checks and formatting passed.
- The wheel and sdist build reproducibly; the main-tree build is byte-identical to the verified build.
- Isolated wheel consumer checks passed without Node available. These include:
  - upgrade no-op
  - relinquished generated exports
  - downgrade refusal
  - crash recovery
  - drift
  - reinstall
  - the JEV toggle with offline plan validation (synthetic provider responses)
- Upgrading from 0.2.0:
  - Repositories with no missions upgrade cleanly.
  - Missions whose records satisfy the new schemas upgrade cleanly.
  - 0.2.0 evidence without `sequence` is blocked with an actionable message; see `upgrading.md`.
- The integrated changes had an independent cross-review, re-verification of its fixes, and a verified closing round. No blocking issues remain.
- The global CLI was reinstalled from the 0.2.3 wheel. On a fresh repository with all three client profiles, `doctor` reported ok, with only the configure-checks and owner warnings.

### Changes in 0.2.4

- Uninstall keeps an edited factory section in `AGENTS.md`, `CLAUDE.md` or `copilot-instructions.md` and removes only its two marker lines, so a later `init` adds a fresh section.
- A conflicting `.codex/config.toml` key now gets an actionable refusal.
- Documentation now matches the code. It covers:
  - which commands run globally and which in the pinned runtime
  - the recovery journal
  - committing before verification
  - profile switches, which require maintenance
  - dependency downloads during staging
  - JEV limits and deadlines
  - semantic exit codes
  - handoff packet contents
  - the repair budget

### Changes since 0.2.0

Workflow gates:
- Non-record files under `.factory/missions/` are now candidate content and block the gate.
- Weakening a check definition requires an exception decision bound to the fingerprint.
- Filesystem-monitor event loss fails closed on Linux. Other platforms don't install the Linux-only hooks.
- Reviews are accepted only in REVIEWING or READY_PR, and blocking findings need ids and explicit resolutions.
- Terminal missions are immutable.
- Evidence records carry a `sequence`.
- A task whose checks fail after RUNNING must go back through RUNNING, which counts as a repair attempt; interrupted runs don't count.
- Product checks no longer receive `TYPESAFE_API_KEY`.

Installation and CLI:
- A deleted export is relinquished instead of blocking every command.
- Doctor reports a replaced entry-point skill as an error and a missing one as a warning.
- On POSIX, the CLI now replaces itself with the pinned runtime (`execv`), so signals reach it.
- Interrupted transactions are surfaced and refused until recovery.
- Downgrades are refused, and Git is an explicit requirement.
- Upgrades validate historical records against the new schemas and refuse symlinks under `.factory/missions`.
- Doctor reports runtime drift, version skew and the uninstalled state.

JEV routing:
- Only routing-relevant configuration invalidates a plan.
- Candidates are compact: about 46–56 fit the default request limit.
- Each request gets its own deadline, with a 120-second cap per plan.

Removed leftovers:
- the global `--json` compatibility flag
- `init --to`, now `upgrade`-only
- the unused `constitution_path` argument
- the duplicate `semantic --root` option
- the unused `JEV_ENDPOINT` constant
- the unused installed templates
- the `maintenance_kinds`, `hold_states` and `terminal_states` settings
- the `--trunk` transition option and `profile_changes`

Documentation now describes `READY_PR`, reviews and CI records as local and unattested.

### Records

Source identity: `e67186b8baafa9b91c01df025db333eca9b9ade669e91a8ae1a39cdc797e9992` (103 files; excludes `docs/architecture*`, written by another session and not reviewed here). Artifact hashes are in [SHA256SUMS](../dist/SHA256SUMS). Commands, known limitations, deferred items and previous identities are in [verification.json](verification.json).

The constitution (1.0.0) is unchanged.

### Not verified

- Live JEV quality and account access.
- Interactive native clients.
- macOS/Windows: file-monitor behaviour is simulated only.
- External CI and publication.

The unused `factory.json` keys `completion_target`, `parallel_writers` and `evidence_exclude` are deferred: removing them changes the data format.

New projects start with JEV OFF, claim assessment in shadow mode, and a failing product-check placeholder. See the [README](../README.md) for use and the [release process](release-process.md) for scope.
