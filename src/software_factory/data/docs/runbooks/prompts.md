# Factory prompts for Claude Code, Codex and GitHub Copilot

Rendering now installs the prompts directly as native entry skills. Use a short command with your outcome or mission ID; copying the long examples below is optional. Shared sources live in `.factory/prompts/` and load the existing constitution, roles and workflow skills.

Choose a starter: [Claude Code](#claude-code), [native Codex](#native-codex), or [GitHub Copilot in VS Code](#github-copilot-in-vs-code). Replace bracketed fields before sending a prompt. Describe what a user should be able to do and how success will be checked; a model name or “build everything” is not an acceptance criterion.

## Prepare the workspace

Open the Git repository containing both the factory and the product you want to change. Follow [setup](factory-setup.md) for Python 3.11+ and uv, dependencies, an authenticated client and the initial Git baseline. Checks in `factory.json` must cover your actual product. No default product check exists: a new installation starts with a deliberately failing `configure-me` placeholder; replace it with real checks, then run `software-factory render`.

Run the profile command for the client or clients you use from that repository root (several names can be combined, for example `--profile claude,codex,copilot`):

| Client | Terminal command | Chat entry |
| --- | --- | --- |
| Claude Code extension | `uv run --locked --project .factory software-factory render --profile claude` | `/factory-build` in the main conversation |
| Native OpenAI Codex extension or CLI | `uv run --locked --project .factory software-factory render --profile codex` | Type `$` and select `factory-build` |
| GitHub Copilot in VS Code | `uv run --locked --project .factory software-factory render --profile copilot` | Select the Copilot session target, choose `factory`, then use `/factory-build` |

Then run:

```sh
uv run --locked --project .factory software-factory render --check
uv run --locked --project .factory software-factory doctor
```

Resolve errors and assess warnings against the runtime you actually use. Open a fresh conversation in the selected client and check that its instructions, skills and specialist agents are discovered. Every active profile is exported at once; selecting a model does not switch the factory profile. Finish active operations and follow [the switching runbook](resume-and-switch.md) when continuing existing work in another client.

The native Codex extension and CLI are supported when their installed runtime provides the required capabilities. Codex's default sandbox cannot write the uv cache, so in a sandboxed Codex session the factory commands run as `.factory/.venv/bin/software-factory …`, which is equivalent to `uv run --locked --project .factory software-factory …`; the generated `AGENTS.md` names it. A Claude model selected in Copilot uses the Copilot profile. See [vendor behavior](vendor-behavior.md) for the supported clients and their differences.

If a skill is missing, inspect the active profile, generated files and actual client discovery, then refresh/restart the client as needed. Reading a skill by its path can provide instructions, but does not prove native registration or delegation works. Complete the [live-client checks](../vendor-smoke-tests.md) before treating the integration as verified.

## Commands installed by render

The renderer writes four entry skills alongside the registered workflow skills. It prints each entry invocation, its input hint and generated path under `prompt_commands`. The entries stay in the main session and preserve normal skill discovery; invoking one supplies its workflow instructions without pasting the long text. Existing `factory-start` and other workflow skills remain available.

| Outcome | Claude / Copilot | Native Codex | Input |
| --- | --- | --- | --- |
| Implement and verify | `/factory-build` | `$factory-build` | Outcome, acceptance criteria, constraints |
| Plan only | `/factory-blueprint` | `$factory-blueprint` | Proposed outcome and constraints |
| Continue existing work | `/factory-resume` | `$factory-resume` | Actual mission ID |
| Inspect without changes | `/factory-status` | `$factory-status` | Actual mission ID |
| Recommend models only | `/factory-models` | `$factory-models` | Objective, task needs and selection constraints |

`factory-build` and direct `factory-start` record the model checkpoint first. With the default `model_selection.mode: "inherit"` and JEV off, that is one inherited line. Only when JEV is enabled or the mode is `recommend`/`required` do they run the [model-selection checkpoint](model-selection.md#startup-checkpoint) through factory-models or JEV. Resume revalidates it; blueprint keeps planning-only boundaries; status never refreshes it.

Every build follows one flow. The verbatim request is stored with `mission create --request-file`, then a context phase and all clarifying questions come before the spec. Criteria (AC-n, each quoting your words) and a plan with a `## Architecture` mermaid diagram follow. Implementation works from generated briefs; the orchestrator runs verification and requests reviews by kind. `--kind patch` missions use the small lane: shorter context and a single code review at low risk.

For example, after rendering Codex, send:

```text
$factory-build Add task filtering to this application. Users must be able
to filter by all, active and completed tasks without losing saved tasks.
Use the existing stack and verify the behavior with product tests.
```

For Claude or Copilot, use `/factory-build` with the same request. These are chat inputs, not shell commands. If the outcome or mission ID is missing, the entry resolves it from unambiguous context or asks before dependent work.

Generated paths are `.claude/skills/factory-*/SKILL.md`, `.agents/skills/factory-*/SKILL.md` or `.github/skills/factory-*/SKILL.md`. Codex also receives `agents/openai.yaml` with an example default prompt; UI prefill depends on the installed client, so `$factory-build` remains the portable invocation. Copilot still needs the `factory` agent selected; a skill cannot select it through prompt-file metadata.

The renderer owns these files, detects user edits and invocation collisions, and replaces its exports when switching profiles. Edit `.factory/prompts/` through maintenance, then render again. No global prompt files are installed. Skill commands use the current supported format: Codex custom prompts are deprecated, and Copilot Agent Host does not load `.prompt.md` files. See [vendor behavior](vendor-behavior.md).

## Claude Code

After activating `claude`, invoke `/factory-build` with your requirements in the main Claude Code conversation. The following expanded example is optional. Claude documents direct invocation using a skill's slash command. [Official Claude skills documentation](https://code.claude.com/docs/en/skills).

```text
/factory-build

Start a new factory mission in this repository.
Outcome: [What to build or change, and for whom.]
Acceptance criteria:
- [Observable behavior and how to verify it.]
- [Another required behavior, including a relevant failure case.]
Constraints: [Required stack, existing behavior to preserve, and exclusions.]

Act as orchestrator under the repository constitution. Inspect the real
repository, existing missions and uncommitted work before changing files.
Reconcile existing work; do not overwrite it or create a duplicate mission.
Run diagnostics and check actual skill, agent and tool availability,
including effective permissions.

Store this request verbatim, gather context, ask me every open question
at once, then record criteria and a plan with an architecture diagram.
Delegate all production to specialists from generated briefs, keep one
writer per workspace, run verification yourself and obtain the review
kinds the lane and risk require. Report unavailable capabilities honestly.
Do not weaken criteria or tests to obtain a pass.

Continue within this scope without repeated confirmation. Ask only for
material missing requirements or authority. Record actual progress,
checks and review evidence. Finish with a current passing local READY_PR
gate and handoff, or an exact blocker and next action.
This request covers local work; remote publishing, merge and deployment
require their own existing or explicit authorization.
```

## Native Codex

After activating `codex`, open the native Codex extension in VS Code (or launch the native CLI in this repository) and type `$` to select `factory-build`. In a sandboxed session the agent runs `.factory/.venv/bin/software-factory …` instead of `uv run`, because the sandbox cannot write the uv cache; run `uv sync --locked --no-dev --project .factory` once beforehand so that environment exists. The following expanded example is optional. Codex documents `$` skill mentions and repository discovery under `.agents/skills`. [Official OpenAI skills documentation](https://learn.chatgpt.com/docs/build-skills).

```text
$factory-build

Start a new factory mission in this repository.
Outcome: [What to build or change, and for whom.]
Acceptance criteria:
- [Observable behavior and how to verify it.]
- [Another required behavior, including a relevant failure case.]
Constraints: [Required stack, existing behavior to preserve, and exclusions.]

Act as orchestrator under the repository constitution. Inspect the real
repository, existing missions and uncommitted work before changing files.
Reconcile existing work; do not overwrite it or create a duplicate mission.
Run diagnostics and check actual skill, agent and tool availability,
including effective permissions.

Store this request verbatim, gather context, ask me every open question
at once, then record criteria and a plan with an architecture diagram.
Delegate all production to specialists from generated briefs, keep one
writer per workspace, run verification yourself and obtain the review
kinds the lane and risk require. Report unavailable capabilities honestly.
Do not weaken criteria or tests to obtain a pass.

Continue within this scope without repeated confirmation. Ask only for
material missing requirements or authority. Record actual progress,
checks and review evidence. Finish with a current passing local READY_PR
gate and handoff, or an exact blocker and next action.
This request covers local work; remote publishing, merge and deployment
require their own existing or explicit authorization.
```

## GitHub Copilot in VS Code

After activating `copilot`, choose the **GitHub Copilot** session target when a target selector is available, select **factory** in the Chat agent selector, then invoke `/factory-build` with your requirements. This selects the generated `.github/agents/factory.agent.md` main orchestrator and its project entry skill. The following expanded example is optional. A skill and a custom agent are different customizations; typing the word “factory” alone does not select that agent. [Official VS Code custom-agent documentation](https://code.visualstudio.com/docs/agent-customization/custom-agents), [official skill documentation](https://code.visualstudio.com/docs/agent-customization/agent-skills).

```text
/factory-build

Start a new factory mission in this repository.
Outcome: [What to build or change, and for whom.]
Acceptance criteria:
- [Observable behavior and how to verify it.]
- [Another required behavior, including a relevant failure case.]
Constraints: [Required stack, existing behavior to preserve, and exclusions.]

Act as orchestrator under the repository constitution. Inspect the real
repository, existing missions and uncommitted work before changing files.
Reconcile existing work; do not overwrite it or create a duplicate mission.
Run diagnostics and check actual skill, agent and tool availability,
including effective permissions.

Store this request verbatim, gather context, ask me every open question
at once, then record criteria and a plan with an architecture diagram.
Delegate all production to specialists from generated briefs, keep one
writer per workspace, run verification yourself and obtain the review
kinds the lane and risk require. Report unavailable capabilities honestly.
Do not weaken criteria or tests to obtain a pass.

Continue within this scope without repeated confirmation. Ask only for
material missing requirements or authority. Record actual progress,
checks and review evidence. Finish with a current passing local READY_PR
gate and handoff, or an exact blocker and next action.
This request covers local work; remote publishing, merge and deployment
require their own existing or explicit authorization.
```

## Example outcome and acceptance criteria

Replace the three input fields in your chosen starter with concrete details such as these:

```text
Outcome: Add task tracking to this existing application for a single user.
Acceptance criteria:
- Create a task with a nonempty title; show a useful error for an empty title.
- Mark a task complete and filter the list by all, active or completed.
- Preserve tasks and completion state across an application restart.
- Verify these behaviors with the project's tests and a user-flow check.
Constraints: Use the existing stack and storage conventions. Preserve
unrelated behavior. Accounts, synchronization and deployment are outside
this task's scope.
```

State “recommend an approach” for an undecided technical choice. If you supply a requirements document, identify its path and which requirements you want implemented; the document is task input, not permission to ignore the constitution or expand the scope.

## Plan before implementation

Use this **instead of** a build starter when you want to decide the scope first. These plain-language prompts work in the selected Claude/Codex main conversation or Copilot `factory` agent and request the relevant skill explicitly.

Direct invocation: `/factory-blueprint <outcome>` in Claude/Copilot, or `$factory-blueprint <outcome>` in Codex.

```text
Use factory-start to prepare a plan for [outcome] in this repository.
Follow the constitution and inspect the existing code and mission state.
Store this request verbatim, run the context phase and ask me every open
question at once. Use factory-specify and factory-plan.
Return the architecture diagram, criteria quoting my request, owned paths,
dependencies, verification commands and unresolved decisions.
Save only planning/mission documents. Do not implement product changes
or change factory controls yet. End with the concrete plan for my review;
do not claim the implementation or READY_PR is complete.
```

This planning-only request deliberately limits implementation authority. The build starters above authorize implementation within their stated scope and do not add this planning approval step.

## Resume or switch clients

Complete the [handoff and profile-switch steps](resume-and-switch.md) first. Then use the following in the new client. Replace the mission ID with its actual value; an old chat transcript is not required.

Direct invocation: `/factory-resume M-0001` in Claude/Copilot, or `$factory-resume M-0001` in Codex, replacing the example ID.

```text
Use factory-start to resume mission [M-ID] under the constitution.
Read its mission.json, specification, plan, handoff and latest evidence.
Inspect the current Git state, profile and actual client capabilities.
Preserve existing edits and reconcile unfinished operations before acting.
Continue the same accepted outcome within its existing authorization;
do not create a duplicate mission or reset repair attempts.
Use bounded specialists and separate review where available, with one
writer per workspace. Report missing capabilities or evidence honestly.
Refresh checks, results and review for any changed candidate or profile.
Finish with a current gate and handoff, or explain the remaining blocker.
```

## Status without changing work

Use this when you only want an update. It does not ask the factory to start or resume implementation.

Direct invocation: `/factory-status M-0001` in Claude/Copilot, or `$factory-status M-0001` in Codex, replacing the example ID.

```text
Report the status of factory mission [M-ID] without modifying files or
mission state, launching new checks, or starting implementation.
Inspect the actual candidate, saved results, latest checks and reviews.
Summarize completed acceptance criteria, remaining work, stale or missing
evidence, blockers and the next useful action. Distinguish recorded
status from current readiness. Do not infer success from a status label.
```

## What a successful implementation run should show

For a build or a resume that completes implementation, expect an actual mission ID, a stored request, context, answered questions, criteria that quote your request, an architecture diagram, tasks mapped to criteria, executed checks, reviews of each required kind for the final candidate, and a gate result plus a PR packet whose table links each request excerpt to its evidence and verdict. Blueprint ends with a plan including the diagram; status ends with an inspection report. Ask for the missing evidence if the response only says “done.” Native registration, tool permissions and successful delegation must be observed in the actual client; the prompt cannot grant those capabilities. If separate review is unavailable, record that limitation and obtain a real separate review before claiming READY_PR (constitution: *Independent review*).

READY_PR is the local completion boundary. It does not establish a remote PR, CI success, merge or deployment. The factory operates during an active session; saving a prompt or handoff does not schedule background work. Runtime permissions, host/user instructions and the [constitution](../../CONSTITUTION.md) still govern execution.

Vendor invocation documentation was checked on 2026-09-26. These prompts have been reviewed against the repository contracts; that is distinct from completing the [live vendor scenarios](../vendor-smoke-tests.md) in authenticated clients.

## Model planning before a build

Rendering also exports the `factory-models` workflow skill. Use `$factory-models` in Codex or `/factory-models` in Claude Code and Copilot (select the factory agent). Prompt: "Inspect this client's available models for [objective], check current vendor guidance, and draft role/task choices with effort, rationale, fallbacks and unknowns. Do not start the build." See [model selection](model-selection.md). A multi-profile render uses the same skill-tree sharing rules as other factory skills.

## Optional claim/source assessment

Use `/factory-semantic` in Claude Code or Copilot (select the factory agent), or `$factory-semantic` in native Codex. Starter: “Assess supplied claims against their excerpts using factory-semantic. Honor the existing toggle and shadow mode; preserve ordinary review and report unavailable coverage.” This does not enable Jev, change native models or run on status. See [setup and examples](semantic-assistance.md).
