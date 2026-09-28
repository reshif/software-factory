# Optional failed-check triage

`software-factory triage --mission ID --revision REV [--check CHECK_ID] [--no-network]` asks Jev to suggest why failed checks in one registered verification run failed. It helps choose where to start a diagnosis. It is advisory only: it never changes the repair budget, attempts, `repair_required`, task or mission state, evidence, results or any gate, and a classification never counts as a diagnosis, a retry reason or a passing check.

Triage is a separate top-level command so `software-factory checks [--only ID] [--require-clean]` keeps its existing syntax and behavior.

## Categories and thresholds

One Choice question per failing check chooses exactly one option:

| Category | Meaning | Where diagnosis starts |
| --- | --- | --- |
| flaky | Nondeterministic: timing, ordering, races, intermittent dependencies | Reproduce repeatedly; find the source of nondeterminism |
| environment | Tools, versions, permissions, network, disk or OS differences | Compare the runner environment with the expected one |
| test_defect | The product is right; the test, fixture or expectation is wrong | Check the test against the accepted specification |
| product_defect | The product code behaves incorrectly | Reproduce and fix the product code |
| configuration | Check argv, cwd, timeout, output limit, setup or settings are wrong | Review `factory.json` and setup under the scope policy |
| abstain | Evidence is insufficient to choose | Diagnose without a suggestion |

TypeSafe's [confidence-routing guidance](https://docs.typesafe.ai/patterns/confidence-routing) routes low-confidence choices to a human instead of applying them. A check is `classified` when the choice is not `abstain` and its confidence is at least `jev.triage_accept_confidence` (0–1, default 0.6). Otherwise it is `unclassified` with no category, and a human decides. Confidence describes the returned distribution, not a measured probability that the category is right; see the provider's [confidence documentation](https://docs.typesafe.ai/confidence). An invalid threshold is a configuration error.

## What is read and sent

The command reads `.factory/missions/<ID>/evidence/<REV>/checks.json` only when it is registered in `mission.json` with a matching mission ID, revision and sequence, and passes the evidence schema. It selects checks that did not pass (optionally only `--check`), at most 8. For each it reads the private stdout and stderr logs at the paths the evidence records. A path that differs from the expected location, a symlink, a non-regular file or a SHA-256 that differs from the recorded hash refuses the whole run (exit 2) before any request.

One logical request is sent, containing for each selected check: its ID, argv, status label, exit code, signal, duration, output-truncation flag and the last 6 KiB of stdout and stderr combined. Up to 24 KiB of whole lines at the end of each log is masked first and only then cut to that bound, so a cut tail starts at the next full line and never splits a secret or a mask. Argv items and log text pass through the factory's redaction before sending: private key blocks, passwords in URL userinfo, Slack webhook URLs, Azure connection-string keys (`AccountKey=`, `SharedAccessKey=`, `SharedAccessSignature=`) and SAS `sig=` values, `Authorization` values (including `Bearer`, `Basic`, `Token`, `Digest` and `Bot` schemes) and bearer tokens, JWTs, AWS access key IDs and `sk-`, Stripe (`sk_`/`rk_`), Google (`AIza`), GitHub, GitLab (`glpat-`), Hugging Face (`hf_`), SendGrid (`SG.`), npm and Slack tokens, docker `"auth"` and `.npmrc` `_auth` values, `.netrc` passwords (`login NAME password VALUE`, or a line starting `password VALUE`), the password in `curl -u`/`--user user:pass`, `--password`/`--token`-style flag values, MySQL `-p…`, `sshpass -p` and `docker login -p` values (also when the value is the next argv item; `mysql -p NAME` keeps `NAME`, the database), and values assigned to key, secret, token, password, passphrase or credential names or names with a whole `pass` part such as `DB_PASS` (quoted values whole); home-directory prefixes become `~`. Known limits: redaction is a best-effort denylist of known shapes, not a guarantee. A bare high-entropy string without a recognizable prefix or key name (such as a bare AWS secret access key), a secret in an unusual or vendor-specific format, a value on another line from its key, short numeric values, a key name with more than 64 characters after its key word (or a flag with more than 64 on either side), and a password flag more than 1 KiB after its command name are not masked; masking can also hide harmless values that look like assignments. Do not let checks print material you would not send to the provider. Requests larger than `jev.max_request_bytes` are not sent and report `unresolved`. `jev.deadline_ms` and `jev.max_response_bytes` bound the request; the transport retries at most twice inside that deadline. Responses are validated against the exact option set.

## Modes, records and exit codes

`jev.enabled` false reports `unavailable` (reason `disabled`) and makes no request; `--no-network` does the same with reason `network_disallowed`. Both happen before evidence or logs are read. No environment override or saved credential reports `credential_missing`; unsafe or unavailable saved storage reports `credential_unavailable`. See [authentication](authentication.md).

`jev.claim_mode` also selects the triage output. In `shadow` (the default) output contains only operational metadata (status, coverage counts, usage, attempts, elapsed time) and the private report path and hash; categories, confidences and review states are withheld. In `advisory` output lists `{check, category, confidence, review}` per check, with `category` null unless classified.

Each run writes a report under `.factory/local/semantic/triage/`, which must be ignored by a shared `.gitignore` rule and untracked. The report records the evidence reference and hash, recorded log hashes, hashes of the sent excerpts and the request, mask counts, the raw choice, probabilities, confidence and review state. It never stores log text, argv or the key.

Exit code 2 means `unavailable` or `invalid`. `complete`, `unresolved` and `nothing_to_triage` exit 0.

## Using the result

Before retrying a failed check, a triage category can suggest where diagnosis starts. It never replaces reproducing the failure, stating a hypothesis and collecting discriminating evidence, and it never justifies retrying blindly, skipping a check or changing required checks. Repairs, attempts and gates follow the [repair procedure](../../skills/factory-repair/SKILL.md) and [delivery and recovery](delivery-and-recovery.md) unchanged. Paid inference and calibration for your checks are not established by the test suite.

Credential setup is shared with model planning and semantic checks: run `software-factory auth login` once. See [authentication](authentication.md).
