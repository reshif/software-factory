# Optional Jev semantic assistance

The factory provides a shared Python 3.11+ helper and canonical `factory-semantic` skill for Claude Code, native Codex and GitHub Copilot in VS Code. It assesses claims against explicitly supplied excerpts. The main orchestrator remains responsible for investigation, implementation, verification and independent review.

Jev is OFF by default. The initial mode is shadow: judgments are saved privately and withheld from command output. No performance, model accuracy, account access or live client-discovery claim follows from installing this feature.

## Decisions and authority

One Choice question per eligible claim returns one relationship:

| Relationship | Meaning | Follow-up |
| --- | --- | --- |
| supports | The supplied excerpts support the entire material claim under matching conditions | Keep sources and ordinary review |
| contradicts | Clear conflict under matching conditions | Investigate the claim and source interpretation |
| not_addressed | Readable excerpts do not address the claim | Find evidence or qualify the claim |
| mixed | Clear conflicting evidence or materially partial support for a compound claim | Split the claim or resolve the conflict |
| insufficient_context | Ambiguity or missing conditions prevent useful assessment | Obtain context or keep unresolved |

Confidence is a 0–1 statistic of the returned answer distribution, retained separately from the relationship. Following TypeSafe's citation-check guidance, each evaluated claim also gets an advisory review state: `accepted` when confidence is at least `jev.claim_accept_confidence` (default 0.8), otherwise `needs_review`. `needs_review` means a human must confirm the relationship against the sources before relying on it; the relationship and confidence stay visible in advisory output. `accepted` only means no extra confirmation is suggested by the confidence rule: it is not truth, verification or approval, and neither state ever satisfies or blocks a gate, changes exit codes or alters the overall status. Unresolved, unavailable and invalid claims have no review state. The threshold is not a measured calibration for your material. A relationship describes the supplied material, not global truth or account-specific availability. Transport failures never become semantic answers. A missing quote means not found in this representation, not automatically fabricated.

Claim/source assessments support model research, planning research and review preparation. Model selection also uses JEV when enabled; see [model routing](../jev-routing.md). Check-failure classification is the separate advisory `software-factory triage` command described in [check triage](check-triage.md). Context ranking, skill routing and task-intent suggestions remain deferred; enabling Jev does not enable those operations.

The constitution and host/user authority apply throughout. Jev cannot apply native model assignments, execute repairs, reset attempts, approve scope, satisfy tests/review, transition a mission, merge or deploy.

## Configuration and toggle

Add or edit this optional block in `factory.json`:

```json
{
  "jev": {
    "enabled": false,
    "provider": "typesafe",
    "model": "jev-1.13.0",
    "claim_mode": "shadow"
  }
}
```

Omission also means OFF. Set `enabled` to `true` to use JEV for model selection and permit explicit claim assessments; set it back to `false` to disable. Keep `claim_mode: "shadow"` for evaluation. Use `claim_mode: "advisory"` only when the local evaluation justifies visible suggestions. The helper does not certify that evaluation has happened or grant permission to transmit material.

After changing configuration or canonical sources, regenerate the active profile with `uv run --locked --project .factory software-factory render`. Configuration changes affect candidate fingerprints and invalidate affected evidence. Do not edit generated skills. All three profiles export the same canonical helper instructions and rubric; multiple-profile discovery follows the existing renderer rules.

| Setting | Default | Enforced bound or behavior |
| --- | --- | --- |
| deadline_ms | 3000 | 100–30000; overall HTTP request and body deadline |
| max_pairs | 16 | 1–32 supplied claims per invocation |
| max_request_bytes | 24576 | 1024–24576 UTF-8 serialized request bytes |
| max_response_bytes | 65536 | 1024–262144 response bytes |
| max_source_age_hours | 168 | 1–8760 from recorded retrieval time |
| cache | true | Local applicable results only |
| cache_ttl_seconds | 900 | 1–86400; source freshness independently checked |
| claim_accept_confidence | 0.8 | 0–1; confidence at or above it is `accepted`, below it `needs_review` (advisory) |

