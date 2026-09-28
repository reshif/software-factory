# Maintain factory behavior and configuration

Factory maintenance is a distinct scope because roles, skills, permissions, policy and checks govern later product work. An explicitly authorized maintenance request already provides that scope; do not ask again solely because a protected path is involved. Record the decision reference and have another context review the changed controls.

## Change canonical sources

Edit `.factory/CONSTITUTION.md`, `.factory/roles`, `.factory/skills`, `.factory/prompts`, vendor mappings or authored configuration. Do not edit generated profiles to hide drift. Keep the constitution compact enough to load and place task details in relevant skills. Confirm descriptions route to the intended work, links survive rendering and optional vendor-specific fields are actually supported. Entry prompts have a required core entry library plus validated registry extensions and required name, description, argument-hint and default-prompt metadata; the renderer validates it before touching exports.

Before changing vendor behavior, open the relevant official documentation, record the date/source/limits in [vendor behavior](vendor-behavior.md), and inspect the selected client's installed version. Avoid hard-coding a model or permission bypass to make setup succeed.

## Verify the change

Amending `.factory/CONSTITUTION.md` follows its Amendment section: a maintenance mission, independent review, a semver bump (MAJOR removes or redefines an obligation, MINOR adds one, PATCH changes wording only) and an impact record naming changed rules, affected roles, skills and exports, and the handling of in-flight missions. Update the [constitution enforcement map](constitution-enforcement.md) in the same change and cite rules by title, not number.

The constitution change itself is a factory-control change: land it through a maintenance mission, or on the trunk outside any product mission's diff (for example a `software-factory upgrade` committed on its own). Inside a product mission's candidate the gate reports "Protected factory path requires a maintenance mission: .factory/CONSTITUTION.md" by design.

Once the new constitution has landed, every pre-merge mission accepted under the old one, of any kind (including the maintenance mission that made the change), stops until it is reconciled; the gate and transition errors print the exact commands, and `upgrade` lists the missions under `missions_needing_constitution_reconcile` without blocking. For each: `software-factory mission block --mission ID --reason "Constitution changed; reconciling governance" --next "Reconcile and re-accept scope"`; record the user's actual approval as an `exception` decision `{"id": "D-CONST-<first 8 hex>", "kind": "exception", "subject_hash": "<exact new constitution sha256>", "reference": "<who approved the constitution change, and where>"}` (a decision for a different hash is rejected); then `software-factory mission accept-scope --mission ID`. The scope decision must still match the specification, or record the user's amended acceptance first. The mission returns to PLANNED with tasks TODO, keeping attempts and history, and stores the new `constitution_version`. Restart implementation and recapture checks, results and reviews after reconciliation. Details: [reconciling in-flight missions](constitution-enforcement.md#reconciling-in-flight-missions).

Run tool tests, regenerate the selected profile, check for generation drift and run doctor. Validate skill frontmatter and references, then forward-test any substantial behavior change using realistic tasks in a temporary Git repository. Inspect actual artifacts and outcomes, not whether the agent repeats expected phrases.

Use independent review of state/evidence rules and generated wrappers. Test negative paths: changed source, skipped/failed checks, missing dependencies, changed generated files, interrupted work and exhausted retries. Apply [vendor smoke tests](../vendor-smoke-tests.md) when a client's configuration or instructions changed.

## Upgrade and rollback

`factory.lock.json` records generated ownership and source hashes. Commit an internally consistent set of canonical files, configuration, tests and active exports through the normal review process. Keep mission evidence pinned to the constitution/specification it examined.

For rollback, restore the reviewed factory version through normal Git workflow, inspect user changes, regenerate owned exports and rerun checks. Do not copy old evidence onto a new candidate or reset task attempts to bypass a limit. A change to schemas used by stored missions stops an upgrade until its data transition has been reviewed. New results use the indexed native format; verify schemas and negative paths with the Python package tests before release.

## Operational review

Review real missions for incorrect completion claims, repeated repairs, missing checks, human interventions and time spent reviewing. Record available usage metrics honestly; provider spending may be unavailable and is not enforced by skill text. Improve instructions in response to observed failures rather than accumulating universal rules for speculative scenarios.

Local validation, live vendor behavior and external GitHub/deployment controls have separate evidence. Maintain that distinction in release notes for the factory itself.

The verifier bounds command waiting and attempts process-group cleanup. A program that deliberately detaches a descendant can escape that group; this kit does not provide operating-system process containment. Run untrusted checks inside the product's existing isolated runner/container and inspect uncertain surviving processes before continuing.

Use the [registry extension contract](scaling-and-extensions.md#controlled-content-extensions) to add supported specialist roles, skills or entry prompts. New native vendor adapters still require code and official contract review.
