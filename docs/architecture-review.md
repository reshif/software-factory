# Architecture review — file coverage and findings

This review read every pre-existing first-party file in the Python/uv project and traced the executable paths through callers, schemas, bundled assets and tests. The source manifest contains **107 files / 19,850 lines** and a SHA-256 for each reviewed file. The reviewed snapshot includes the 0.2.4 version bump, edited-section uninstall/reinstall fix and associated regression tests made concurrently in this workspace.

[Architecture and five diagrams](architecture.md) · [Machine-readable source manifest](architecture/source-review.json)

## Scope and method

The coverage below is one row per file, including hidden project configuration, both lockfiles, all Python source/tests, every bundled JSON/Markdown/template, and historical verification records. Lockfiles were parsed in full to inspect package records, dependency edges and artifact hashes. Code/data contracts were compared with both positive and negative tests; existing docs were corroborating material, never the basis for assuming a capability exists.

Generated `.venv` dependencies, Python bytecode, pytest/ruff caches and build archives are not additional authored modules. Their third-party source was not audited file by file. Wheel/sdist contents and package/source identity were checked separately, and the isolated wheel smoke exercised the installed runtime. Newly written architecture artifacts are outputs of this review, so they are excluded from the 107-file source manifest. The original `/home/reshif/software-factory` checkout was not modified.

The workspace has no Git metadata. SHA-256 file identities, rather than an invented commit ID, pin this review. Validation ran against a disposable copy of the exact reviewed runtime/source bytes so concurrent edits could not silently change the tested input. Two root release reports were subsequently refreshed by the concurrent release session and reread; those files are outside the package and executable checks.

## Documentation disagreements and implementation limits

These findings describe the reviewed snapshot; they are not changes to runtime behavior.

| Existing claim / implied capability | What the implementation actually does | Evidence |
|---|---|---|
| README's sample product check is ready to paste. | It omits required `timeout_seconds`; the factory schema requires it on each check. | `README.md` Configure the product; `schemas/factory.schema.json` check definition. |
| README's complete sequence puts final results before checks. | A completed task result must reference applicable verification; provisional/blocked results can precede verification. | `checks.verify_mission`; `workflow.validate_completed_result`, `record_results`, `transition_task`. |
| Vendor smoke checklist says exact mechanical profile switching can be exempt from protected-path maintenance. | Generated paths remain protected, including profile changes. The current resume runbook correctly says maintenance is required. | `data/docs/vendor-smoke-tests.md` Cross-vendor test; `workflow._assess_gate_snapshot` explicit comment and protected-path predicate. |
| Prompts runbook says default `factory-tests` validates the factory. | New installs get a deliberately failing `configure-me` check until the product is configured. | `data/docs/runbooks/prompts.md`; `installation.starter`. |
| Prompts runbook promises argument/path hints, `next_step`, and `model_commands` in render output. | `prompt_commands` entries contain `profile`, `invoke`, `name`; those extra response fields are not emitted. | `rendering.plan_render` command construction and return object. |
| Maintenance runbook promises required argument/default-prompt metadata and validated registry extensions. | `metadata` enforces name/description. The renderer reads the registry directly; hashing the shipped registry schema is not full schema validation. | `rendering.metadata`, `rendering.plan_render`; `schemas/registry.schema.json`. |
| Shipped vendor JSON and templates might look like executable extension points. | Vendor rendering is hardcoded in Python. Mission JSON and PR/release packets are constructed in Python; not every shipped template is consumed. | `rendering.plan_render`; `workflow.create_mission`, `create_packet`. |
| Historical verification says three configuration keys have no runtime effect. | `completion_target` does not drive progression; `parallel_writers` is constrained to one with a hardcoded writer rule; `evidence_exclude` is validated, while actual exclusions are built in. These are not three arbitrary working extension knobs. | `docs/verification.json`; factory schema; `core.load_config`; `workflow._assert_writer`; `evidence.is_metadata`. |
| A passing render check or readiness gate could be read as all doctor diagnostics passing. | Doctor's replaced entry-skill error and runtime-drift diagnostics are distinct checks, not automatically included as universal gate/launch preconditions. | `cli.doctor`, `runtime_drift`, `_dispatch`; `workflow._assess_gate_snapshot`. |
| Configured reviewer name, CI URL, observed model and delivery references could be read as authenticated proof. | These are caller-supplied local records. The gate rejects the maintainer's author label but does not enforce `owners.reviewer` as an identity allowlist or authenticate remote outcomes. | `workflow.review_reasons`, `_assess_gate_snapshot`, `record_ci`, `assess_merged`; `models.validate_task_observation`. |

