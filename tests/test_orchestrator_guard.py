import importlib.util
import io
import json
import shutil
import subprocess
import sys

import pytest

from software_factory.core import asset_root

GUARD_PATH = asset_root() / "hooks/orchestrator_guard.py"
spec = importlib.util.spec_from_file_location("orchestrator_guard", GUARD_PATH)
guard = importlib.util.module_from_spec(spec)
_saved, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # Keep package data free of caches.
try:
    spec.loader.exec_module(guard)
finally:
    sys.dont_write_bytecode = _saved


def claude(tool, tool_input=None, **extra):
    payload = {
        "session_id": "s",
        "transcript_path": "/tmp/t.jsonl",
        "cwd": "/repo",
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input if tool_input is not None else {},
        "tool_use_id": "toolu_1",
    }
    payload.update(extra)
    return payload


def run(payload, argv=()):
    out, err = io.StringIO(), io.StringIO()
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    code = guard.main(list(argv), io.StringIO(raw), out, err)
    return code, out.getvalue(), err.getvalue()


def bash(command):
    return run(claude("Bash", {"command": command, "description": "x"}))


ALLOWED = [
    "uv run --locked --project .factory software-factory status",
    "uv run --locked --project .factory software-factory mission brief --mission M-1 --kind code",
    "software-factory status",
    "software-factory gate --mission M-1",
    "software-factory mission risk --mission 'M-1'",
    "software-factory version",
    "software-factory doctor",
    "software-factory inspect",
    "software-factory doctor --help",
    "software-factory --help",
    "software-factory --version",
    "uv run --locked --project .factory software-factory doctor",
    "uv run --locked --project .factory software-factory inspect",
    "uv run --locked --project .factory software-factory checks",
    "software-factory verify --mission M-1",
    "software-factory packet --mission M-1",
    "software-factory models --help",
    "software-factory semantic --help",
    "software-factory jev --help",
    "software-factory triage --help",
    "software-factory mission list",
    "software-factory mission transition --mission M-1 --to BLOCKED --reason 'no --root here'",
    "git status",
    "git status --porcelain",
    "git diff --stat HEAD~1",
    "git diff --no-ext-diff -- 'src/*.py'",
    "git log --oneline -5",
    "git show HEAD:factory.json",
    "git rev-parse HEAD",
    "git ls-files -- src",
    "git branch --show-current",
    "ls -la .factory",
    "cat factory.json",
    "head -n 20 README.md",
    "tail -n 5 .factory/missions/M-1/mission.json",
    "wc -l src/app.py",
    "grep -rn 'TODO' src",
    'grep -n "a b" file.txt',
    "rg -n 'def main' src",
    "rg --pretty foo",
    "find . -name '*.py' -type f",
    "git status && git diff --stat",
    "git log --oneline | head -5",
    "git status; software-factory status",
    "git diff --quiet || git status",
    "grep -c x file | wc -l",
    "grep 'a|b;c>d' file",
    "grep '$HOME `x` $(y)' file",
    "  git   status  ",
    "cat 'file with spaces.txt'",
    "grep \\; file",
]

