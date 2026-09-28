"""Fail-closed PreToolUse guard for the factory orchestrator (stdlib only, Python 3.11+).

The orchestrator coordinates; it never edits files. This hook denies file-editing tools
and any shell command outside a small read-only allowlist. It is a local guardrail, not
an attested control: records stay local-unattested and live client behaviour is not_run.

Usage: python -I -B orchestrator_guard.py   (Claude Code PreToolUse hook JSON on stdin)

Allow: exit 0 with no output, so the client's normal permission flow still applies (the
guard never grants a permission). Deny: exit 2, the documented deny JSON on stdout and
the reason on stderr. Any internal error or malformed input also denies.

Client contract, reviewed 2026-09-28:

Claude Code, https://code.claude.com/docs/en/hooks
  input: session_id, transcript_path, cwd, permission_mode, hook_event_name ("PreToolUse"),
    tool_name, tool_input, tool_use_id; agent_id ("Present only when the hook fires inside
    a subagent call") and agent_type ("Present when the session uses --agent or the hook
    fires inside a subagent"). Bash tool_input: {"command", "description", "timeout",
    "run_in_background"}; Write: {"file_path", "content"}; Edit: {"file_path",
    "old_string", "new_string"}; Agent (formerly Task): {"description", "prompt",
    "subagent_type", ...}.
  deny: {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision":
    "deny", "permissionDecisionReason": "..."}}; "exit 2 blocks whether or not you print
    JSON". Other non-zero exits and timeouts do not block, so this script catches every
    error and exits 2.
  Frontmatter hooks (https://code.claude.com/docs/en/sub-agents) "fire when the agent is
    spawned as a subagent ... and when the agent runs as the main session via --agent";
    project agent hooks run only after the workspace trust dialog is accepted.

The factory exports no Copilot hooks: Copilot preToolUse input carries no agent identity
(https://docs.github.com/en/copilot/reference/hooks-reference), so a repository hook would
also restrict implementer sessions.
"""

from __future__ import annotations

import json
import shlex
import sys

ORCHESTRATOR = "factory-orchestrator"
MAX_COMMAND = 262144
DELEGATE = (
    "The factory orchestrator coordinates and never edits files: brief a specialist with "
    "`software-factory mission brief` and delegate the change to factory-implementer "
    "(research/specs to factory-planner, checks to factory-verifier, review to factory-reviewer)."
)
SHELL_HELP = (
    "Allowed shell commands: `uv run --locked --project .factory software-factory ...`, "
    "`software-factory ...` (project commands only: no init, upgrade, uninstall, recover, render, auth "
    "or --root), read-only git (status, diff, log, show, rev-parse, ls-files, "
    "branch --show-current) and ls/cat/head/tail/wc/grep/rg/find without -exec or -delete; "
    "joined only by &&, ||, ;, | or newlines; stdin only from `< file` or a heredoc (quote the "
    "delimiter, <<'EOF', for text with $, backticks or backslashes); no output redirection or "
    "expansion."
)

# TodoWrite only updates the session task list and AskUserQuestion only asks the user; neither
# reads or writes project files or runs programs. Skill is not allowed:
# a skill can pre-approve tools and run shell expansions outside this guard's review.
CLAUDE_ALLOW = {"Read", "Glob", "Grep", "LS", "TodoWrite", "AskUserQuestion"}
# The Agent(...) frontmatter allowlist is ignored when the orchestrator itself runs as a
# subagent (https://code.claude.com/docs/en/sub-agents), so this guard is then the only barrier.
CLAUDE_DELEGATE = {"Agent", "Task"}
# Must match rendering.SPECIALISTS: the exported Claude agent names (.claude/agents/<name>.md).
SPECIALISTS = ("factory-planner", "factory-implementer", "factory-verifier", "factory-reviewer")
CLAUDE_EDIT = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
CLAUDE_SHELL = {"Bash"}

