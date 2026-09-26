# holdouts-repo

The paired black-box acceptance repo for one product cell (final draft
§13.1 #4). Agents never get access to this repo — only `CODEOWNERS`
(`@po @sec`) can change `scenarios/`, and only the controller's separate,
read-only holdout identity can trigger and read its workflow runs.

```text
CODEOWNERS                       # PO + security only
scenarios/*.yaml                 # HTTP request -> expected status/body contains
runner/run_blackbox.py           # stdlib only; --staging-url -> holdout-result.json
runner/miniyaml.py               # the small YAML subset run_blackbox.py parses
.github/workflows/holdout.yml    # workflow_dispatch, uploads the result artifact
```

## Setting up the holdout runner's token

`WorkflowHoldoutRunner` (`factory-controller/src/factory/verification/
holdout.py`) drives `holdout.yml` over the GitHub REST API with its **own**
identity — never the push-bot or merge-bot credentials. That identity (a
GitHub App installation or a fine-grained PAT, scoped to this repo only)
needs:

- **Actions: write** — required to call `workflow_dispatch` on
  `holdout.yml`. This is also the level that grants the read access needed
  to poll the run's status and list/download its `holdout-result` artifact
  (GitHub exposes one combined `Actions` permission, not separate dispatch
  and read scopes).

No other permission is needed on this repo — in particular, no `contents`
access. The runner never reads the scenario files themselves; it only
dispatches the workflow and reads back the `{"passed", "total"}` JSON the
run's own steps produce.

## `run-name` and `correlation_id`

`workflow_dispatch` doesn't hand back a run id, so the caller polls by
matching on the run's name instead: `holdout.yml` takes a required
`correlation_id` input and sets `run-name: holdout ${{ inputs.correlation_id
}}` exactly. Whatever value the controller dispatches with, it should expect
to find that exact string as the triggered run's name.