This table applies to `semantic check`; model planning has its own [per-plan limits](../jev-routing.md). These are conservative initial operational limits, not measured vendor performance or exact tokenization. Each claim-check invocation makes one logical request with no hidden splitting. The transport may retry transient failures (HTTP 408, 429, 500, 502, 503, 504, 529 and connection errors) at most twice, always within the same overall `deadline_ms`; no retry starts after the deadline, and other errors are not retried. Each retry can incur provider work. Repeated invocations still incur repeated work: these controls are not a provider billing cap or a mission-wide quota. Do not loop on failed requests without new evidence.

The endpoint is fixed to `https://api.typesafe.ai/v1/systemone`; redirects and endpoint environment overrides are not followed. A versioned `jev-X.Y.Z` ID is required; moving aliases are rejected. Run `software-factory auth login` once; all JEV operations use the shared user credential automatically. `TYPESAFE_API_KEY` remains an explicit environment override for CI. See [authentication](authentication.md) for storage, status, rotation and project upgrades. Keep the value out of factory configuration, prompts, committed files and command arguments.

## Commands and input

All commands run from the repository root:

```sh
uv run --locked --project .factory software-factory semantic status
uv run --locked --project .factory software-factory semantic example
uv run --locked --project .factory software-factory semantic check --input .factory/local/claims.json
uv run --locked --project .factory software-factory semantic check --input .factory/local/claims.json --mission M-0001
uv run --locked --project .factory software-factory semantic check --input .factory/local/claims.json --no-network
uv run --locked --project .factory software-factory semantic check --input .factory/local/claims.json --no-persist
```

`--input -` reads the packet from standard input instead (at most 256 KiB, UTF-8, one `-` per invocation). The orchestrator always uses this form with a quoted heredoc, so it never writes a file:

```sh
uv run --locked --project .factory software-factory semantic check --input - --mission M-0001 <<'JSON'
{"schema_version": 1, "operation": "claim_support", ...}
JSON
```

`example` prints a fresh, synthetic input without network access or writes. After making sure `.factory/local/` exists and is ignored, redirect that output to `.factory/local/claims.json` for a local example. Running `check` against it while ON makes an actual billed request; the example is not a quality benchmark.

Input follows `.factory/schemas/semantic.schema.json`:

```json
{
  "schema_version": 1,
  "operation": "claim_support",
  "purpose": "planning_research",
  "sources": [
    {
      "id": "S1",
      "reference": "https://example.invalid/product-documentation",
      "locator": "Deployment section, paragraph 2",
      "retrieved_at": "2026-09-27T00:00:00.000Z",
      "excerpt": "The service supports staging deployments.",
      "sha256": "<SHA-256 of the exact UTF-8 excerpt>",
      "context_complete": true
    }
  ],
  "claims": [
    {
      "id": "C1",
      "text": "The service supports staging deployments.",
      "source_ids": ["S1"],
      "quote": "supports staging deployments"
    }
  ]
}
```

This annotated shape is not executable input because the hash is a placeholder. Use `example` for valid synthetic input. Replace the example's content and provenance with actual retrieved material for real research. A caller-provided timestamp/hash establishes local consistency, not source authenticity or proof of a fresh retrieval.

Source IDs and claim IDs must be unique, and each claim source ID must name a supplied source. Claims, excerpts, reference/locator labels and any supplied quote must contain non-whitespace text; invalid input fails before credential lookup or inference. The quote field is optional. When supplied, quote matching normalizes whitespace only; no hidden fuzzy matching or silent truncation. A supplied quote absent from its sources, explicitly incomplete declared context or stale recorded retrieval time marks affected claims unresolved before inference. Other eligible claims may still be assessed in the same batch. All declared claims remain in coverage.

The request includes only eligible claim text, selected excerpts, IDs and locators. Claim text, locators and excerpts pass through the same secret mask as `verify-claims` (see Redaction below) before sending, and the report records the mask counts, never the secrets. Each source's `sha256` and the packet hash are taken over the raw, unmasked excerpt you supplied, so they still identify the retrieved text; the masked form is what the provider sees. Reference URLs, timestamps, hashes, mission records and repository files are not automatically uploaded. Question instructions explicitly identify each pair because Jev does not see question-map keys during inference.

## OFF, status, shadow and fallback