READ_ONLY = {"ls", "cat", "head", "tail", "wc", "grep", "rg", "find"}
GIT_READ = {"status", "diff", "log", "show", "rev-parse", "ls-files"}
# Git accepts unique prefixes of long options, so a prefix of these names is refused too.
GIT_FORBIDDEN = ("--output", "--ext-diff", "--textconv", "--exec", "--upload-pack", "--open-files-in-pager")
FIND_FORBIDDEN = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
FACTORY_PREFIX = ["uv", "run", "--locked", "--project", ".factory", "software-factory"]
# Project-scoped CLI commands. Setup/admin commands (init, upgrade, uninstall, recover, render,
# auth) and anything unknown are refused: they install, remove or regenerate files or handle
# credentials, which belongs to the user, not the orchestrator.
FACTORY_COMMANDS = {
    "version", "doctor", "inspect", "status", "checks", "verify", "gate", "packet",
    "mission", "models", "semantic", "jev", "triage",
}  # fmt: skip
FACTORY_NO_ARGS = {"version", "doctor", "inspect"}  # Read-only; any extra word could name a path.
FACTORY_HELP = {"-h", "--help"}


class Denied(Exception):
    pass


REDIRECT = "\x00<"  # Placeholder word for a stdin redirection; NUL never survives input checks.


def heredoc_delimiter(command: str, index: int) -> tuple[str, bool, int]:
    """Parse the delimiter word after << or <<-; return (word, quoted, next index)."""
    while index < len(command) and command[index] in " \t":
        index += 1
    word, quoted = [], False
    while index < len(command) and command[index] not in " \t\n;&|<>()":
        char = command[index]
        if char in "'\"":
            end = command.find(char, index + 1)
            if end < 0:
                raise Denied("unterminated quote in heredoc delimiter")
            word.append(command[index + 1 : end])
            quoted, index = True, end + 1
        elif char == "\\":
            if index + 1 >= len(command) or command[index + 1] == "\n":
                raise Denied("dangling escape in heredoc delimiter")
            word.append(command[index + 1])
            quoted, index = True, index + 2
        else:
            word.append(char)
            index += 1
    delimiter = "".join(word)
    if not delimiter or any(c in delimiter for c in "$`\n"):
        raise Denied("heredoc needs a plain delimiter word")
    return delimiter, quoted, index


def heredoc_bodies(command: str, index: int, pending: list[tuple[str, bool, bool]]) -> int:
    """Skip heredoc bodies (data, never commands) starting at index; return the index after them.

    Unquoted bodies would expand $, backticks and backslash-newline, so they must not contain
    those characters; quote the delimiter (<<'EOF') to pass arbitrary text.
    """
    for delimiter, strip_tabs, quoted in pending:
        while True:
            if index >= len(command):
                raise Denied(f"unterminated heredoc (missing {delimiter} line)")
            end = command.find("\n", index)
            line = command[index:] if end < 0 else command[index:end]
            index = len(command) if end < 0 else end + 1
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                break
            if not quoted and any(c in line for c in "$`\\"):
                raise Denied(
                    "unquoted heredoc bodies expand $, backticks and backslashes; quote the delimiter (<<'EOF')"
                )
    return index