DENIED = [
    ("echo hi > out.txt", "redirection"),
    ("cat a >> b", "redirection"),
    ("git diff | tee out.patch", "tee"),
    ("sed -i s/a/b/ file", "sed"),
    ("perl -i -pe 's/a/b/' file", "perl"),
    ("rm -rf build", "rm"),
    ("mv a b", "mv"),
    ("cp a b", "cp"),
    ("git apply x.patch", "git apply"),
    ("git commit -m x", "git commit"),
    ("git checkout -- file", "git checkout"),
    ("git reset --hard", "git reset"),
    ("git add .", "git add"),
    ("git push", "git push"),
    ("git branch -D main", "git branch"),
    ("git branch", "git branch"),
    ("git -c core.pager=sh log", "git -c"),
    ("git -C /tmp status", "git -C"),
    ("git diff --output=x.patch", "--output"),
    ("git log --out=x", "--out"),
    ("git diff --ext-diff", "--ext-diff"),
    ("git show --textconv HEAD", "--textconv"),
    ("curl -o x https://example.com", "curl"),
    ("wget https://example.com", "wget"),
    ('python -c \'open("x","w")\'', "python"),
    ("python3 -c pass", "python3"),
    ("node -e 1", "node"),
    ("bash -c 'git status'", "bash"),
    ("sh -c ls", "sh"),
    ("find . -delete", "-delete"),
    ("find . -name x -exec rm {} ;", "subshells"),
    ("find . -name x -exec rm '{}' \\;", "-exec"),
    ("find . -execdir ls ';'", "-execdir"),
    ("find . -fprint out", "-fprint"),
    ("find . '-del'ete", "-delete"),
    ("rg --pre=sh foo", "--pre"),
    ("rg --pre sh foo", "--pre"),
    ("git status && rm -rf x", "rm"),
    ("git status; rm x", "rm"),
    ("git status || rm x", "rm"),
    ("ls | xargs rm", "xargs"),
    ("cat $(echo file)", "expansion"),
    ("cat `echo file`", "expansion"),
    ('cat "$(rm x)"', "expansion"),
    ("cat $HOME/.ssh/id_rsa", "expansion"),
    ("(rm x)", "subshells"),
    ("{ rm x; }", "subshells"),
    ("ls {a,-R}", "brace"),
    ("ls *", "glob"),
    ("find . *", "glob"),
    ("git status &", "background"),
    ("git status |& cat", "|&"),
    ("git status\nrm x", "`rm`"),
    ("git status\rrm x", "carriage-return"),
    ("FOO=1 git status", "FOO=1"),
    ("GIT_EXTERNAL_DIFF=sh git diff", "GIT_EXTERNAL_DIFF"),
    ("env git status", "env"),
    ("/usr/bin/git status", "/usr/bin/git"),
    ("./software-factory status", "./software-factory"),
    ("uv run software-factory status", "`uv`"),
    ("uv run --locked --project other software-factory status", "`uv`"),
    *[
        (f"{prefix} {args}", fragment)
        for prefix in ("software-factory", "uv run --locked --project .factory software-factory")
        for args, fragment in [
            ("init", "`init`"),
            ("init /other/project --profile claude", "`init`"),
            ("upgrade", "`upgrade`"),
            ("upgrade ../other", "`upgrade`"),
            ("uninstall", "`uninstall`"),
            ("uninstall --dry-run", "`uninstall`"),
            ("recover", "`recover`"),
            ("recover --apply", "`recover`"),
            ("render", "`render`"),
            ("render --check", "`render`"),
            ("auth login anthropic", "`auth`"),
            ("auth status", "`auth`"),
            ("auth logout", "`auth`"),
            ("auth", "`auth`"),
            ("--root /other status", "--root"),
            ("--root=/other status", "--root"),
            ("status --root /other", "--root"),
            ("status --root=/other", "--root"),
            ("mission brief --mission M-1 --root ../x", "--root"),
            ("gate --mission M-1 --root=../x", "--root"),
            ("status --roo /other", "--root"),
            ("doctor /other/project", "no arguments"),
            ("inspect ../other", "no arguments"),
            ("version /other", "no arguments"),
            ("doctor --json", "no arguments"),
            ("", "needs a command"),
            ("--verbose status", "`--verbose`"),
            ("--version status", "no further arguments"),
            ("unknown", "`unknown`"),
            ("Status", "`Status`"),
        ]
    ],
    ("git status && software-factory init .", "`init`"),
    ("software-factory status | software-factory render", "`render`"),
    ("uv pip install x", "`uv`"),
    ("cd src && git status", "cd"),
    ("git status &&", "empty command"),
    ("; git status", "empty command"),
    ("cat 'unterminated", "unterminated"),
    ("", "empty"),
    ("   ", "empty"),
    ("cat file\\", "dangling"),
    ("tee out", "tee"),
    ("x" * 300000, "too long"),
]


@pytest.mark.parametrize("command", ALLOWED)
def test_allowed_shell_commands(command):
    code, out, err = bash(command)
    assert (code, out, err) == (0, "", ""), err


@pytest.mark.parametrize(("command", "fragment"), DENIED)
def test_denied_shell_commands(command, fragment):
    code, out, err = bash(command)
    assert code == 2
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    assert fragment in decision["permissionDecisionReason"]
    assert "factory-implementer" in decision["permissionDecisionReason"]
    assert err.strip() == decision["permissionDecisionReason"]


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
def test_claude_edit_tools_are_denied(tool):
    code, out, _ = run(claude(tool, {"file_path": "src/a.py", "content": "x"}))
    reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    assert code == 2 and "edits files" in reason and "factory-implementer" in reason