- OFF returns before input, credential, cache and candidate inspection; it creates no local semantic records.
- `status` validates local configuration only. It never checks credentials, creates a cache, or calls a provider. It reports the effective `claim_accept_confidence` and the retry policy (`requests_per_invocation: 1`, `automatic_retries: 2`, bounded by `deadline_ms`). Factory status remains read-only.
- `--no-network` is a full bypass, including cached advice. Use it when the request disallows external evaluation.
- `--no-cache` avoids cache reads and writes but still saves an operational/advisory record.
- `--no-persist` avoids all local semantic writes and cache use. It is supported in advisory mode; shadow mode skips because it needs a private evaluation record.
- Shadow output contains a report path/hash, timing, usage and coverage, with no relationships/probabilities/confidence and no review states or accepted/needs_review counts (counts would reveal withheld confidence). The private report holds per-claim review states and a `review` summary. `report_hash` is SHA-256 of the report serialized as compact JSON with object keys sorted recursively and array order preserved; it is not the pretty-printed file checksum. Freeze and finish baseline work before inspecting that file. This does not hide the file from an agent with filesystem access; the skill defines the evaluation procedure.
- API errors, deadlines and invalid responses return unavailable/invalid assistance. Changed inputs invalidate advice; changes detected during final publication skip the result and reconcile local writes. Continue the ordinary workflow and retain full required evidence.
- Exit 0 means completed, unresolved or skipped advisory work; exit 1 means a local configuration/input/command/storage error (including failed cleanup); exit 2 means unavailable/invalid assistance, from the provider or from local causes such as inputs that changed during the call (invalid) or a busy local request lock (unavailable). No exit code establishes product readiness.

Configuration and caller cancellation are observed through preparation, inference, cache/report writes and lock release. Advice inputs and candidate are rechecked after persistence, then configuration and cancellation are checked at the public return boundary. A detected change suppresses output and rolls back this invocation's owned writes: new artifacts are removed, overwritten cache bytes are restored, and previous reports or successor writes are preserved. Rollback reacquires the exclusive local lock to coordinate with other helper invocations. If that lock is busy or cleanup fails, the helper returns a local error without advice; inspect local records before retrying.

Observation and rollback are best effort, not an atomic filesystem transaction or a crash-recovery guarantee. Provisional files can exist during a call; do not consume them before the helper returns successfully. Cancellation cannot undo transmitted data or provider billing. Later changes cannot erase annotations already loaded into an agent context. The HTTP deadline covers request and body handling; reported `elapsed_ms` measures assessment before persistence and final revalidation, so measure total command wall time separately for latency evaluation.

## Local records and cache

The helper uses `.factory/local/semantic/` for an exclusive request lock, content-keyed cache and shadow/advisory records. The entire directory must be ignored and untracked before persistence. Existing symlinks and non-regular input files are rejected. Local paths are not an OS sandbox against hostile concurrent filesystem replacement.

Cache identity includes the complete supplied input, candidate snapshot, optional mission specification, configuration (including `claim_accept_confidence`, which reports also record in `provenance` and `review`), rubric, schema and actual helper implementation. The cache stores only validated raw answers; review states are recomputed from them with the current threshold. Cached provider responses undergo the same validation as network responses. Cache replacement keeps a bounded in-memory backup for rollback; an existing entry larger than the configured response bound plus 4096 bytes is preserved instead of replaced. A unique write ID distinguishes a cache generation from successor writes. TTL does not establish freshness of remote documentation; retrieve and timestamp sources honestly. No cache advice is supplied while OFF or under `--no-network`.

Records contain IDs, source references/hashes, relationships, probabilities, confidence, review states, the acceptance threshold, usage and timing; raw excerpts, raw HTTP bodies and API keys are not stored. Source references/IDs can still be sensitive: keep these ignored records local and inspect before sharing. No raw upstream error messages are printed.

The lock rejects concurrent persisted invocations without making another request. After a crash, inspect the recorded PID and actual process state before removing a stale `request.lock`; no automatic lock stealing occurs. `--no-persist` creates no lock. Cache/report retention is operator-managed; inspect and prune this directory between completed evaluations. Local hashes provide consistency, not authentication or immutable audit.

## Checking implementer claims against the mission diff