def split_segments(command: str) -> list[str]:
    """Split on &&, ||, ;, | and newlines outside quotes; heredoc bodies are skipped as data.

    Refuses anything a shell would expand, any output redirection and any construct this
    scanner does not model. A stdin redirection (< word) becomes the REDIRECT placeholder.
    """
    if any(c in command for c in "\x00\r"):
        raise Denied("NUL or carriage-return characters are not allowed")
    if len(command) > MAX_COMMAND:
        raise Denied("shell command is too long to review")
    command = command.strip(" \t\n")
    if not command:
        raise Denied("empty shell command")
    segments, current, quote, pending, index, last_separator = [], [], None, [], 0, ""
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'" and command[index + 1 : index + 2] in ("\n", ""):
            raise Denied("line continuations and dangling escapes are not allowed")
        if quote == "'":
            quote = None if char == "'" else quote
            current.append(char)
        elif quote == '"':
            if char in "$`":
                raise Denied("shell expansion ($, backticks) is not allowed")
            if char == "\\":
                current.append(command[index : index + 2])
                index += 2
                continue
            quote = None if char == '"' else quote
            current.append(char)
        elif char in "'\"":
            quote = char
            current.append(char)
        elif char == "\\":
            current.append(command[index : index + 2])
            index += 2
            continue
        elif char in "$`":
            raise Denied("shell expansion ($, backticks, $(...)) is not allowed")
        elif char == "#" and (not current or current[-1][-1:].isspace()):
            raise Denied("shell comments are not allowed")
        elif char == ">" or command.startswith("&>", index):
            raise Denied("output redirection (>, >>, >|, &>) is not allowed")
        elif char == "<":
            if command.startswith("<<<", index):
                raise Denied("here-strings (<<<) are not allowed; use a quoted heredoc")
            if command.startswith("<<", index):
                strip_tabs = command.startswith("<<-", index)
                delimiter, quoted, index = heredoc_delimiter(command, index + (3 if strip_tabs else 2))
                pending.append((delimiter, strip_tabs, quoted))
                current.append(" ")
                continue
            if command[index + 1 : index + 2] in ("&", ">", "("):
                raise Denied("only plain stdin redirection (< file) is allowed")
            current.append(f" {REDIRECT} ")
        elif char in "(){}":
            raise Denied("subshells, grouping and brace expansion are not allowed")
        elif char in "*?[]":
            raise Denied("unquoted glob patterns are not allowed; quote patterns or use the Glob tool")
        elif char in "&|;\n":
            pair = command[index : index + 2]
            if pair in ("&&", "||"):
                index += 2
            elif pair == "|&":
                raise Denied("|& is not allowed")
            elif char == "&":
                raise Denied("background jobs (&) are not allowed")
            else:
                index += 1
            if char == "\n" and pending:
                index = heredoc_bodies(command, index, pending)
                pending = []
            segments.append("".join(current))
            current, last_separator = [], char
            continue
        else:
            current.append(char)
        index += 1
    if quote:
        raise Denied("unterminated quote in shell command")
    if pending:
        raise Denied("heredoc without a body")
    if current or last_separator != "\n":  # A heredoc body may end the command.
        segments.append("".join(current))
    if any(not segment.strip() for segment in segments):
        raise Denied("empty command in a shell chain")
    return segments


def check_factory(args: list[str]) -> None:
    """Allow only project-scoped software-factory commands on the current project."""
    for arg in args:
        name = arg.split("=", 1)[0]
        # --root (or an abbreviation argparse might accept) would select another project.
        if name.startswith("--ro") and "--root".startswith(name):
            raise Denied(
                "software-factory --root is not allowed; the orchestrator works on this project only"
            )
    if not args:
        raise Denied("software-factory needs a command")
    command, rest = args[0], args[1:]
    if command in FACTORY_HELP or command == "--version":
        if rest:
            raise Denied(f"software-factory {command} takes no further arguments")
        return
    if command not in FACTORY_COMMANDS:
        raise Denied(f"software-factory `{command}` is not an allowed orchestrator command")
    if command in FACTORY_NO_ARGS and any(arg not in FACTORY_HELP for arg in rest):
        raise Denied(
            f"software-factory {command} takes no arguments here (a path could target another project)"
        )


def check_words(words: list[str]) -> None:
    program, args = words[0], words[1:]
    if words[: len(FACTORY_PREFIX)] == FACTORY_PREFIX:
        check_factory(words[len(FACTORY_PREFIX) :])
        return
    if program == "software-factory":
        check_factory(args)
        return
    if program == "git":
        if args == ["branch", "--show-current"]:
            return
        if not args or args[0] not in GIT_READ:
            shown = " ".join(words[:2])
            raise Denied(f"`{shown}` is not an allowed read-only git command")
        for arg in args[1:]:
            name = arg.split("=", 1)[0]
            if name.startswith("--") and any(
                name.startswith(bad) or (len(name) > 3 and bad.startswith(name)) for bad in GIT_FORBIDDEN
            ):
                raise Denied(f"git option {name} can write files or run programs")
        return
    if program in READ_ONLY:
        if program == "find":
            bad = sorted(FIND_FORBIDDEN.intersection(args))
            if bad:
                raise Denied(f"find {bad[0]} can modify files or run programs")
        if program == "rg" and any(a == "--pre" or a.startswith("--pre=") for a in args):
            raise Denied("rg --pre runs programs")
        return
    raise Denied(f"`{program}` is not an allowed orchestrator command")


