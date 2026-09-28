# Product setup, checks and evidence

Configure product commands in `factory.json` as argv arrays with repository-relative working directories. Commands launch through Python subprocesses without an implicit shell. Explicit shell commands remain product configuration. Setup steps execute in order before checks; all setup steps are mandatory. A failed, interrupted, timed-out or output-limited step stops setup and prevents a passing run.

Example fragment for a Python product:

```json
{
  "setup": [
    {"id": "dependencies", "command": ["uv", "sync", "--locked"], "cwd": ".", "timeout_seconds": 120}
  ],
  "checks": [
    {"id": "tests", "command": ["uv", "run", "--locked", "pytest"], "cwd": ".", "required": true, "timeout_seconds": 180}
  ]
}
```

Retain the other required configuration fields. The product can use any language or explicit toolchain; installation does not execute detected scripts. Configure product dependencies, services and credentials in the hosting environment. No product CI workflow is installed automatically. Setup must be repeatable because verification runs it again. Ignore generated dependencies and build outputs deliberately.

Full runs require at least one required check. Optional failures are reported without blocking successful required checks. A focused `checks --only ID` reports the selected check's success and identifies `scope: focused`; it cannot establish full-product CI evidence. Doctor inspects configuration, exports and the factory runtime without executing product setup, checking authentication or contacting an inference provider. Actual execution establishes whether product commands are available and work.

## Candidate identity

```sh
uv run --locked --project .factory software-factory checks
uv run --locked --project .factory software-factory checks --require-clean
uv run --locked --project .factory software-factory verify --mission M-0001 --revision R-1
uv run --locked --project .factory software-factory gate --mission M-0001
```

`--require-clean` rejects a dirty candidate before setup. `verify --revision` takes a unique evidence run label (R-1, R-2, …), not a Git revision; reusing a label fails. Ordinary local runs permit existing changes and fingerprint their bytes; the result's `revision` field records the starting HEAD. Only a stable clean run has `exact_revision: true`. Merge CI evidence must represent a complete, exact-commit run. These measurements are locally supplied evidence, not authenticated remote attestations; READY_PR only means this local evidence is consistent. For authoritative assurance, have branch-protected remote CI re-run `software-factory checks --require-clean` (or the product checks) itself rather than trusting a recorded `ci-result`.

Verification binds configuration, specification, constitution, source, governance, runtime and dependency bytes. Schema bytes are loaded from the selected project assets. Before/after snapshots and watchdog filesystem observation cover the check interval. Observation begins before configuration is read, and its final events are collected before evidence is published. Observed transient edits invalidate a run even if the bytes are restored. Ignored output files are excluded; ignored governance and nested instruction files still count.

Monitor failures, terminated observers and event-capacity exhaustion fail closed, including Linux inotify queue overflow, failed directory watches, and Linux runs where that loss detection cannot be installed. macOS FSEvents drop/rescan and Windows buffer overflow are not surfaced by watchdog 6 and remain an unverified limit. Observation is a local consistency mechanism, not an OS security sandbox or a guarantee against every adversarial filesystem race. Resolve reported monitoring uncertainty and rerun; a matching before/after hash alone does not clear it.

## Limits and private logs

Configured entry points default to a 1 MiB combined stdout/stderr hard limit per command. `limits.check_output_bytes` changes the default; `output_limit_bytes` changes an individual command's limit. Schema bounds are 1 KiB–64 MiB. Timeouts are capped by `limits.check_timeout_seconds`, including setup.

Mission verification streams output into exclusive private files under `.factory/local/runs/`, with separate setup logs. Evidence stores their hashes and the gate checks retained bytes. The direct `run_check` API separately allows a bounded in-memory preview. An output-limited command terminates with `truncated: true` and cannot pass. Timeouts and interrupts terminate the process group on POSIX. Inspect private logs before sharing; product command output may contain sensitive data.

Keep summaries and raw logs under ignored `.factory/local/`. Writing a report into candidate source creates a source change. CI can persist its JSON summary and deliberately select artifacts under the hosting service's retention policy.

## Risk tier

`software-factory mission risk --mission ID` reads the candidate against `base_commit` and returns `low` or `high` with reasons: sensitive or protected paths, check-definition changes, deleted or shrunk tests (policy `test_paths`), dependency manifests and lockfiles, CI workflow files, a diff larger than `limits.high_risk_lines` (default 400, excluding tests and lockfiles), or an exhausted repair budget. It changes nothing. A high tier adds acceptance and adversarial reviews to the gate.

## Repository and platform boundary

Evidence requires a real Git baseline, the repository root and regular candidate files. Tracked or visible untracked symlinks, submodules, unmerged entries, skip-worktree and assume-unchanged flags are rejected by candidate fingerprinting. Ignored dependency caches can contain links but must not conceal governance or product source.

Linux is validated by the package's release checks. macOS and Windows process, filesystem and native-client behavior require separate validation. In particular, do not assume Windows shell-script launching or Codex metadata pipe polling has been established by Linux tests.