`semantic verify-claims` checks short claims an implementer made about its own work (for example "greet() now takes a name") against what actually changed in the repository. It follows TypeSafe's citation-check cookbook: a deterministic evidence check first, then one Choice question per remaining claim. It is advisory only: it never modifies mission state, task results, evidence, repair budgets or the gate, and its output is never evidence for the gate, verification or review.

```sh
uv run --locked --project .factory software-factory semantic verify-claims --mission M-0001 --input - <<'JSON'
{"schema_version": 1, "claims": [{"id": "C1", "claim": "greet() now takes a name argument", "paths": ["src/app.py"]}]}
JSON
```

`--no-network`, `--no-cache` and `--no-persist` behave as for `check`, except that `--no-network` still runs the deterministic step: claims with repository changes are reported `unresolved` with reason `network_disallowed`. The input has the same rules as `check`: `-` for standard input, or a repository-root-relative regular file without symlinks; at most 256 KiB, schema-validated; errors never echo input. Input follows `.factory/schemas/claims-verify.schema.json`:

```json
{
  "schema_version": 1,
  "claims": [
    {"id": "C1", "task_id": "T1", "claim": "greet() now takes a name argument", "paths": ["src/app.py"]}
  ]
}
```

`task_id` is optional and must name a task of the mission. Each claim has at most 1000 characters and cites 1–8 repository-relative files; at most 16 claims (and no more than `jev.max_pairs`) per invocation. A cited path must be a regular file in the repository at the mission base or in the working tree; paths outside the repository, under `.git/` or `.factory/local/` (compared case-insensitively and by resolved real path), symlinks or paths through symlinks, directories and ignored untracked files are rejected before any request.

What is sent. Evidence comes only from the repository, never from agent text: for each cited path, the diff between the mission's `base_commit` and the current working tree (untracked new files appear as added lines). Each path's excerpt is capped at 4 KiB and each claim's at 16 KiB; truncation is recorded per path and per claim. Only the masked claim text, cited paths and masked excerpts are sent, with one Choice question per claim whose criteria are exactly `supports`, `contradicts` and `says_nothing`. If none of a claim's cited paths changed since the base commit, the verdict is `no_evidence` and the claim is not sent. Claims are added to the single request in input order while it fits `max_request_bytes`; later claims are `unresolved` with `request_too_large` rather than split into hidden extra requests.

Redaction. Before sending, the claim text and every excerpt pass through the shared masking helper, which replaces recognized secrets with `[REDACTED:kind]`: PEM private key blocks, including a body without its BEGIN line (a run of three or more lines of 40 or more base64 characters, or any block ending in `-----END … PRIVATE KEY-----`); passwords in URL userinfo (`postgres://user:…@host`, `https://user:…@host`); Slack webhook URLs; Azure connection-string keys (`AccountKey=…`, `SharedAccessKey=…`, `SharedAccessSignature=…`) and SAS `sig=…` values; `Authorization` header values (including `Bearer`, `Basic`, `Token`, `Digest` and `Bot` schemes) and bearer tokens; JWTs (`eyJ….eyJ….…`); AWS access key IDs; `sk-…`, Stripe secret and restricted keys (`sk_live_…`, `sk_test_…`, `rk_…`, `whsec_…`), Google API keys (`AIza…`), GitHub (`ghp_…`, `github_pat_…`), GitLab (`glpat-…`), Hugging Face (`hf_…`), SendGrid (`SG.….…`), npm (`npm_…`) and Slack (`xox?-…`) tokens; docker config `"auth": "…"` and `.npmrc` `_auth=…` values; `.netrc` passwords (`login NAME password VALUE`, or a line starting `password VALUE`); the password in `curl -u user:…`/`--user user:…`; values of `--password`/`--token`-style flags, MySQL `-p…`, `sshpass -p …` and `docker login -p …`; and values assigned to names containing `api_key`, `secret`, `token`, `password`, `passphrase`, `pwd`, `private_key`, `access_key` or `credential`, or a whole `pass` name part (`DB_PASS`, `SMTP_PASS`, `PASS`, but not `bypass`, `passed` or `pass_rate`), where a quoted value is masked whole. Plain short numbers, booleans/null and obvious code (`name(…`, `name[…`, comparisons such as `==`) are left as written. Home-directory prefixes such as `/home/<name>` and the current user's home become `~`. Each cited path's diff is bounded to 64 KiB of whole lines, masked, and only then cut on a line boundary to the excerpt limit, so a cut never splits a secret or a mask. Reports record per-claim mask counts, never the secrets.