@pytest.mark.parametrize("tool", ["Read", "Glob", "Grep", "LS", "TodoWrite", "AskUserQuestion"])
def test_claude_read_and_coordination_tools_are_allowed(tool):
    assert run(claude(tool, {"file_path": "x"})) == (0, "", "")


def test_skill_tool_is_denied():
    code, out, _ = run(claude("Skill", {"skill": "factory-build"}))
    assert code == 2 and "fail closed" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("tool", ["Agent", "Task"])
@pytest.mark.parametrize("subagent", guard.SPECIALISTS)
def test_delegation_to_factory_specialists_is_allowed(tool, subagent):
    tool_input = {"description": "d", "prompt": "p", "subagent_type": subagent}
    assert run(claude(tool, tool_input)) == (0, "", "")
    # The orchestrator running as a subagent (Agent(...) allowlist ignored) is guarded the same way.
    assert run(claude(tool, tool_input, agent_type="factory-orchestrator", agent_id="a1")) == (0, "", "")


@pytest.mark.parametrize("tool", ["Agent", "Task"])
@pytest.mark.parametrize(
    "tool_input",
    [
        {"description": "d", "prompt": "p", "subagent_type": "general-purpose"},
        {"description": "d", "prompt": "p", "subagent_type": "fork"},
        {"description": "d", "prompt": "p", "subagent_type": "Explore"},
        {"description": "d", "prompt": "p", "subagent_type": "factory-orchestrator"},
        {"description": "d", "prompt": "p", "subagent_type": "factory-copilot-implementer"},
        {"description": "d", "prompt": "p", "subagent_type": "Factory-Implementer"},
        {"description": "d", "prompt": "p", "subagent_type": "factory-implementer "},
        {"description": "d", "prompt": "p", "subagent_type": ["factory-implementer"]},
        {"description": "d", "prompt": "p", "subagent_type": None},
        {"description": "d", "prompt": "p"},
        {},
        ["factory-implementer"],
        "factory-implementer",
    ],
)
def test_delegation_to_other_agents_is_denied(tool, tool_input):
    for extra in ({}, {"agent_type": "factory-orchestrator", "agent_id": "a1"}):
        code, out, err = run(claude(tool, tool_input, **extra))
        reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
        assert code == 2 and "may only start" in reason and "factory-implementer" in reason
        assert err.strip() == reason


def test_guard_specialists_match_the_rendered_claude_agents():
    from software_factory import rendering

    assert guard.SPECIALISTS == rendering.SPECIALISTS


@pytest.mark.parametrize("tool", ["WebFetch", "WebSearch", "PowerShell", "mcp__server__write", "Unknown"])
def test_unknown_or_unlisted_tools_fail_closed(tool):
    code, out, _ = run(claude(tool, {}))
    assert code == 2 and "fail closed" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        "[]",
        "null",
        json.dumps({"tool_input": {}}),
        json.dumps({"tool_name": 3}),
        json.dumps(claude("Bash", ["git status"])),
        json.dumps(claude("Bash", {"command": ["git", "status"]})),
        json.dumps(claude("Bash", {})),
        json.dumps(claude("Read", hook_event_name="PostToolUse")),
        json.dumps(claude("Read", agent_id="a1")),
        json.dumps(claude("Read", agent_id=5, agent_type="factory-implementer")),
    ],
)
def test_malformed_input_is_denied(raw):
    code, out, _ = run(raw)
    assert code == 2
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_main_session_agent_is_guarded_and_specialist_calls_are_not():
    # --agent main session: agent_type is present without agent_id.
    assert run(claude("Write", {}, agent_type="factory-orchestrator"))[0] == 2
    # The orchestrator spawned as a subagent is still guarded.
    assert run(claude("Write", {}, agent_type="factory-orchestrator", agent_id="a1"))[0] == 2
    # A delegated specialist's own call is scoped by its agent file, not by this guard.
    assert run(claude("Write", {}, agent_type="factory-implementer", agent_id="a2")) == (0, "", "")