def check_shell(command) -> None:
    if not isinstance(command, str):
        raise Denied("shell tool input has no command string")
    for segment in split_segments(command):
        try:
            words = shlex.split(segment, posix=True)
        except ValueError as exc:
            raise Denied(f"unparseable shell command: {exc}") from exc
        plain, index = [], 0
        while index < len(words):
            if words[index] == REDIRECT:
                if index + 1 >= len(words) or words[index + 1] == REDIRECT:
                    raise Denied("stdin redirection without a file")
                if words[index + 1].startswith(("/dev/tcp/", "/dev/udp/")):
                    raise Denied("network redirection is not allowed")
                index += 2  # The file is only read as standard input.
                continue
            plain.append(words[index])
            index += 1
        if not plain:
            raise Denied("empty command in a shell chain")
        check_words(plain)


def decide(payload) -> None:
    """Return normally to allow; raise Denied to block."""
    if not isinstance(payload, dict):
        raise Denied("hook input is not a JSON object")
    agent_id, agent_type = payload.get("agent_id"), payload.get("agent_type")
    if agent_id is not None or agent_type is not None:
        if not isinstance(agent_type, str) or (agent_id is not None and not isinstance(agent_id, str)):
            raise Denied("malformed agent_id/agent_type in hook input")
        if agent_id and agent_type != ORCHESTRATOR:
            return  # A delegated specialist's own tool call; its agent file scopes its tools.
    if payload.get("hook_event_name", "PreToolUse") != "PreToolUse":
        raise Denied("orchestrator guard only handles PreToolUse")
    tool, args = payload.get("tool_name"), payload.get("tool_input")
    if not isinstance(tool, str) or not tool:
        raise Denied("hook input has no tool name")
    if tool in CLAUDE_ALLOW:
        return
    if tool in CLAUDE_DELEGATE:
        subagent = args.get("subagent_type") if isinstance(args, dict) else None
        if subagent not in SPECIALISTS:
            shown = subagent if isinstance(subagent, str) else "a missing or malformed subagent_type"
            raise Denied(f"{tool} may only start {', '.join(SPECIALISTS)}, not {shown}. {DELEGATE}")
        return
    if tool in CLAUDE_EDIT:
        raise Denied(f"{tool} edits files. {DELEGATE}")
    if tool in CLAUDE_SHELL:
        if not isinstance(args, dict):
            raise Denied("shell tool input is not an object")
        try:
            check_shell(args.get("command"))
        except Denied as exc:
            raise Denied(f"{exc}. {SHELL_HELP} {DELEGATE}") from exc
        return
    raise Denied(f"tool {tool} is not on the orchestrator allowlist (fail closed). {DELEGATE}")


def deny_output(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def main(argv=None, stdin=None, stdout=None, stderr=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    try:
        if argv:
            raise Denied("usage: orchestrator_guard.py (hook JSON on stdin, no arguments)")
        raw = stdin.read(1 << 20)
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise Denied("hook input is not valid JSON") from exc
        decide(payload)
        return 0
    except Denied as exc:
        reason = f"software-factory orchestrator guard: {exc}"
    except Exception as exc:  # noqa: BLE001 - fail closed on any internal error
        reason = f"software-factory orchestrator guard failed closed: {type(exc).__name__}"
    stdout.write(json.dumps(deny_output(reason)) + "\n")
    stderr.write(reason + "\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
