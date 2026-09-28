# Local release process

The authorized outcome is a private, reusable Python/uv package built in this directory, including JEV and the existing factory workflow. The original Node checkout is the reference and remains preserved. This port does not publish to a registry, create a PR, or claim deployment.

## Architecture and acceptance

The global `software-factory` command installs or upgrades a curated payload. Each product receives a local Python kernel, `pyproject.toml`, `uv.lock`, canonical assets and ownership manifests under `.factory/`. Normal operations dispatch to that pinned kernel before parsing project command arguments. This lets repositories retain their own reviewed versions.

The installer uses preimage checks, staged dependency setup and a private transaction journal. Shared instruction sections and TOML keys have explicit ownership; product files and changed user content are preserved. Task results have one indexed storage format with explicit execution attempts. The package has one canonical constitution location and no conversion command.

The native Python modules implement the workflow, process checks, candidate/dependency fingerprints, state/result/evidence gates, model metadata planning and optional JEV transport. They do not invoke Node. The JEV toggle chooses the model selector: enabled uses JEV, disabled uses factory-models. The independent claim helper uses claim_mode, initially shadow. JEV cannot approve task results or satisfy readiness.

The distribution is a wheel and source archive; runtime management uses Python and uv. The local matrix covers Linux and Python 3.11/3.14, all seven generated profile combinations, and mocked JEV. Interactive client discovery, other operating systems, paid inference/quality calibration, registry publication and remote CI are separately unverified. In particular, Codex metadata discovery uses POSIX pipe polling.

## Required release checks

1. `uv sync --locked`, `uv run pytest -q`, and `uv run ruff check .`.
2. Run the test suite in the declared minimum Python version using a separate environment.
3. `uv build --no-sources`. This builds the wheel from its source distribution, catching missing source assets.
4. `uv run python scripts/release_smoke.py dist/software_factory-<version>-py3-none-any.whl`. The isolated wheel consumer tests hydration, all selected exports, documentation closure, no-Node operation, project-file preservation, the JEV toggle and offline plan validation with a mocked provider, pinned dispatch, missing-runtime refusal, upgrade, downgrade refusal, interrupted-operation `recover`, deleted-export re-render and uninstall/reinstall.
5. Inspect the sdist for tests, validation scripts and the lockfile. Inspect wheel contents for private state, development caches and Node executables.
6. Obtain independent review of the actual runtime and packaging controls. Resolve findings with regression tests. Save exact artifact hashes and a final verification record outside the archive, then install the verified wheel using `uv tool install`.

The build backend's explicit source inclusion keeps tests/scripts in the sdist and runtime data in the wheel. See [uv build-backend documentation](https://docs.astral.sh/uv/concepts/build-backend/) and [source-include configuration](https://docs.astral.sh/uv/reference/settings/#source-include), checked 2026-09-27.

## Version changes

Update `pyproject.toml`, `src/software_factory/__init__.py`, and the runtime template at `src/software_factory/data/runtime/pyproject.toml` together. Resolve `uv.lock` and the runtime template lock independently; the runtime template has no developer dependency group. Repeat all release checks. A project's pinned kernel changes only through an explicit `upgrade`, and `upgrade` refuses a downgrade unless `--allow-downgrade` is given.

The current native package schema rejects removed compatibility/configuration formats. Keep JEV model/rubric/schema changes explicit and independently reviewed. Do not treat successful mocked responses as live quality or account validation. Leave new projects OFF until the operator deliberately configures authorized use.

## 0.3.0 additions

Also confirm, in the disposable-project smoke run, that every relative Markdown link in exported skills and `.factory/docs` resolves, that no exported specialist has an agent tool, and that the opt-in Claude orchestrator agent has no Edit/Write. Unit tests exercise `orchestrator_guard.py` offline; record the Claude hook behavior as not run until observed in a live client. No Copilot hooks ship in 0.3.0. Check that 0.2.x mission history still validates on `upgrade --dry-run`.

## 0.3.1 additions

The release ships constitution 2.0.0 (MAJOR; see docs/implementation.md). In the disposable-project smoke run, also confirm that the managed AGENTS.md section carries the 2.0.0 text, that `.factory/docs/runbooks/constitution-enforcement.md` is installed and its links resolve, and that no installed role, skill, prompt or runbook cites a rule by number. Upgrade a project holding a 0.3.0 pre-merge mission: `upgrade --dry-run` must list it under `missions_needing_constitution_reconcile` without blocking, and the reconciliation in the runbook must return it to PLANNED. Keep the enforcement map in step with any gate or guard change.

## Review decisions in this port

Independent reviewers covered installation/ownership/transactions, JEV, models/calibration, and workflow/evidence/checks. Repairs bind operations to early preimages, protect journals before writes, reject path/ownership aliases, preserve shared text, validate required assets, bound model discovery writes, collect final filesystem-monitor events, discover ignored governance files on each snapshot, and observe configuration before running checks. These strengthen consistency checks without adding permissions or changing the constitution.

The runtime can detect observed changes and bind local evidence to actual bytes. It is not an OS security sandbox, authenticated approval service or immutable audit store. The selected coding client's permissions and the user's authorization remain authoritative.
