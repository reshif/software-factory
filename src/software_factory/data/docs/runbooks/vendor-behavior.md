# Verified vendor behavior

Official pages opened on **2026-09-28** before authoring the roles, skills and runbooks. This record describes documented behavior, not a claim that three installed clients or accounts were exercised. Recheck documentation and the live smoke tests after client upgrades.

## Supported client and harness map

This table is the factory's maintained compatibility boundary. A model provider does not identify the client harness. “Supported” means this repository supplies an adapter and checks; account access and interactive behavior still require the [live smoke tests](../vendor-smoke-tests.md).

| Actual client/session | Factory profile | Model harness identifier | Factory entry |
| --- | --- | --- | --- |
| Anthropic Claude Code, including its native VS Code extension | `claude` | `claude-code-native` | `/factory-build`; `/factory-models` |
| OpenAI's native Codex IDE extension or native Codex CLI | `codex` | `codex-native` | `$factory-build`; `$factory-models` |
| VS Code Copilot Chat, Local harness | `copilot` | `copilot-local` | Select `factory`, then `/factory-build` or `/factory-models` |
| VS Code Copilot on Copilot Agent Host | `copilot` | `copilot-agent-host` | Select `factory`, then `/factory-build` or `/factory-models` |
| VS Code's separately integrated OpenAI Codex harness on Agent Host | None | Unsupported target | Do not apply the native Codex factory profile's configuration assumptions |
| VS Code's separately integrated Claude harness, cloud coding agents, or another editor integration | None | Not validated by these adapters | Establish a reviewed adapter and run its native smoke tests before claiming compatibility |