Known limits: masking is a best-effort denylist of known shapes, not a guarantee. A bare high-entropy string without a recognizable prefix or key name (for example a bare 40-character AWS secret access key, a hex API key or a random password in prose) is not masked; nor is a secret in an unusual or vendor-specific format, a value on a different line from its key, an unquoted value shaped like code (`name(…`), a short numeric value, a key name with more than 64 characters after its key word (or a flag with more than 64 on either side), or a password flag more than 1 KiB after its command name. Masking can also hide harmless values that look like assignments. Do not cite files containing anything you would not send to the provider.

Verdicts and review. `supports`, `contradicts` or `says_nothing` get `review: accepted` when confidence is at least `jev.claim_accept_confidence` (default 0.8), otherwise `needs_review`; an abstaining answer, if ever returned, is `unresolved` with `needs_review`. `no_evidence` and `unresolved` claims have no review state. Treat `needs_review`, `contradicts` and `no_evidence` as prompts to inspect the diff yourself, never as approval or as proof of a defect; `supports` with `accepted` is not verification.

Modes and records. Disabled (`jev.enabled: false`) returns `unavailable` with reason `disabled` (exit 2) without reading input or sending anything. Shadow mode (default) returns only operational metadata (status, reason, claim count, timing) plus the private report path and `report_hash`, with no verdicts, review states, counts, usage or cache state; shadow with `--no-persist` skips. Advisory mode returns per-claim `verdict`, `review`, `confidence`, `paths` and `truncated`, plus the full record. Reports and the cache live under `.factory/local/semantic/verify-claims/` with the same private-directory, lock, rollback and exit-code rules as `check`. The cache key binds the candidate fingerprint, mission ID, base commit, excerpt hashes, input, settings (including the threshold) and model; the cache stores only validated raw answers. Reports keep excerpt hashes, never excerpt text.

## Client prompts

| Client | Invocation |
| --- | --- |
| Claude Code | `/factory-semantic` |
| Native Codex | `$factory-semantic` |
| GitHub Copilot | Select the factory agent, then `/factory-semantic` or explicitly ask it to use the skill |

Starter: “Use factory-semantic to assess the supplied research claims and excerpts. Check the current toggle first. Keep ordinary review intact, honor shadow mode, and report coverage and unavailable results. Do not enable the service or change model assignments.”

The main orchestrator calls the CLI through permitted execution/network access. Planner and reviewer permissions are not widened. No MCP server, automatic prompt hook, model interception or native Jev coding model is installed. Actual discovery/environment access must be checked in each client, separately from generated-file tests.

## Evaluation and operational readiness

Compare existing workflow, deterministic source/quote checks, and those checks plus Jev on independently labeled representative cases. Include contradictions, partial support, missing context, stale material, source swapping, malformed responses, omissions, injected source instructions and service failures. Freeze model/rubric/input versions and baseline packets. Keep shadow annotations hidden until baseline work is complete.

Measure additional confirmed findings, false support, false alarms, missed defects, coverage/abstentions, reviewer time, total cost and end-to-end p50/p95 latency. Include failures/timeouts. Synthetic fixtures verify mechanics; they do not measure Jev quality. Visible advisory trials are needed to measure anchoring and actual review speed. Keep OFF if added burden exceeds benefit.

Current checks cover mocked transport and local workflows. Paid inference, calibration, account availability and native client behavior are not established by the test suite. See [provider references](../../skills/factory-semantic/references/typesafe.md) and [vendor smoke tests](../vendor-smoke-tests.md).

## Installation and upgrades

New installations include this skill, helper, schema, rubric and runbook with OFF/shadow configuration. Omitting the optional block means OFF. The existing upgrader conservatively reports conflicts for changed existing schemas when a target has mission history; follow the reviewed upgrade procedure in [upgrading](upgrading.md). Do not bypass that safeguard for this optional feature.

See [the complete Jev factory flow](../jev-architecture.md) for entry scopes, task and mission lifecycles, delivery/recovery and the ON/OFF diagram.