def test_internal_error_fails_closed(monkeypatch):
    def boom(*_args):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(guard, "decide", boom)
    code, out, err = run(claude("Read"))
    assert code == 2 and "failed closed" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "failed closed" in err


def test_bad_arguments_deny():
    out, err = io.StringIO(), io.StringIO()
    assert guard.main(["--mode", "other"], io.StringIO(json.dumps(claude("Read"))), out, err) == 2
    assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] == "deny"
    # Copilot-shaped input is not supported and fails closed.
    copilot = {"sessionId": "s", "timestamp": 1, "toolName": "view", "toolArgs": "{}"}
    assert run(copilot)[0] == 2


def test_isolated_interpreter_runs_the_shipped_script():
    def call(payload, *args):
        return subprocess.run(
            [sys.executable, "-I", "-B", str(GUARD_PATH), *args],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    denied = call(claude("Write", {"file_path": "a", "content": "b"}))
    assert denied.returncode == 2 and "factory-implementer" in denied.stderr
    assert json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    allowed = call(claude("Bash", {"command": "software-factory status"}))
    assert (allowed.returncode, allowed.stdout) == (0, "")
    assert call(claude("Read"), "--mode", "copilot").returncode == 2


def test_guard_uses_only_the_standard_library():
    source = GUARD_PATH.read_text()
    imports = {line.split()[1] for line in source.splitlines() if line.startswith(("import ", "from "))}
    assert imports <= {"__future__", "json", "shlex", "sys"}


SF = "uv run --locked --project .factory software-factory"
CRITERIA = '{"items": [{"id": "AC-1", "text": "$HOME `x` > y; rm -rf / && echo \\\\ (z)"}]}'

STDIN_ALLOWED = [
    f"{SF} mission criteria --mission M --input - <<'EOF'\n{CRITERIA}\nEOF",
    f"{SF} mission criteria --mission M --input - <<'EOF'\n{CRITERIA}\nEOF\n",
    'software-factory mission record-doc --mission M --doc plan --input - <<"EOF"\n$(rm x)\n`rm y`\nEOF',
    "software-factory mission record-doc --mission M --doc plan --input - <<EOF\n# Plan\n\nplain text\nEOF",
    "software-factory mission record-doc --mission M --doc spec --input - << END_OF_DOC\nbody\nEND_OF_DOC",
    "software-factory mission clarify --mission M --input - <<-'EOF'\n\tindented > body\n\tEOF",
    "software-factory mission record-doc --mission M --doc plan --input - < /dev/stdin",
    "software-factory mission criteria --mission M --input - < .factory/local/criteria.json",
    "software-factory mission criteria --mission M --input - <'criteria file.json'",
    "cat criteria.json | software-factory mission criteria --mission M --input -",
    "cat < README.md | wc -l",
    f"{SF} mission record-doc --mission M --doc context --input - <<'EOF'\nrm -rf /\nEOF\ngit status",
    f"{SF} mission record-doc --mission M --doc handoff --input - <<'EOF' && git status\nbody\nEOF",
    f"{SF} mission record-doc --mission M --doc plan --input - <<'EOF'\nEOF \n EOF\nEOFX\nEOF",
    "git status\ngit diff --stat",
    "git status\n\ngit log",
    "git status;",
    "git status;\ngit log",
    "git log --format=#%h -1",
    "grep '# heading' README.md",
]

STDIN_DENIED = [
    (f"{SF} mission criteria --mission M --input - <<'EOF'\nbody\nEOF\nrm -rf x", "`rm`"),
    (f"{SF} mission criteria --mission M --input - <<'EOF' && rm x\nbody\nEOF", "`rm`"),
    (f"{SF} mission criteria --mission M --input - <<'EOF' | tee out\nbody\nEOF", "`tee`"),
    ("software-factory mission criteria --input - <<EOF\n$(rm x)\nEOF", "unquoted heredoc"),
    ("software-factory mission criteria --input - <<EOF\n`rm x`\nEOF", "unquoted heredoc"),
    ("software-factory mission criteria --input - <<EOF\nEO\\\nF\nrm x\nEOF", "unquoted heredoc"),
    ("software-factory mission criteria --input - <<'EOF'\nbody", "unterminated heredoc"),
    ("software-factory mission criteria --input - <<'EOF'\nbody\n EOF", "unterminated heredoc"),
    ("software-factory mission criteria --input - <<'EOF'", "heredoc without a body"),
    ("software-factory mission criteria --input - <<", "delimiter"),
    ("software-factory mission criteria --input - <<$X\nbody\n$X", "expansion"),
    ("software-factory mission criteria --input - <<'EOF\nbody\nEOF", "unterminated quote"),
    ("software-factory mission criteria --input - <<<'{}'", "here-strings"),
    ("software-factory mission criteria --input - > out.json", "output redirection"),
    ("software-factory status >> log", "output redirection"),
    ("git log >| out", "output redirection"),
    ("git log &> out", "output redirection"),
    ("git log 2>/dev/null", "output redirection"),
    ("git log 2>&1", "output redirection"),
    ("cat <> file", "plain stdin"),
    ("cat <&3", "plain stdin"),
    ("cat <(rm x)", "plain stdin"),
    ("cat <", "without a file"),
    ("cat < < file", "without a file"),
    ("cat < $FILE", "expansion"),
    ("cat < /dev/tcp/example.com/80", "network"),
    ("cat x | software-factory mission criteria --input - | tee y", "`tee`"),
    ("cat x | sh", "`sh`"),
    ("rm x < /dev/null", "`rm`"),
    ("python3 - <<'EOF'\nprint(1)\nEOF", "`python3`"),
    ("bash <<'EOF'\nrm -rf x\nEOF", "`bash`"),
    ("find . -de\\\nlete", "line continuations"),
    ('grep "a\\\nb" file', "line continuations"),
    ("git status # <<'EOF'\nrm -rf x\nEOF", "comments"),
    ("git status #x", "comments"),
    ("#x", "comments"),
]


@pytest.mark.parametrize("command", STDIN_ALLOWED)
def test_stdin_and_heredoc_input_is_allowed(command):
    code, out, err = bash(command)
    assert (code, out, err) == (0, "", ""), err


@pytest.mark.parametrize(("command", "fragment"), STDIN_DENIED)
def test_stdin_and_heredoc_abuse_is_denied(command, fragment):
    code, out, _ = bash(command)
    assert code == 2
    assert fragment in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.skipif(not shutil.which("bash"), reason="bash is required for the differential check")
@pytest.mark.parametrize("command", ALLOWED + STDIN_ALLOWED)
def test_bash_runs_only_allowlisted_programs_for_allowed_commands(tmp_path, command):
    """Differential check: bash, given only logging stubs, invokes nothing outside the allowlist."""
    stubs = tmp_path / "bin"
    stubs.mkdir()
    log = tmp_path / "calls.log"
    for name in ("uv", "software-factory", "git", *guard.READ_ONLY):
        stub = stubs / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n')
        stub.chmod(0o755)
    for name in ("README.md", "criteria.json", "file", "factory.json"):
        (tmp_path / name).write_text("x\n")
    (tmp_path / ".factory/local").mkdir(parents=True)
    (tmp_path / ".factory/local/criteria.json").write_text("{}\n")
    (tmp_path / "criteria file.json").write_text("{}\n")
    result = subprocess.run(
        [shutil.which("bash"), "--norc", "--noprofile", "-c", command],
        cwd=tmp_path,
        env={"PATH": str(stubs)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert "not found" not in result.stderr, result.stderr
    calls = log.read_text().splitlines() if log.exists() else []
    assert calls and all(c.split()[0] in {"uv", "software-factory", "git", *guard.READ_ONLY} for c in calls)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        [
            "bin",
            "calls.log",
            "README.md",
            "criteria.json",
            "file",
            "factory.json",
            ".factory",
            "criteria file.json",
        ]
    )


# 0.3.2: per-subcommand option allowlist, project-relative paths and hook input checks.
VENV = ".factory/.venv/bin/software-factory"

OPTION_ALLOWED = [
    f"{VENV} status",
    f"{VENV} mission brief --mission M-1 --kind code",
    f"{VENV} semantic check --input - --mission M-1 <<'JSON'\n{{}}\nJSON",
    "software-factory semantic verify-claims --input - --no-network <<'JSON'\n{}\nJSON",
    "software-factory jev check --input .factory/local/claims.json --no-cache --no-persist",
    "software-factory models plan --input - --catalog .factory/local/models/catalog.json <<'JSON'\n{}\nJSON",
    "software-factory models validate --kind plan --input -",
    "software-factory models dispatch --plan .factory/local/models/plan.json --assignment implementer",
    "software-factory models outcome-record --input -",
    (
        "software-factory models template --kind request --profile claude --session WORK-A --id PLAN-001 "
        "--objective 'Implement it' --output .factory/local/models/request.json"
    ),
    "software-factory models --profile claude",
    "software-factory models",
    "software-factory mission --help",
    "software-factory mission create --id M-1 --title 'A title' --kind product --base main",
    "software-factory mission create --input - <<'JSON'\n{}\nJSON",
    "software-factory mission status --mission=M-1",
    "software-factory mission resume --mission M-1 --to PLANNED --resolution fixed --replan-models",
    "software-factory mission task-transition --mission M-1 --task T-1 --to RUNNING",
    "software-factory mission record-doc --mission M-1 --doc plan --input -",
    "software-factory checks --only lint --require-clean",
    "software-factory verify --mission M-1 --reconcile-postmerge",
    "software-factory triage --mission M-1 --check lint --no-network",
    "software-factory packet --mission M-1 --kind handoff",
    "software-factory status --mission M-1 --help",
    "git show HEAD:factory.json",
    "git diff HEAD..main -- src",
    "git log main~3..HEAD",
    "cat .git/config",
    "cat < /dev/null",
    "rg --no-search-zip foo src",
]

OPTION_DENIED = [
    (
        "software-factory mission approve --mission M-1 --kind scope --reference 'user said yes'",
        "records the user's own approval",
    ),
    (
        "software-factory mission ci-result --mission M-1 --head abc --conclusion success --url https://ci/x",
        "remote CI result the user observed",
    ),
    ("software-factory models discover --client /bin/sh", "models discover"),
    ("software-factory models discover --profile claude", "models discover"),
    (f"{VENV} models discover", "models discover"),
    ("uv run --locked --project .factory software-factory models discover", "models discover"),
    ("software-factory models plan --client /bin/sh", "--client"),
    ("software-factory models --timeout-ms 5", "--timeout-ms"),
    ("software-factory models unknown", "`models unknown`"),
    ("software-factory verify --candidate-root /other", "--candidate-root"),
    ("software-factory verify --candidate-root=../x", "--candidate-root"),
    ("software-factory verify --mission M-1 --cand /x", "--candidate-root"),
    ("software-factory semantic check --input /etc/passwd", "outside the project"),
    ("software-factory semantic check --input=/etc/passwd", "outside the project"),
    ("software-factory semantic verify-claims --input ~/claims.json", "outside the project"),
    ("software-factory jev check --input ../other/claims.json", "outside the project"),
    ("software-factory models plan --input - --catalog /tmp/catalog.json", "outside the project"),
    ("software-factory models dispatch --plan 'C:\\plan.json'", "outside the project"),
    ("software-factory models validate --input 'a\\..\\..\\b.json'", "outside the project"),
    ("software-factory mission criteria --mission M-1 --input /etc/hosts", "outside the project"),
    ("software-factory mission create --request-file ../request.md --id M --title T", "outside the project"),
    ("software-factory mission brief --mission M-1 --summary x", "--summary"),
    ("software-factory mission brief --miss M-1", "--miss"),
    ("software-factory mission bogus --mission M-1", "`mission bogus`"),
    ("software-factory semantic run", "`semantic run`"),
    ("software-factory status extra", "'extra'"),
    ("software-factory status -", "'-'"),
    ("software-factory status --mission", "needs a value"),
    ("software-factory status --mission --help", "looks like an option"),
    ("software-factory gate --mission M-1 --", "option --"),
    ("software-factory checks --require-clean=yes", "--require-clean"),
    ("software-factory mission --verbose", "--verbose"),
    ("software-factory mission list --root /x", "--root"),
    (f"{VENV} render", "`render`"),
    (f"{VENV} --root /x status", "--root"),
    ("./.factory/.venv/bin/software-factory status", "not an allowed"),
    ("rg --hostname-bin=/bin/sh foo", "--hostname-bin"),
    ("rg --hostname-bin /bin/sh foo", "--hostname-bin"),
    ("rg -z foo", "-z/--search-zip"),
    ("rg -nz foo", "-z/--search-zip"),
    ("rg --search-zip foo", "-z/--search-zip"),
    ("cat ~/.ssh/id_rsa", "outside the project"),
    ("cat /proc/self/environ", "outside the project"),
    ("head ../secrets.txt", "outside the project"),
    ("tail src/../../x", "outside the project"),
    ("ls ~", "outside the project"),
    ("find / -name id_rsa", "outside the project"),
    ("find . -newer /etc/passwd", "outside the project"),
    ("grep -f/etc/passwd file", "outside the project"),
    ("grep --file=/etc/passwd file", "outside the project"),
    ("rg foo /home", "outside the project"),
    ("wc -l ..", "outside the project"),
    ("git diff --no-index /etc/passwd file", "outside the project"),
    ("git show HEAD -- ../x", "outside the project"),
    ("git log -- ~/x", "outside the project"),
    ("cat < /etc/passwd", "outside the project"),
    ("cat < ../x", "outside the project"),
    ("git status;;", "empty command"),
    ("git status; ;", "empty command"),
    ("git status &&\n\ngit log", "empty command"),
]


@pytest.mark.parametrize("command", OPTION_ALLOWED)
def test_allowlisted_factory_options_and_project_paths_are_allowed(command):
    assert bash(command) == (0, "", "")


@pytest.mark.parametrize(("command", "fragment"), OPTION_DENIED)
def test_unlisted_options_and_outside_paths_are_denied(command, fragment):
    code, out, err = bash(command)
    reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    assert code == 2 and fragment in reason, reason
    assert err.strip() == reason


def _parser_options():
    """(command path) -> (value options, flags) from the real CLI parser."""
    import argparse

    from software_factory.cli import build_parser

    found = {}

    def walk(parser, path):
        values, flags = set(), set()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    walk(child, (*path, name))
            elif action.option_strings and not isinstance(action, argparse._HelpAction):
                (flags if action.nargs == 0 else values).update(action.option_strings)
        found[path] = (values, flags)

    walk(build_parser(), ())
    return found


# Parser options the guard deliberately refuses; a new CLI option must be classified here or allowed.
EXCLUDED = {
    ("verify",): {"--candidate-root"},
    ("models",): {"--client", "--provider", "--billing", "--picker", "--client-version", "--timeout-ms"},
    # Creating a parallel mission's worktree beside the repository is the user's decision.
    ("mission", "create"): {"--worktree", "--skip-sync"},
    # Writing a check into factory.json is the user's setup step.
    ("checks",): {"--add"},
}


def test_guard_option_allowlist_matches_the_cli_parser():
    parser = _parser_options()
    tables = {(c,): options for c, options in guard.FACTORY_COMMANDS.items()}
    for command, subcommands in guard.FACTORY_SUBCOMMANDS.items():
        assert {path[1] for path in parser if len(path) == 2 and path[0] == command} >= set(subcommands)
        tables.update({(command, sub): options for sub, options in subcommands.items()})
    for path, (values, flags) in tables.items():
        real_values, real_flags = parser[path]
        assert values <= real_values and flags <= real_flags, path
        unlisted = (real_values | real_flags) - values - flags
        if path[0] == "mission":
            unlisted -= {"--full"}  # Accepted on every mission command: it only widens the printed record.
        assert unlisted == EXCLUDED.get(path, set()), (path, unlisted)
    from software_factory.models import MODEL_COMMANDS

    assert guard.MODEL_SUBCOMMANDS == set(MODEL_COMMANDS) - {"discover"}
    paths = {o for values, _ in tables.values() for o in values if o in guard.FACTORY_PATH_OPTIONS}
    assert paths == guard.FACTORY_PATH_OPTIONS


def test_hook_event_name_is_required():
    payload = claude("Read")
    del payload["hook_event_name"]
    code, out, _ = run(payload)
    assert code == 2 and "PreToolUse" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    # Checked before the specialist pass-through.
    code, _, _ = run(
        claude("Write", {}, agent_type="factory-implementer", agent_id="a2", hook_event_name="Stop")
    )
    assert code == 2


def test_oversized_payload_names_the_reason():
    raw = json.dumps(claude("Read", {"file_path": "x" * (1 << 20)}))
    code, out, err = run(raw)
    assert (
        code == 2 and "payload too large" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    )
    assert "payload too large" in err