The older render-lock/recovery and profile-switch claims in the resume runbook were corrected in the snapshot captured here; they are not reported as remaining defects. The maintenance runbook's `scaling-and-extensions.md#controlled-content-extensions` link still targets an absent section. This architecture adds a separate reference rather than editing existing user/runtime files.

## Validation

The final source review reached version 0.2.5. The full tests, build and wheel smoke below ran on the isolated 0.2.4 snapshot. The subsequent changes are five version-metadata edits and a corrected routing deadline comment; comparison confirmed no executable routing-logic change. Those edits were reviewed without claiming another full 0.2.5 test run.

Final results are recorded in [validation.json](architecture/validation.json). These checks cover Python behavior, packaging, preservation, mocked provider behavior and Mermaid syntax/rendering. They do not establish live native-client discovery, paid JEV inference, real remote CI/deployment, or macOS/Windows behavior. No live provider request or publication was performed.

## File-by-file coverage

| File | Lines | Architectural finding |
|---|---:|---|
| [.gitignore](../.gitignore) | 7 | Excludes generated environments, bytecode, build outputs and local tooling caches from authored source. |
| [AGENTS.md](../AGENTS.md) | 3 | Port constraints, original-checkout preservation, one-writer rule and release validation requirements. |
| [README.md](../README.md) | 97 | User entry path, product configuration, pinned dispatch, JEV and lifecycle claims; check example and completion ordering have mismatches below. |
| [docs/implementation.md](../docs/implementation.md) | 20 | Implementation scope, packaging decisions and intentional external execution boundaries; checked against Python modules. |
| [docs/release-process.md](../docs/release-process.md) | 36 | Release/build/install procedure; describes validation and private publication boundary, not an executing pipeline. |
| [docs/restart-record.json](../docs/restart-record.json) | 78 | Historical continuation/provenance record; contextual evidence only, not imported by runtime. |
| [docs/verification.json](../docs/verification.json) | 274 | Release source/artifact hashes, checks and limitations; the concurrent 0.2.4 update was reread and compared with this review’s independently tested wheel. |
| [docs/verification.md](../docs/verification.md) | 94 | Historical release validation narrative; distinguished from checks executed for this architecture review. |
| [pyproject.toml](../pyproject.toml) | 29 | Python >=3.11 package, uv_build backend, console entry point, four runtime dependencies and development-only test/lint dependencies. |
| [scripts/release_smoke.py](../scripts/release_smoke.py) | 418 | Actual wheel isolation, no-Node PATH, hydrate/dispatch, all profiles, preservation/reinstall, crash recovery and mocked routing fixtures. |
| [src/software_factory/__init__.py](../src/software_factory/__init__.py) | 1 | Version constant; synchronized with root/runtime packaging metadata. |
| [src/software_factory/__main__.py](../src/software_factory/__main__.py) | 4 | Module entry point delegates to cli.main. |
| [src/software_factory/calibration.py](../src/software_factory/calibration.py) | 341 | Private immutable outcome records, task-result/model provenance joins and comparable-cohort statistics without auto-ranking or policy feedback. |
| [src/software_factory/checks.py](../src/software_factory/checks.py) | 536 | Bounded subprocesses, sequential setup/check suites, private logs, evidence sequence registration and current-result validation. |
| [src/software_factory/cli.py](../src/software_factory/cli.py) | 445 | Pre-parser pinned-runtime dispatch; global lifecycle exceptions; inspect/doctor diagnostics and command wiring. |
| [src/software_factory/core.py](../src/software_factory/core.py) | 282 | Safe paths and asset lookup, JSON/schema helpers, Git/root discovery, ignored-private directory enforcement and runtime/dependency identity. |
| [src/software_factory/data/CONSTITUTION.md](../src/software_factory/data/CONSTITUTION.md) | 26 | Constitution 1.0.0: authority, scope, bounded delegation, evidence, review and external-action principles; embedded in exports and hashed in missions. |
| [src/software_factory/data/docs/architecture.md](../src/software_factory/data/docs/architecture.md) | 138 | Older bundled high-level architecture; compared with dispatch, workflow and ownership implementation rather than accepted as authority. |
| [src/software_factory/data/docs/jev-architecture.md](../src/software_factory/data/docs/jev-architecture.md) | 182 | Bundled JEV design explanation; checked against separate routing and claim-assistance code paths. |
| [src/software_factory/data/docs/jev-routing.md](../src/software_factory/data/docs/jev-routing.md) | 74 | Routing request/receipt/bounds and failure semantics; validated against models.py, routing.py and jev.py. |
| [src/software_factory/data/docs/runbooks/adopt-existing-repo.md](../src/software_factory/data/docs/runbooks/adopt-existing-repo.md) | 6 | Adoption/baseline procedure and preserving existing product setup. |
| [src/software_factory/data/docs/runbooks/delivery-and-recovery.md](../src/software_factory/data/docs/runbooks/delivery-and-recovery.md) | 40 | External-evidence progression and recovery procedure; state mutation is not deployment execution. |
| [src/software_factory/data/docs/runbooks/factory-maintenance.md](../src/software_factory/data/docs/runbooks/factory-maintenance.md) | 33 | Governance change procedure; overstates renderer metadata/registry validation and links a missing extension anchor. |
| [src/software_factory/data/docs/runbooks/factory-setup.md](../src/software_factory/data/docs/runbooks/factory-setup.md) | 24 | Python/uv install, configuration and native-client setup procedure. |
| [src/software_factory/data/docs/runbooks/github-setup.md](../src/software_factory/data/docs/runbooks/github-setup.md) | 7 | Manual external GitHub controls and example CI setup; no hosting API automation exists in runtime. |
| [src/software_factory/data/docs/runbooks/model-selection.md](../src/software_factory/data/docs/runbooks/model-selection.md) | 202 | Research/inventory/selection checkpoint and host application procedure; distinguish request from observed identity. |
| [src/software_factory/data/docs/runbooks/prompts.md](../src/software_factory/data/docs/runbooks/prompts.md) | 248 | Client entry workflows; stale default-check and renderer output-field claims listed below. |
| [src/software_factory/data/docs/runbooks/resume-and-switch.md](../src/software_factory/data/docs/runbooks/resume-and-switch.md) | 62 | Current maintenance-only profile-switch rule and shared journal recovery now match code; pause/resume and post-merge candidate reconciliation checked. |
| [src/software_factory/data/docs/runbooks/runtime-contract.md](../src/software_factory/data/docs/runbooks/runtime-contract.md) | 49 | Command/runtime behavior and local record contracts; compared with CLI dispatch and actual gates. |
| [src/software_factory/data/docs/runbooks/scaling-and-extensions.md](../src/software_factory/data/docs/runbooks/scaling-and-extensions.md) | 7 | Guidance for growth and extension; does not add distributed scheduling or executable plugin discovery. |
| [src/software_factory/data/docs/runbooks/semantic-assistance.md](../src/software_factory/data/docs/runbooks/semantic-assistance.md) | 157 | Packet/mode/privacy/caching and unavailable behavior; checked against semantic.py early exits and publication checks. |
| [src/software_factory/data/docs/runbooks/upgrading.md](../src/software_factory/data/docs/runbooks/upgrading.md) | 15 | Upgrade ownership, schema-history reconciliation, runtime and edited-export preservation procedure. |
| [src/software_factory/data/docs/runbooks/vendor-behavior.md](../src/software_factory/data/docs/runbooks/vendor-behavior.md) | 60 | Vendor format/source guidance and observation limits; this review validates generated code output, not current live vendor compatibility. |
| [src/software_factory/data/docs/vendor-smoke-tests.md](../src/software_factory/data/docs/vendor-smoke-tests.md) | 67 | Unexecuted live-client checklist; remaining profile-switch exception contradicts the current gate. |
| [src/software_factory/data/models/policy.json](../src/software_factory/data/models/policy.json) | 43 | Model-selection hard controls, assignment defaults, required capabilities, inventory freshness and provenance constraints. |
| [src/software_factory/data/models/recommendations.json](../src/software_factory/data/models/recommendations.json) | 135 | Reviewed identity/guidance records used by selector; advice is not live account availability evidence. |
| [src/software_factory/data/models/selection-rubric.json](../src/software_factory/data/models/selection-rubric.json) | 5 | Versioned bounded-choice model-selection rubric and provider instructions included in routing-contract identity. |
| [src/software_factory/data/policy.json](../src/software_factory/data/policy.json) | 39 | Protected/sensitive path patterns and required decision types; runtime combines baseline/current policy with a hardcoded protected minimum. |
| [src/software_factory/data/prompts/factory-blueprint.md](../src/software_factory/data/prompts/factory-blueprint.md) | 20 | Draft-only planning entry with explicit read-only handling; restrictions are native-client instructions. |
| [src/software_factory/data/prompts/factory-build.md](../src/software_factory/data/prompts/factory-build.md) | 20 | Main-session entry workflow for implementation, startup checkpoint, specification, tasks, checks and review. |
| [src/software_factory/data/prompts/factory-resume.md](../src/software_factory/data/prompts/factory-resume.md) | 20 | Existing mission reconciliation, checkpoint freshness and continuation without resetting history. |
| [src/software_factory/data/prompts/factory-status.md](../src/software_factory/data/prompts/factory-status.md) | 18 | Read-only status entry; does not authorize executing checks or refreshing model selection. |
| [src/software_factory/data/registry.json](../src/software_factory/data/registry.json) | 54 | Four specialists, twelve workflow skills and four entry prompts; renderer uses these lists, but does not call full registry-schema validation. |
| [src/software_factory/data/roles/implementer.md](../src/software_factory/data/roles/implementer.md) | 11 | Bounded product edits, task ownership, verification/result obligations and repair reporting. |
| [src/software_factory/data/roles/orchestrator.md](../src/software_factory/data/roles/orchestrator.md) | 37 | Native main-session coordination, authority, specialist context, workflow checkpoints and evidence responsibility. |
| [src/software_factory/data/roles/planner.md](../src/software_factory/data/roles/planner.md) | 11 | Read-oriented specialist planning and research contract; no Python planner agent is instantiated. |
| [src/software_factory/data/roles/reviewer.md](../src/software_factory/data/roles/reviewer.md) | 9 | Separate review context, source/evidence inspection, severity and findings contract. |
| [src/software_factory/data/roles/verifier.md](../src/software_factory/data/roles/verifier.md) | 11 | Configured check execution and truthful evidence reporting without product-repair authority. |
| [src/software_factory/data/runtime/pyproject.toml](../src/software_factory/data/runtime/pyproject.toml) | 20 | Installed project packaging metadata matching Python version/direct dependencies, without development tool dependencies. |
| [src/software_factory/data/runtime/uv.lock](../src/software_factory/data/runtime/uv.lock) | 295 | Parsed complete runtime lock: ten package records including conditional dependencies and hashed artifacts; used by installed uv hydration. |
| [src/software_factory/data/schemas/evidence.schema.json](../src/software_factory/data/schemas/evidence.schema.json) | 228 | Verification sequence, snapshot/fingerprint, check statuses, log references/hashes and model-assignment fields. |
| [src/software_factory/data/schemas/factory.schema.json](../src/software_factory/data/schemas/factory.schema.json) | 333 | Project profiles, check argv/cwd/timeouts, owners, limits, delivery, model-selection and JEV configuration contract. |
| [src/software_factory/data/schemas/installation.schema.json](../src/software_factory/data/schemas/installation.schema.json) | 59 | Installation version, runtime, managed hashes and status; validated by lifecycle code. |
| [src/software_factory/data/schemas/mission.schema.json](../src/software_factory/data/schemas/mission.schema.json) | 51 | Mission/state/task/attempt/decision/review/evidence/delivery records; structural validation complements workflow predicates. |
| [src/software_factory/data/schemas/model-outcome.schema.json](../src/software_factory/data/schemas/model-outcome.schema.json) | 613 | Calibration record links, model/runtime provenance, task class, comparable cohort and metrics. |
| [src/software_factory/data/schemas/models.schema.json](../src/software_factory/data/schemas/models.schema.json) | 1261 | Definitions for requests, inventory, plans, assignments, identities, policy and reviewed recommendation documents. |
| [src/software_factory/data/schemas/registry.schema.json](../src/software_factory/data/schemas/registry.schema.json) | 85 | Declared registry extension contract; presence does not mean rendering.py enforces the whole schema. |
| [src/software_factory/data/schemas/result-index.schema.json](../src/software_factory/data/schemas/result-index.schema.json) | 38 | Task-to-result index and immutable record hashes. |
| [src/software_factory/data/schemas/result.schema.json](../src/software_factory/data/schemas/result.schema.json) | 21 | Task-result status, revision/evidence link, changed files/checks, unresolved items and observed-model metadata. |
| [src/software_factory/data/schemas/semantic.schema.json](../src/software_factory/data/schemas/semantic.schema.json) | 124 | Explicit claim/source/relationship packet structure and constraints. |
| [src/software_factory/data/skills/factory-handoff/SKILL.md](../src/software_factory/data/skills/factory-handoff/SKILL.md) | 28 | Reconciliation and useful local PR/handoff packets; external controls remain separate. |
| [src/software_factory/data/skills/factory-implement/SKILL.md](../src/software_factory/data/skills/factory-implement/SKILL.md) | 28 | One-writer bounded implementation, attempt/result contract and explicit scope limits. |
| [src/software_factory/data/skills/factory-models/SKILL.md](../src/software_factory/data/skills/factory-models/SKILL.md) | 35 | Current model research/catalog, plan/dispatch workflow, inheritance limits and observed execution evidence. |
| [src/software_factory/data/skills/factory-plan/SKILL.md](../src/software_factory/data/skills/factory-plan/SKILL.md) | 30 | Task DAG, owned paths, acceptance checks, risks and recovery planning. |
| [src/software_factory/data/skills/factory-recover/SKILL.md](../src/software_factory/data/skills/factory-recover/SKILL.md) | 26 | Delivery/recovery evidence and follow-up instructions, distinct from installation crash recovery. |
| [src/software_factory/data/skills/factory-release/SKILL.md](../src/software_factory/data/skills/factory-release/SKILL.md) | 26 | Release progression requires real authorization/references; instruction content does not execute deployment. |
| [src/software_factory/data/skills/factory-repair/SKILL.md](../src/software_factory/data/skills/factory-repair/SKILL.md) | 28 | Bounded repair/replan and exhaustion escalation without silently resetting attempts. |
| [src/software_factory/data/skills/factory-review/SKILL.md](../src/software_factory/data/skills/factory-review/SKILL.md) | 28 | Independent review and current findings/evidence contract. |
| [src/software_factory/data/skills/factory-semantic/SKILL.md](../src/software_factory/data/skills/factory-semantic/SKILL.md) | 20 | Optional explicit claim/source research workflow and shadow/advisory boundaries. |
| [src/software_factory/data/skills/factory-semantic/references/typesafe.md](../src/software_factory/data/skills/factory-semantic/references/typesafe.md) | 15 | Provider contract/reference guidance; runtime transport behavior verified separately from prose. |
| [src/software_factory/data/skills/factory-semantic/resources/claim-support.json](../src/software_factory/data/skills/factory-semantic/resources/claim-support.json) | 12 | Versioned five-relation claim-support rubric validated by semantic.py and included in context hashes. |
| [src/software_factory/data/skills/factory-specify/SKILL.md](../src/software_factory/data/skills/factory-specify/SKILL.md) | 28 | Accepted scope/criteria and decision-reference procedure. |
| [src/software_factory/data/skills/factory-start/SKILL.md](../src/software_factory/data/skills/factory-start/SKILL.md) | 43 | Intake and startup/model checkpoint, existing-work discovery and bounded mission coordination. |
| [src/software_factory/data/skills/factory-verify/SKILL.md](../src/software_factory/data/skills/factory-verify/SKILL.md) | 28 | Configured checks, persisted verification and failure handling. |
| [src/software_factory/data/templates/ci-python.yml.in](../src/software_factory/data/templates/ci-python.yml.in) | 12 | Optional example GitHub Actions configuration; installation does not create/enable a live product CI workflow. |
| [src/software_factory/data/templates/decision.md](../src/software_factory/data/templates/decision.md) | 13 | Read as decisions.md scaffolding; operational decisions are separately recorded in mission.json. |
| [src/software_factory/data/templates/handoff.md](../src/software_factory/data/templates/handoff.md) | 19 | Read by mission creation as authored handoff; generated handoff packet uses a separate output path. |
| [src/software_factory/data/templates/mission.json](../src/software_factory/data/templates/mission.json) | 5 | Shipped example mission structure; create_mission constructs the runtime record in Python rather than loading this file. |
| [src/software_factory/data/templates/plan.md](../src/software_factory/data/templates/plan.md) | 23 | Read by mission creation; authored Risks content is checked by readiness prerequisites. |
| [src/software_factory/data/templates/pull-request.md](../src/software_factory/data/templates/pull-request.md) | 21 | Shipped reference template; create_packet assembles PR text in Python rather than loading this file. |
| [src/software_factory/data/templates/recovery.md](../src/software_factory/data/templates/recovery.md) | 21 | Read by mission creation; authored recovery detail is checked by readiness prerequisites. |
| [src/software_factory/data/templates/release.md](../src/software_factory/data/templates/release.md) | 23 | Shipped reference template; release packets are assembled in Python with supplied external references. |
| [src/software_factory/data/templates/review.md](../src/software_factory/data/templates/review.md) | 17 | Reference review template; runtime record_review validates supplied JSON and does not parse this Markdown. |
| [src/software_factory/data/templates/spec.md](../src/software_factory/data/templates/spec.md) | 27 | Read by mission creation as authored specification scaffolding; accepted hash governs scope. |
| [src/software_factory/data/vendors/claude.json](../src/software_factory/data/vendors/claude.json) | 16 | Shipped Claude descriptor/source metadata, hashed by renderer; adapter behavior is implemented in rendering.py. |
| [src/software_factory/data/vendors/codex.json](../src/software_factory/data/vendors/codex.json) | 16 | Shipped native Codex descriptor/source metadata, hashed by renderer; does not supply dynamically executed adapter code. |
| [src/software_factory/data/vendors/copilot.json](../src/software_factory/data/vendors/copilot.json) | 16 | Shipped Copilot descriptor/source metadata, hashed by renderer; generated paths/permissions are selected in Python. |
| [src/software_factory/data/workflow.json](../src/software_factory/data/workflow.json) | 25 | Complete allowed mission/task transition adjacency; Python adds prerequisites, resume and recovery behavior. |
| [src/software_factory/evidence.py](../src/software_factory/evidence.py) | 605 | Candidate/source snapshots, governed inputs, mission fingerprint and watchdog-based transient-change detection with Linux loss hooks. |
| [src/software_factory/installation.py](../src/software_factory/installation.py) | 578 | Defaults, payload copies, history-schema compatibility, uv staging, ownership-aware lifecycle; 0.2.4 edited-block detachment reviewed. |
| [src/software_factory/jev.py](../src/software_factory/jev.py) | 306 | Fixed-endpoint HTTPS with deadlines and bounded reads; strict JSON, probabilities, response model/Choice validation and redacted errors. |
| [src/software_factory/models.py](../src/software_factory/models.py) | 962 | Contract validation, catalogs/requests, eligibility, deterministic planning, native dispatch report, observed-model checks and Codex inventory protocol. |
| [src/software_factory/rendering.py](../src/software_factory/rendering.py) | 547 | Hardcoded vendor adapters, canonical hashes, prompt metadata, owned exports and relinquishment; 0.2.4 marker handling/TOML conflict message reviewed. |
| [src/software_factory/routing.py](../src/software_factory/routing.py) | 419 | JEV selection after eligibility, bounded payload/deadline, no silent fallback, contract receipts and offline response replay. |
| [src/software_factory/semantic.py](../src/software_factory/semantic.py) | 765 | Explicit claim/source assessment; disabled early bypass, shadow/advisory modes, cancellation/stamps, private cache/report publication and rollback. |
| [src/software_factory/transactions.py](../src/software_factory/transactions.py) | 248 | Durable installation/render journal, lock, planning snapshots, per-file atomic writes, conditional rollback and dead-owner recovery. |
| [src/software_factory/workflow.py](../src/software_factory/workflow.py) | 2016 | Mission and task transitions, DAG/budgets, writer lock, scope/decisions/reviews/results, model binding, gate/CI/merge/delivery, local packets. |
| [tests/test_checks.py](../tests/test_checks.py) | 390 | Check timeout/output/process cleanup, required setup, source drift, log/evidence integrity and invalidation fixtures. |
| [tests/test_cli.py](../tests/test_cli.py) | 196 | Parser/root options, inspection/doctor, dispatch bypass and pinned-runtime behavior. |
| [tests/test_installation.py](../tests/test_installation.py) | 927 | All seven profile combinations; preservation, conflicts, three-way upgrade, journals/recovery, relinquishment, schema history and new 0.2.4 detachment regressions. |
| [tests/test_jev.py](../tests/test_jev.py) | 431 | Transport/JSON contract boundaries, response limits, invalid Choice/probabilities, deadline and failure handling with mocked services. |
| [tests/test_models.py](../tests/test_models.py) | 507 | Eligibility, identity/effort/profile constraints, catalogs, discovery fixtures, dispatch and plan integrity. |
| [tests/test_native_package.py](../tests/test_native_package.py) | 86 | Python package contents/contract, bundled assets and no legacy Node runtime in package fixtures. |
| [tests/test_routing.py](../tests/test_routing.py) | 706 | Factory/JEV toggle, explicit preferences, multi-assignment bounds, abstention/errors, contract mutation and offline replay. |
| [tests/test_semantic.py](../tests/test_semantic.py) | 693 | OFF/no-network behavior, packet/rubric validation, caching, cancellation/mutation, privacy, stale inputs and advisory boundaries. |
| [tests/test_workflow.py](../tests/test_workflow.py) | 823 | Mission/task DAG, retry limits, evidence/results/reviews, protected controls, CI/merge/delivery, pause/resume and post-merge reconciliation. |
| [uv.lock](../uv.lock) | 393 | Parsed complete development lock: 17 package records, dependency edges and hashed artifacts; distinct from installed runtime lock. |
