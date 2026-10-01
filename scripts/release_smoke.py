"""Exercise the actual wheel in an isolated uv environment and consumer repository."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def run(argv, *, cwd, env, code=0, input_text=None):
    result = subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=180, input=input_text
    )
    if result.returncode != code:
        raise RuntimeError(f"Command failed ({result.returncode}): {argv}\n{result.stdout}\n{result.stderr}")
    return result.stdout


CRASH_DRIVER = """
import os, sys
from pathlib import Path
import software_factory.transactions as tx
from software_factory.installation import install

original, writes = tx._replace, []


def crash(root, name, value, mode=0o644):
    original(root, name, value, mode)
    writes.append(name)
    if len(writes) == 3:
        os._exit(9)  # Simulated kill: no rollback, journal and lock remain.


tx._replace = crash
install(Path(sys.argv[1]), selected="claude", skip_sync=True)
"""


def tree(root):
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not ({".git", ".venv", "local", "__pycache__"} & set(p.relative_to(root).parts))
    }


def lifecycle_checks(command, python, project, work, env):
    """Upgrade, relinquishment, downgrade, crash recovery and drift reinstall paths."""
    reviewer = project / ".claude/agents/factory-reviewer.md"
    before = tree(project)
    upgraded = json.loads(run([command, "upgrade", str(project)], cwd=work, env=env))
    assert upgraded["changed"] == [] and tree(project) == before, upgraded
    # A deleted export is relinquished by upgrade/doctor; explicit render recreates it.
    original_reviewer = reviewer.read_bytes()
    reviewer.unlink()
    report = json.loads(run([command, "doctor", "--root", str(project)], cwd=work, env=env))
    assert report["ok"], report
    assert "exports_relinquished" in {i["code"] for i in report["issues"]}
    run([command, "upgrade", str(project)], cwd=work, env=env)
    assert not reviewer.exists()
    assert json.loads((project / "factory.lock.json").read_text())["relinquished"] == [
        ".claude/agents/factory-reviewer.md"
    ]
    assert json.loads(run([command, "doctor", "--root", str(project)], cwd=work, env=env))["ok"]
    run([command, "render", "--root", str(project)], cwd=work, env=env)
    assert reviewer.read_bytes() == original_reviewer
    assert json.loads(run([command, "render", "--check", "--root", str(project)], cwd=work, env=env))["ok"]
    # Downgrade requires explicit consent.
    manifest = project / ".factory/installation.json"
    saved = manifest.read_bytes()
    value = json.loads(saved)
    value["version"] = "99.0.0"
    manifest.write_text(json.dumps(value, indent=2) + "\n")
    snapshot = tree(project)
    refused = subprocess.run(
        [command, "upgrade", str(project)],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert refused.returncode == 1 and "--allow-downgrade" in refused.stderr, refused.stderr
    assert tree(project) == snapshot
    manifest.write_bytes(saved)
    # A killed install leaves a journal; writes are refused until recover --apply.
    crashed = work / "crashed"
    crashed.mkdir()
    (crashed / "AGENTS.md").write_bytes(b"User rules\n")
    initial = tree(crashed)
    driver = work / "crash_driver.py"
    driver.write_text(CRASH_DRIVER)
    run([str(python), str(driver), str(crashed)], cwd=work, env=env, code=9)
    assert (crashed / ".factory/local/installation-transaction.json").is_file()
    diagnosis = json.loads(run([command, "doctor", "--root", str(crashed)], cwd=work, env=env, code=2))
    assert "interrupted_transaction" in {i["code"] for i in diagnosis["issues"]}, diagnosis
    blocked = subprocess.run(
        [command, "init", str(crashed), "--profile", "claude", "--skip-sync"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert blocked.returncode == 1 and "recover" in blocked.stderr, blocked.stderr
    preview = json.loads(run([command, "recover", "--root", str(crashed)], cwd=work, env=env))
    assert preview["status"] == "planned" and preview["restored"], preview
    run([command, "recover", "--apply", "--root", str(crashed)], cwd=work, env=env)
    assert tree(crashed) == initial, sorted(set(tree(crashed)) ^ set(initial))
    assert not (crashed / ".factory-install.lock").exists()
    reinstalled = json.loads(
        run([command, "init", str(crashed), "--profile", "claude", "--skip-sync"], cwd=work, env=env)
    )
    assert reinstalled["setup"] == "dependencies_missing", reinstalled
    # Without a hydrated runtime doctor exits 2, but exports and journal are clean.
    diagnosis = json.loads(run([command, "doctor", "--root", str(crashed)], cwd=work, env=env, code=2))
    codes = {i["code"] for i in diagnosis["issues"] if i["severity"] == "error"}
    assert codes == {"runtime_missing"} and diagnosis["exports"]["ok"], diagnosis


def drift_reinstall_checks(command, project, work, env):
    reviewer = project / ".claude/agents/factory-reviewer.md"
    reviewer.write_text(reviewer.read_text() + "Local reviewer note\n")
    edited = reviewer.read_bytes()
    removed = json.loads(run([command, "uninstall", "--root", str(project)], cwd=work, env=env))
    assert ".claude/agents/factory-reviewer.md" in removed["preserved"], removed
    run([command, "init", str(project), "--profile", "claude,codex,copilot"], cwd=work, env=env)
    assert reviewer.read_bytes() == edited
    assert json.loads(run([command, "doctor", "--root", str(project)], cwd=work, env=env))["ok"]


def enforcement_checks(command, project, work, env, shell):
    """The opt-in orchestrator agent renders and its guard runs on the pinned runtime."""
    runtime = project / ".factory/.venv/bin/python"
    assert runtime.is_file()
    factory_json = project / "factory.json"
    saved = factory_json.read_bytes()
    config = json.loads(saved)
    config["enforcement"] = {"claude_orchestrator_agent": True}
    factory_json.write_text(json.dumps(config, indent=2) + "\n")
    changed = json.loads(run([command, "render", "--root", str(project)], cwd=work, env=env))["changed"]
    agent = ".claude/agents/factory-orchestrator.md"
    assert agent in changed and not (project / ".github/hooks").exists(), changed
    header = (project / agent).read_text().split("---\n")[1]
    assert "Agent(factory-planner, factory-implementer, factory-verifier, factory-reviewer)" in header
    import yaml

    # Compare whole tool names: TodoWrite (a task list, not a file write) is allowed.
    tools = {
        t.strip() for t in re.sub(r"Agent\([^)]*\)", "Agent", yaml.safe_load(header)["tools"]).split(",")
    }
    assert not tools & {"Edit", "Write", "MultiEdit", "NotebookEdit"}, tools
    assert "PreToolUse" in header

    claude_hook = yaml.safe_load(header)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    guard_env = {**env, "CLAUDE_PROJECT_DIR": str(project)}
    # The guard must run on the pinned runtime, never on the python3 fallback.
    guard_env["PATH"] = str(work / "no-python")

    def hook(command_line, payload, code):
        result = subprocess.run(
            [shell, "-c", command_line],
            cwd=project,
            env=guard_env,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == code, (command_line, payload, result.stdout, result.stderr)
        return result

    write = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": "a", "content": ""},
    }
    denied = hook(claude_hook, write, 2)
    assert json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    status = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "software-factory status"},
    }
    assert hook(claude_hook, status, 0).stdout == ""
    report = json.loads(run([command, "doctor", "--root", str(project)], cwd=work, env=env))
    assert report["ok"] and report["enforcement"]["claude"]["layer"] == "hook", report
    assert report["enforcement"]["copilot"]["layer"] == "tool_allowlist"
    assert report["enforcement"]["codex"]["layer"] == "instructions"
    copilot_agent = (project / ".github/agents/factory.agent.md").read_text().split("---\n")[1]
    assert "edit" not in yaml.safe_load(copilot_agent)["tools"]
    factory_json.write_bytes(saved)
    run([command, "render", "--root", str(project)], cwd=work, env=env)
    assert not (project / agent).exists()
    assert json.loads(run([command, "doctor", "--root", str(project)], cwd=work, env=env))["ok"]


def crew_checks(command, work, env, shell):
    """0.3.3: constitution 2.1.0, project knowledge with consent, graded options, new entry prompts."""
    project = work / "crew-product"
    project.mkdir()
    (project / "README.md").write_text("Crew smoke product\n")
    run([command, "init", str(project), "--profile", "claude,codex,copilot", "--commit"], cwd=work, env=env)
    agents = (project / "AGENTS.md").read_text()
    assert "Version: 2.1.0" in agents and "with no fixed number" in agents
    assert "is not product code: do not read or search it" in agents
    deny = json.loads((project / ".claude/settings.json").read_text())["permissions"]["deny"]
    assert {"Read(./.factory/src/**)", "Read(./.factory/.venv/**)"} <= set(deny), deny
    installed = [p for p in project.rglob("*.md") if ".venv" not in p.parts]
    assert not any("two or three questions" in p.read_text() for p in installed)
    for name in ("factory-onboard", "factory-retro"):
        exported = [p for p in project.rglob(f"{name}/SKILL.md") if ".factory" not in p.parts]
        assert {p.relative_to(project).parts[0] for p in exported} >= {".claude", ".agents"}, exported
    root = ["--root", str(project)]
    # 0.3.4: the user sets the test command; a mission is not created on uncommitted setup.
    added = json.loads(
        run([command, "checks", "--add", "tests", *root, "--", "python", "-c", "pass"], cwd=project, env=env)
    )
    assert added["checks"] == ["tests"], added
    request = project / ".factory/local/request.md"
    create = [command, "mission", "create", "--id", "M-1", "--title", "Health", "--kind", "feature",
              "--request-file", ".factory/local/request.md", *root]  # fmt: skip
    request.parent.mkdir(parents=True, exist_ok=True)
    request.write_text("Please add a health endpoint.\n")
    unfinished = subprocess.run(
        create, cwd=project, env=env, capture_output=True, text=True, check=False, timeout=180
    )
    assert unfinished.returncode == 1 and "Finish the factory setup" in unfinished.stderr, unfinished.stderr
    commit = ["git", "-c", "user.name=Smoke", "-c", "user.email=smoke@example.invalid", "commit", "-qm"]
    run(["git", "add", "-A"], cwd=project, env=env)
    run([*commit, "Configure the tests check"], cwd=project, env=env)
    created = json.loads(run(create, cwd=project, env=env))
    assert created["state"] == "PROPOSED" and created["about"].startswith("Summary"), created

    def fails(argv):
        result = subprocess.run(argv, cwd=project, env=env, capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, check=False, timeout=180)  # fmt: skip
        assert result.returncode == 1, (argv, result.stdout, result.stderr)
        return result.stderr

    refused = fails([command, "mission", "accept-scope", "--mission", "M-1", *root])
    assert "options are graded against the criteria" in refused.lower(), refused
    knowledge = project / ".factory/local/knowledge.md"
    knowledge.write_text(
        "# Crew smoke product\n\n## Purpose\nA smoke test.\n\n## Users\nThe release process.\n\n"
        "## Must not break\n- The health endpoint.\n\n## Definition of done\nThe smoke passes.\n\n"
        "## Off-limits\n- Nothing.\n\n## Rules\n- Keep it small.\n"
    )
    proposed = json.loads(
        run(
            [
                command,
                "crew",
                "propose",
                "--target",
                "project",
                "--input",
                ".factory/local/knowledge.md",
                *root,
            ],
            cwd=project,
            env=env,
        )
    )
    assert proposed["proposal"] == "P-0001" and not (project / ".factory/crew").exists()
    denied = subprocess.run([command, "crew", "apply", "--proposal", "P-0001", *root], cwd=project, env=env,
                            capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False, timeout=180)  # fmt: skip
    assert denied.returncode == 1 and "interactive terminal" in denied.stderr, denied.stderr
    runtime = str(project / ".factory/.venv/bin/python")
    guard = str(project / ".factory/hooks/orchestrator_guard.py")
    for line in ("software-factory crew apply --proposal P-0001", "software-factory crew forget --target project",
                 "software-factory crew import --from ~/crew"):  # fmt: skip
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": line}}
        result = subprocess.run([runtime, "-I", "-B", guard], cwd=project, env=env, input=json.dumps(payload),
                                capture_output=True, text=True, check=False, timeout=60)  # fmt: skip
        assert result.returncode == 2, (line, result.stdout, result.stderr)
    settings = json.loads((project / ".claude/settings.json").read_text())
    chat = settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "smoke", "prompt": "approve P-0001 crew"}
    saved = subprocess.run([shell, "-c", chat], cwd=project, env={**env, "CLAUDE_PROJECT_DIR": str(project)},
                           input=json.dumps(prompt), capture_output=True, text=True, check=False, timeout=120)  # fmt: skip
    assert saved.returncode == 0 and "saved knowledge proposal P-0001" in saved.stdout, (
        saved.stdout,
        saved.stderr,
    )
    assert (project / ".factory/crew/project.md").read_text() == knowledge.read_text()
    status = json.loads(run([command, "crew", "status", *root], cwd=project, env=env))
    assert status["project"]["ledgered"] and status["ledger"] == {"count": 1, "problems": []}
    # 0.3.5: the approval itself commits the knowledge and refreshes the waiting mission.
    assert "committed it" in saved.stdout and "Refreshed missions: M-1" in saved.stdout, saved.stdout
    assert run(["git", "status", "--short", "--", ".factory/crew"], cwd=project, env=env) == ""
    run([command, "mission", "brief", "--mission", "M-1", "--kind", "context", *root], cwd=project, env=env)
    # 0.3.5: the agent proposes setup; one approval line applies, commits and moves the mission.
    setup_input = project / ".factory/local/setup.json"
    setup_input.write_text(
        json.dumps(
            {
                "reason": "Add a lint check for the scaffold",
                "checks": [
                    {"id": "tests", "command": ["python", "-c", "pass"], "cwd": ".", "required": True,
                     "timeout_seconds": 300},
                    {"id": "lint", "command": ["python", "-c", "pass"], "cwd": ".", "required": True,
                     "timeout_seconds": 300},
                ],
                "gitignore": ["node_modules/"],
            }
        )
    )  # fmt: skip
    shown = json.loads(
        run(
            [command, "setup", "propose", "--input", ".factory/local/setup.json", *root], cwd=project, env=env
        )
    )
    assert shown["proposal"] == "S-0001" and [
        c["id"] for c in json.loads((project / "factory.json").read_text())["checks"]
    ] == ["tests"]
    refused = fails([command, "setup", "apply", "--proposal", "S-0001", *root])
    assert "interactive terminal" in refused, refused
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": "software-factory setup apply --proposal S-0001"}}  # fmt: skip
    blocked = subprocess.run([runtime, "-I", "-B", guard], cwd=project, env=env, input=json.dumps(payload),
                             capture_output=True, text=True, check=False, timeout=60)  # fmt: skip
    assert blocked.returncode == 2, blocked.stdout
    before = json.loads(run([command, "mission", "status", "--mission", "M-1", *root], cwd=project, env=env))
    prompt["prompt"] = "approve S-0001 setup"
    applied = subprocess.run([shell, "-c", chat], cwd=project, env={**env, "CLAUDE_PROJECT_DIR": str(project)},
                             input=json.dumps(prompt), capture_output=True, text=True, check=False, timeout=180)  # fmt: skip
    assert applied.returncode == 0 and "applied setup proposal S-0001" in applied.stdout, (
        applied.stdout,
        applied.stderr,
    )
    assert "M-1 moved" in applied.stdout, applied.stdout
    after = json.loads(run([command, "mission", "status", "--mission", "M-1", *root], cwd=project, env=env))
    head = run(["git", "rev-parse", "HEAD"], cwd=project, env=env).strip()
    assert after["base_commit"] == head != before["base_commit"], (
        before["base_commit"],
        after["base_commit"],
    )
    assert [c["id"] for c in json.loads((project / "factory.json").read_text())["checks"]] == [
        "tests",
        "lint",
    ]
    assert (
        run(
            ["git", "status", "--short", "--", "factory.json", ".gitignore", "factory.lock.json"],
            cwd=project,
            env=env,
        )
        == ""
    )
    assert json.loads(run([command, "render", "--check", *root], cwd=project, env=env))["ok"]
    run([command, "mission", "brief", "--mission", "M-1", "--kind", "context", *root], cwd=project, env=env)
    removed = json.loads(run([command, "uninstall", *root], cwd=project, env=env))
    assert ".factory/crew" in removed["retained"] and (project / ".factory/crew/ledger.jsonl").is_file()


def routing_fixture(root):
    """Run only inside the installed project interpreter; provider behavior is mocked."""
    import io
    from contextlib import redirect_stdout

    from software_factory import auth, models, routing
    from software_factory.cli import main as factory_main
    from software_factory.core import read_json, write_json

    assert Path(models.__file__).resolve().is_relative_to(root / ".factory/src")
    config_file = root / "factory.json"
    before = config_file.read_bytes()
    policy = models.model_controls(root)["policy"]
    request = models.request_template(
        profile="codex",
        harness="codex-native",
        session_id="wheel-fixture",
        objective="Bounded fixture task",
        policy=policy,
    )
    request["assignments"] = [a for a in request["assignments"] if a["id"] == "implementer"]
    catalog = models.catalog_template(profile="codex", harness="codex-native", session_id="wheel-fixture")
    catalog.update(
        client_version="2.1.283",
        billing_context="subscription",
        provenance={"kind": "fixture", "reference": "Synthetic wheel fixture; no live inference"},
        models=[
            {
                "id": identifier,
                "provider": "openai",
                "identity": "exact",
                "resolved_model": None,
                "availability": "visible",
                "operations": ["main", "subagent"],
                "capabilities": ["text", "image", "tools"],
                "context_tokens": 100000,
                "efforts": ["medium", "high"],
                "default_effort": "medium",
                "lifecycle": "stable",
                "cost_tier": tier,
                "guidance_key": None,
            }
            for identifier, tier in (("gpt-6-sol", 2), ("gpt-6-luna", 1))
        ],
    )
    write_json(root, ".factory/local/models/request.json", request)
    write_json(root, ".factory/local/models/catalog.json", catalog)
    argv = [
        "models",
        "plan",
        "--input",
        ".factory/local/models/request.json",
        "--catalog",
        ".factory/local/models/catalog.json",
        "--root",
        str(root),
    ]

    def fail(*_a, **_kw):
        raise AssertionError("Unexpected provider/credential access")

    original_transport = routing.request_jev
    original_resolver = auth.get_typesafe_key
    try:
        routing.request_jev = fail
        output = io.StringIO()
        with redirect_stdout(output):
            factory_main(argv)
        off = json.loads(output.getvalue())
        assert off["routing"]["engine"] == "factory-models"
        assert off["assignments"][0]["model"]["id"] == "gpt-6-sol"
        config = read_json(root, "factory.json")
        config["jev"]["enabled"] = True
        write_json(root, "factory.json", config)
        calls = []

        def choose(body, **_kwargs):
            assert _kwargs["api_key"] == "synthetic-wheel-fixture"
            calls.append(body)
            key = next(c["choice"] for c in body["state"]["candidates"] if c["id"] == "gpt-6-luna")
            options = body["questions"]["selection"]["criteria"]
            return {
                "model": body["model"],
                "answers": {
                    "selection": {
                        "type": "choice",
                        "choice": key,
                        "confidence": 0.9,
                        "probabilities": {c: 1 if c == key else 0 for c in options},
                    }
                },
                "usage": {"input_tokens": 100, "output_tokens": 1},
            }

        routing.request_jev = choose
        assert "TYPESAFE_API_KEY" not in os.environ
        assert auth.get_typesafe_key() == "synthetic-wheel-fixture"
        output = io.StringIO()
        with redirect_stdout(output):
            factory_main(argv)
        on = json.loads(output.getvalue())
        assert len(calls) == 1 and on["routing"]["engine"] == "jev"
        assert on["assignments"][0]["model"]["id"] == "gpt-6-luna"
        routing.request_jev = fail
        auth.get_typesafe_key = fail
        models.validate_plan(root, on)
        models.dispatch_assignment(root, on, "implementer", catalog)
    finally:
        config_file.write_bytes(before)
        routing.request_jev = original_transport
        auth.get_typesafe_key = original_resolver
        os.environ.pop("TYPESAFE_API_KEY", None)
    print(
        json.dumps({"off": "factory-models", "on": "jev", "offline_validation": "pass", "provider": "mocked"})
    )


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--routing-fixture":
        return routing_fixture(Path(sys.argv[2]))
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        source = Path(__file__).resolve().parents[1] / "src/software_factory"
        for file in source.rglob("*"):
            if file.is_file() and "__pycache__" not in file.parts and file.suffix != ".pyc":
                member = "software_factory/" + file.relative_to(source).as_posix()
                assert archive.read(member) == file.read_bytes(), member
        assert all(
            not n.endswith((".mjs", ".js", ".pyc", "migration.py", "legacy-assets.json")) for n in names
        )
        assert all(
            not any(part in n.split("/") for part in (".git", ".venv", "__pycache__", "missions", "local"))
            for n in names
        )
        for suffix in (
            "data/runtime/uv.lock",
            "data/hooks/orchestrator_guard.py",
            "data/skills/factory-semantic/SKILL.md",
            "data/schemas/semantic.schema.json",
            "jev.py",
            "routing.py",
            "data/models/selection-rubric.json",
            "workflow.py",
        ):
            assert "software_factory/" + suffix in names, suffix
    uv = shutil.which("uv")
    git = shutil.which("git")
    shell = shutil.which("sh")
    assert uv and git and shell
    with tempfile.TemporaryDirectory(prefix="sf-wheel-consumer-") as temporary:
        work = Path(temporary)
        environment = {
            k: v
            for k, v in os.environ.items()
            if k
            not in (
                "PYTHONPATH",
                "PYTHONHOME",
                "VIRTUAL_ENV",
                "TYPESAFE_API_KEY",
                "SOFTWARE_FACTORY_AUTH_DISABLED",
            )
            and not k.startswith("GIT_")
        }
        environment["UV_NO_PROGRESS"] = "1"
        environment["XDG_CONFIG_HOME"] = str(work / "user-config")
        environment["APPDATA"] = str(work / "user-config")
        venv = work / "consumer"
        run([uv, "venv", str(venv)], cwd=work, env=environment)
        python = venv / "bin/python"
        run([uv, "pip", "install", "--python", str(python), str(wheel)], cwd=work, env=environment)
        command = str(venv / "bin/software-factory")
        # The fixture deliberately offers no Node executable.
        bin_dir = work / "bin"
        bin_dir.mkdir()
        for name, target in (("uv", uv), ("git", git), ("python", python), ("python3", python)):
            (bin_dir / name).symlink_to(target)
        environment["PATH"] = str(bin_dir)
        assert shutil.which("node", path=environment["PATH"]) is None
        assert json.loads(run([command, "version"], cwd=work, env=environment))["runtime"] == "python-uv"
        # One-time global login, followed by unrelated fresh processes with no key env.
        login = json.loads(
            run(
                [command, "auth", "login", "--stdin"],
                cwd=work,
                env=environment,
                input_text="synthetic-wheel-fixture\n",
            )
        )
        assert login["saved"] and "synthetic-wheel-fixture" not in json.dumps(login)
        assert (
            json.loads(run([command, "auth", "status"], cwd=work, env=environment))["source"] == "user_store"
        )
        project = work / "product"
        project.mkdir()
        originals = {
            "README.md": b"Existing product README\n",
            "AGENTS.md": b"Product-specific guidance\n",
            "package.json": b'{"scripts":{"test":"touch must-not-run"}}\n',
            "go.mod": b"module example.invalid/product\n",
        }
        for name, data in originals.items():
            (project / name).write_bytes(data)
        preview = json.loads(
            run(
                [command, "init", str(project), "--profile", "claude,codex,copilot", "--dry-run"],
                cwd=work,
                env=environment,
            )
        )
        assert preview["setup"] == "planned" and not (project / ".factory").exists()
        result = json.loads(
            run(
                [command, "init", str(project), "--profile", "claude,codex,copilot"],
                cwd=work,
                env=environment,
            )
        )
        assert result["setup"] == "configured", result
        for name in ("README.md", "package.json", "go.mod"):
            assert (project / name).read_bytes() == originals[name]
        diagnostic = json.loads(run([command, "doctor", "--root", str(project)], cwd=work, env=environment))
        assert diagnostic["ok"], diagnostic
        assert json.loads(
            run([command, "render", "--check", "--root", str(project)], cwd=work, env=environment)
        )["ok"]
        run([command, "inspect", "--root", str(project)], cwd=work, env=environment)
        assert not (project / "must-not-run").exists()
        bypass = json.loads(
            run(
                [command, "semantic", "check", "--input", "missing.json", "--root", str(project)],
                cwd=work,
                env=environment,
            )
        )
        assert bypass["status"] == "skipped", bypass
        routing_result = json.loads(
            run(
                [
                    str(project / ".factory/.venv/bin/python"),
                    str(Path(__file__).resolve()),
                    "--routing-fixture",
                    str(project),
                ],
                cwd=work,
                env=environment,
            )
        )
        assert routing_result["on"] == "jev" and routing_result["off"] == "factory-models"
        # Links are checked in their final installed location.
        for document in project.rglob("*.md"):
            if "/src/software_factory/" in str(document) or "/templates/" in str(document):
                continue
            for link in re.findall(r"\]\(([^)]+)\)", document.read_text()):
                if re.match(r"[a-z]+:|#", link) or any(c in link for c in ("<", "{", " ")):
                    continue
                target = link.split("#")[0]
                assert not target or (document.parent / target).exists(), (document, link)
        # The pinned runtime handles commands absent from the global version.
        launcher = project / ".factory/run.py"
        before = launcher.read_bytes()
        launcher.write_text(
            "import json,sys\nprint(json.dumps({'pinned':sys.argv[1:]}))\nraise SystemExit(7)\n"
        )
        dispatched = json.loads(
            run([command, "future-command", "--root", str(project)], cwd=work, env=environment, code=7)
        )
        assert dispatched["pinned"][0] == "future-command"
        launcher.write_bytes(before)
        # Missing local dependencies fail explicitly, without falling back globally.
        local_python = project / ".factory/.venv/bin/python"
        saved_python = local_python.with_name("python.saved")
        local_python.rename(saved_python)
        run([command, "render", "--check", "--root", str(project)], cwd=work, env=environment, code=1)
        saved_python.rename(local_python)
        lifecycle_checks(command, python, project, work, environment)
        run([command, "uninstall", "--root", str(project)], cwd=work, env=environment)
        for name, data in originals.items():
            assert (project / name).read_bytes() == data
        run([command, "init", str(project), "--profile", "claude,codex,copilot"], cwd=work, env=environment)
        assert json.loads(run([command, "doctor", "--root", str(project)], cwd=work, env=environment))["ok"]
        drift_reinstall_checks(command, project, work, environment)
        enforcement_checks(command, project, work, environment, shell)
        crew_checks(command, work, environment, shell)
        assert json.loads(run([command, "auth", "status"], cwd=project, env=environment))["configured"]
        assert all(b"synthetic-wheel-fixture" not in data for data in tree(project).values())
        assert json.loads(run([command, "auth", "logout"], cwd=work, env=environment))["removed"]
        assert not json.loads(run([command, "auth", "status"], cwd=work, env=environment, code=2))[
            "configured"
        ]
        print(
            json.dumps(
                {
                    "wheel": wheel.name,
                    "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                    "entries": len(names),
                    "isolated_install": "pass",
                    "node_available": False,
                    "hydration_dispatch_profiles_preservation_links_reinstall": "pass",
                    "upgrade_noop_relinquish_downgrade_crash_recovery_drift_reinstall": "pass",
                    "jev_toggle_and_offline_plan_validation": "pass (mocked provider)",
                    "persistent_auth_pinned_lookup_preservation_and_logout": "pass",
                    "orchestrator_agent_export_and_pinned_guard": "pass (live client behaviour not_run)",
                    "crew_knowledge_chat_hook_guard_options_protection_uninstall": "pass (live client not_run)",
                    "setup_proposal_one_line_approval_commit_and_mission_move": "pass (live client not_run)",
                    "live_provider": "not_run",
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