OpenAI documents its own [Codex IDE extension](https://learn.chatgpt.com/docs/codex/ide). VS Code separately documents [harness-specific customization](https://code.visualstudio.com/docs/agents/guides/customize-copilot-guide) and [harness selection](https://code.visualstudio.com/docs/agents/run/agent-harnesses). Recheck this table when those integration contracts change.

## Claude Code

`CLAUDE.md` can import repository guidance with `@AGENTS.md`. Explicit imports remain useful when client configuration or older versions differ in direct `AGENTS.md` discovery. This factory keeps imports inside the repository. Instruction files are context, not a permission boundary. [Official instruction loading](https://code.claude.com/docs/en/memory).

Project skills use `.claude/skills/NAME/SKILL.md`. The portable workflow exports use `name` and `description` frontmatter. Four entry prompts additionally use `argument-hint` and `user-invocable: true`, exposing `/factory-build`, `/factory-blueprint`, `/factory-resume` and `/factory-status`. They retain normal automatic discovery and main-session context. Verify skill discovery in the actual client. [Official skill documentation](https://code.claude.com/docs/en/skills).

Custom roles use `.claude/agents/*.md`. Ordinary custom subagents start with separate context; the parent must supply task context. Preloaded skills and tool lists affect capability, but inherited runtime settings still matter. Forked sessions behave differently. This design delegates to explicit specialist definitions and never assumes the parent conversation reached them. [Official subagent documentation](https://code.claude.com/docs/en/sub-agents).

## Native Codex

Codex builds an instruction chain from global guidance and repository files toward the working directory. Overrides, nested files and the configured size limit can affect the result. The generated root `AGENTS.md` carries the common constitution; diagnostics and a fresh session must verify the effective guidance. [Official instruction discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

Repository skills are discovered from `.agents/skills` along the repository path. Duplicate names are not merged, so this factory exports each active vendor's skills only into that vendor's own tree and rejects unowned same-name copies. [Official skill discovery](https://learn.chatgpt.com/docs/build-skills).

The four entry skills use `$factory-build`, `$factory-blueprint`, `$factory-resume` and `$factory-status`. Each includes `agents/openai.yaml` with an example `interface.default_prompt`; native skill invocation works independently of whether a particular client prefills that text. Normal automatic discovery is preserved. The factory does not install user-global custom prompts, which are deprecated in favor of skills. [Official skill metadata](https://learn.chatgpt.com/docs/build-skills), [custom-prompt migration guidance](https://learn.chatgpt.com/docs/custom-prompts).

Current native custom-agent files live under `.codex/agents/*.toml` and require `name`, `description` and `developer_instructions`. Parent runtime overrides can affect child permissions even when a role supplies defaults. Do not infer read-only enforcement from a role's label. The installed Codex version must accept the generated schema before operational use. The generated `.codex/config.toml` sets `[agents] max_concurrent_threads_per_session` (current documentation; `max_threads` is its legacy alias); older Codex clients may ignore it, and no minimum version has been verified. [Official subagent configuration](https://learn.chatgpt.com/docs/agent-configuration/subagents).

## GitHub Copilot in VS Code

Instruction discovery depends on the selected harness/session target. Copilot guidance uses `.github/copilot-instructions.md`; custom instructions do not govern inline completions. Inspect the actual customization diagnostics instead of assuming a file was loaded. [Official VS Code instruction documentation](https://code.visualstudio.com/docs/agent-customization/custom-instructions).

Copilot custom agents use `.github/agents/*.agent.md`. Native tool names and availability matter; unavailable tools can be ignored. Skill discovery supports `.github/skills` with `SKILL.md` names matching their directory. This factory uses portable skill content and checks generated configuration, then requires interactive tests of role discovery and tool behavior. [Official custom agents](https://code.visualstudio.com/docs/agent-customization/custom-agents), [official skills](https://code.visualstudio.com/docs/agent-customization/agent-skills).

Four entry skills expose `/factory-build`, `/factory-blueprint`, `/factory-resume` and `/factory-status`, with `argument-hint` and `user-invocable: true`. Select the `factory` custom agent before invocation; skill metadata does not bind an agent as prompt-file metadata can. No `context: fork` or automatic-discovery override is emitted. Prompt files are deprecated and not loaded by Agent Host, so this factory uses skills and installs no `.github/prompts` files. Existing prompt-name collisions are rejected to preserve Local-client customizations. [Official skill invocation](https://code.visualstudio.com/docs/agent-customization/agent-skills), [prompt-file compatibility](https://code.visualstudio.com/docs/agent-customization/prompt-files).

Copilot also discovers `.claude/skills` and `.agents/skills`, and Claude-format agents under `.claude/agents`, and no supported setting restricts that. When Copilot is active together with Claude or Codex, the factory therefore exports no `.github/skills` tree; Copilot reads the other trees and may list a same-named skill once per tree. Same-named entries are equivalent exports of one canonical source: the workflow skills are identical copies, while entry-skill frontmatter (`argument-hint`/`user-invocable` in `.claude/skills`, `agents/openai.yaml` in `.agents/skills`) and agent file formats differ per vendor. Because Copilot also lists Claude-format agents and duplicate-name resolution is undocumented, Copilot's specialists are exported as `factory-copilot-<role>` whenever Claude Code is active too, and the `factory` agent delegates to those names. Known limit: the Claude-format `factory-<role>` agents remain visible and pickable in Copilot; they are Claude Code definitions, not Copilot's. `.github/copilot-instructions.md` is a short addendum that points at the shared `AGENTS.md` instead of repeating the constitution. Whether Copilot co-loads `AGENTS.md` automatically is recorded in the smoke tests, not assumed. With `claude` and `copilot` both active, clients that read both files (Copilot CLI; VS Code with `chat.useClaudeMdFile`) can load `AGENTS.md` directly and again through `CLAUDE.md`'s `@AGENTS.md` import; the files differ, so identical-file de-duplication does not apply. [Official skill discovery](https://code.visualstudio.com/docs/agent-customization/agent-skills).

The integrated Codex harness inside VS Code is distinct from the native Codex profile targeted here. Its documented `.github/agents` integration does not apply the `tools` field as an allowlist. Selecting a Claude model within Copilot likewise does not make the session Claude Code. Record the actual extension and harness during validation. [Official harness-specific setup](https://code.visualstudio.com/docs/agents/guides/customize-copilot-guide).

Copilot CLI and the Copilot cloud agent are unsupported targets. Copilot CLI nonetheless auto-discovers `AGENTS.md`, `CLAUDE.md`, `.github/agents`, `.claude/agents` and all skill trees in the repository, so factory files are visible there without an adapter. The cloud agent's custom-agent reference does not list the `agents` key used by the generated `factory` agent. [Copilot CLI instructions](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions), [custom agent configuration](https://docs.github.com/en/copilot/reference/custom-agents-configuration).

## Shared implementation decisions and limits

The architecture uses one main session orchestrator that briefs, inspects and records but does not produce, four specialist roles and file-based handoff between vendors. One or more vendor profiles can be exported at once; the shared `AGENTS.md` lists each active client's entry invocation. The renderer no longer writes `.claude/settings.json`, so an existing user settings file is left alone. It does not automate a call from one extension into another. Model choices remain with the session/account; no model identifiers or paid invocation are assumed.

Role instructions constrain intended behavior. Tool/runtime controls constrain capabilities only to the extent the installed client enforces them. The local verifier runs configured processes using the host environment; terminal access can write files and reach available credentials. Keep the account's actual permission configuration under operator control.

Hooks are not needed for core execution. Verification is invoked explicitly, and CI is the independent remote check. Hooks and agent tool lists add opt-in guardrails for the orchestrator's "never produces" rule (below); sharing a file format does not prove equivalent runtime behavior, and each needs a live test in the selected client.

## Enforcement per client

The orchestrator must not write product files and specialists must not spawn nested agents. Exported specialist roles never receive an agent tool in any client. How far the orchestrator rule is enforced differs:

| Client | Orchestrator enforcement | Opt-in setting | Limits |
| --- | --- | --- | --- |
| Claude Code | Optional orchestrator agent `.claude/agents/factory-orchestrator.md`, started with `claude --agent factory-orchestrator`: tools `Agent(factory-planner, factory-implementer, factory-verifier, factory-reviewer), Read, Glob, Grep, Bash` and a frontmatter `PreToolUse` hook running `.factory/hooks/orchestrator_guard.py`, which denies Edit/Write/MultiEdit/NotebookEdit, WebFetch/WebSearch (research is delegated), spawning any agent other than the four factory specialists, and any Bash command outside a read-only and `software-factory` allowlist, including output redirection. The `software-factory` setup and admin commands (`init`, `upgrade`, `uninstall`, `recover`, `render`, `auth`) and `--root` are denied: a human runs setup. The orchestrator records state through `--input -` heredocs | `enforcement.claude_orchestrator_agent: true` | The `Agent(...)` allowlist applies only when the agent runs as the main thread with `claude --agent factory-orchestrator`; frontmatter hooks of a project agent run only after the workspace trust dialog is accepted, and `claude -p` sessions are not trusted. A plain `/factory-build` session is instruction-only. Live behavior: not run |
| GitHub Copilot | The `factory` agent is exported without the `edit` tool (read, search, web, execute, agent), plus instructions | None | Shell writes through `execute` remain instruction-only. The factory ships no Copilot hooks: `preToolUse` input carries no agent identity, so a guard would also block the implementer, and hook files are loaded outside the orchestrator too. Live behavior: not run |
| Native Codex | Instructions only | None | Codex asks users to "treat tool hooks as a useful guardrail, not a complete enforcement boundary"; the factory exports none |

Sources, opened 2026-09-28: [Claude subagents](https://code.claude.com/docs/en/sub-agents), [Claude hooks](https://code.claude.com/docs/en/hooks), [Copilot hooks reference](https://docs.github.com/en/copilot/reference/hooks-reference) (why none are shipped), [Codex hooks](https://learn.chatgpt.com/docs/hooks). The guard itself is unit-tested offline; that is not a live client test. Inspect the rendered agent and hook configuration against these limits before relying on them.

Local render/schema/unit tests establish file generation and utility behavior. They do not prove authenticated vendor access, model reasoning quality, GUI discovery, GitHub branch protection, deployment authority or production recovery. Record those separately in [vendor smoke tests](../vendor-smoke-tests.md).
