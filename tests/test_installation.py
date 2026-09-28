import json
import subprocess
from unittest.mock import patch

import pytest
import yaml

from software_factory.core import FactoryError
from software_factory.installation import ignore_plan, install, uninstall
from software_factory.rendering import render
from software_factory.transactions import apply, recover


def files(root):
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.parts and "local" not in p.parts
    }


def test_folder_dry_run_writes_nothing(tmp_path):
    before = files(tmp_path)
    result = install(tmp_path, selected="claude", dry_run=True, skip_sync=True)
    assert result["setup"] == "planned"
    assert files(tmp_path) == before


@pytest.mark.parametrize(
    "profile",
    ["claude", "codex", "copilot", "claude,codex", "claude,copilot", "codex,copilot", "claude,codex,copilot"],
)
def test_profiles_and_idempotence(tmp_path, profile):
    install(tmp_path, selected=profile, skip_sync=True)
    before = files(tmp_path)
    assert render(tmp_path, check=True)["ok"]
    install(tmp_path, selected=profile, skip_sync=True)
    assert files(tmp_path) == before
    assert not list(tmp_path.rglob("*.mjs"))
    assert json.loads((tmp_path / "factory.json").read_text())["jev"]["enabled"] is False


def test_preserve_project_files_and_sections(tmp_path):
    originals = {
        "README.md": b"Product readme\n",
        "AGENTS.md": b"User agent rules without newline",
        "CLAUDE.md": b"Existing Claude instructions\n",
        "package.json": b'{"name":"mine"}\n',
        ".codex/config.toml": b'# preserve comment\nmodel = "example"\n[agents]\nmax_concurrent_threads_per_session = 2\n',
    }
    for name, content in originals.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    install(tmp_path, selected="claude,codex", skip_sync=True)
    assert (tmp_path / "AGENTS.md").read_bytes().startswith(originals["AGENTS.md"])
    assert (tmp_path / "README.md").read_bytes() == originals["README.md"]
    import tomllib

    assert (
        tomllib.loads((tmp_path / ".codex/config.toml").read_text())["agents"][
            "max_concurrent_threads_per_session"
        ]
        == 2
    )
    render(tmp_path, check=True)
    uninstall(tmp_path)
    for name, content in originals.items():
        assert (tmp_path / name).read_bytes() == content
    assert (tmp_path / "factory.json").exists()
    assert ".factory/local/" in (tmp_path / ".gitignore").read_text()


def test_collision_aborts_whole_init(tmp_path):
    path = tmp_path / ".claude/agents/factory-reviewer.md"
    path.parent.mkdir(parents=True)
    path.write_text("user-owned")
    before = files(tmp_path)
    with pytest.raises(FactoryError, match="collision"):
        install(tmp_path, selected="claude", skip_sync=True)
    assert files(tmp_path) == before


def test_changed_section_fails_render_and_uninstall_preserves(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    p = tmp_path / "AGENTS.md"
    p.write_text(p.read_text().replace("Factory session entry", "Changed session entry"))
    with pytest.raises(FactoryError, match="drift"):
        render(tmp_path)
    result = uninstall(tmp_path)
    assert "AGENTS.md" in result["preserved"]
    assert "Changed session entry" in p.read_text()


def test_user_text_edits_survive_render(tmp_path):
    (tmp_path / "AGENTS.md").write_text("first user text\n")
    install(tmp_path, selected="claude", skip_sync=True)
    p = tmp_path / "AGENTS.md"
    p.write_text(p.read_text().replace("first user text", "new user text"))
    render(tmp_path)
    assert p.read_text().startswith("new user text\n")


def test_git_baseline_not_implicitly_created(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True, git_init=True)
    assert (tmp_path / ".git").is_dir()
    assert (
        subprocess.run(
            check=False,
            args=["git", "-C", str(tmp_path), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
        ).returncode
        != 0
    )


def test_nested_target_and_dirty_opt_in(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    nested = tmp_path / "nested"
    nested.mkdir()
    with pytest.raises(FactoryError, match="explicitly select"):
        install(nested, selected="claude", skip_sync=True)
    (tmp_path / "existing.txt").write_text("work in progress")
    with pytest.raises(FactoryError, match="uncommitted"):
        install(tmp_path, selected="claude", skip_sync=True)
    install(tmp_path, selected="claude", skip_sync=True, allow_dirty=True)
    assert (tmp_path / "existing.txt").read_text() == "work in progress"


def test_dependency_stage_failure_has_no_project_writes(tmp_path):
    before = files(tmp_path)
    with (
        patch("software_factory.installation._sync", side_effect=FactoryError("offline")),
        pytest.raises(FactoryError, match="offline"),
    ):
        install(tmp_path, selected="claude")
    assert files(tmp_path) == before


def test_upgrade_preserves_customization_and_conflicts(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    p = tmp_path / ".factory/docs/architecture.md"
    p.write_text("local customization")
    from software_factory.installation import kernel_payload

    new = kernel_payload()
    new[".factory/docs/architecture.md"] = b"new upstream"
    before = files(tmp_path)
    with (
        patch("software_factory.installation.kernel_payload", return_value=new),
        pytest.raises(FactoryError, match="conflicts"),
    ):
        install(tmp_path, upgrade=True, skip_sync=True)
    assert files(tmp_path) == before
    install(tmp_path, upgrade=True, skip_sync=True)
    assert p.read_text() == "local customization"


def test_transaction_preimage_and_rollback(tmp_path):
    (tmp_path / "a").write_bytes(b"old")
    with pytest.raises(FactoryError, match="changed since"):
        apply(tmp_path, {"a": b"new"}, expected={"a": b"other"})
    import software_factory.transactions as tx

    original = tx._replace

    def replace(root, name, value, mode=0o644):
        if name == "b" and value == b"new":
            raise OSError("injected failure")
        original(root, name, value, mode)

    with patch.object(tx, "_replace", side_effect=replace), pytest.raises(OSError):
        apply(tmp_path, {"a": b"new", "b": b"new"})
    assert (tmp_path / "a").read_bytes() == b"old"
    assert not (tmp_path / "b").exists()


def test_nested_ignore_conflict(tmp_path):
    (tmp_path / ".factory").mkdir()
    (tmp_path / ".factory/.gitignore").write_text("!local/\n")
    with pytest.raises(FactoryError, match="Nested ignore"):
        ignore_plan(tmp_path)


def test_recovery_preserves_concurrent_edit(tmp_path):
    import base64

    from software_factory.core import write_json

    (tmp_path / ".factory").mkdir(exist_ok=True)
    (tmp_path / ".factory/a").write_bytes(b"concurrent")
    encode = lambda b: base64.b64encode(b).decode()
    write_json(
        tmp_path,
        ".factory/local/installation-transaction.json",
        {
            "schema_version": 1,
            "files": {".factory/a": {"before": encode(b"old"), "after": encode(b"new"), "mode": 420}},
        },
    )
    with pytest.raises(FactoryError, match="concurrent"):
        recover(tmp_path, apply_recovery=True)
    assert (tmp_path / ".factory/a").read_bytes() == b"concurrent"


def test_write_then_raise_rolls_back(tmp_path):
    import software_factory.transactions as tx

    (tmp_path / "a").write_bytes(b"old")
    original = tx._replace

    def replace(root, name, value, mode=0o644):
        original(root, name, value, mode)
        if value == b"new":
            raise OSError("after write")

    with patch.object(tx, "_replace", side_effect=replace), pytest.raises(OSError):
        apply(tmp_path, {"a": b"new"})
    assert (tmp_path / "a").read_bytes() == b"old"


def test_edit_during_planning_preserved(tmp_path):
    (tmp_path / "AGENTS.md").write_text("old user rules")

    def edit(root):
        (root / "AGENTS.md").write_text("new user rules")
        return b".factory/local/\n"

    with (
        patch("software_factory.installation.ignore_plan", side_effect=edit),
        pytest.raises(FactoryError, match="changed during planning"),
    ):
        install(tmp_path, selected="claude", skip_sync=True)
    assert (tmp_path / "AGENTS.md").read_text() == "new user rules"
    assert not (tmp_path / "factory.json").exists()


def test_reinstall_after_uninstall(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    uninstall(tmp_path)
    install(tmp_path, selected="claude", skip_sync=True)
    assert render(tmp_path, check=True)["ok"]


def test_noncanonical_manifest_cannot_own_history(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    p = tmp_path / ".factory/missions/M-user/spec.md"
    p.parent.mkdir(parents=True)
    p.write_text("user history")
    m = tmp_path / ".factory/installation.json"
    value = json.loads(m.read_text())
    from software_factory.core import sha256

    value["files"][".factory/./missions/M-user/spec.md"] = {"sha256": sha256(p.read_bytes()), "managed": True}
    m.write_text(json.dumps(value))
    with pytest.raises(FactoryError):
        uninstall(tmp_path)
    assert p.read_text() == "user history"


def test_shared_suffix_survives_in_place(tmp_path):
    (tmp_path / "AGENTS.md").write_text("User prefix without newline")
    install(tmp_path, selected="claude", skip_sync=True)
    p = tmp_path / "AGENTS.md"
    p.write_text(p.read_text() + "New user suffix\n")
    before = p.read_bytes()
    render(tmp_path)
    assert p.read_bytes() == before
    uninstall(tmp_path)
    assert "newlineNew" not in p.read_text()
    assert p.read_text().endswith("New user suffix\n")


def test_missing_canonical_asset_does_not_fall_back(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    (tmp_path / ".factory/roles/orchestrator.md").unlink()
    with pytest.raises(FactoryError, match="Missing installed"):
        render(tmp_path, check=True)


def test_shared_manifest_cannot_claim_whole_file(tmp_path):
    (tmp_path / "AGENTS.md").write_text("User-owned rules\n")
    install(tmp_path, selected="claude", skip_sync=True)
    path = tmp_path / "factory.lock.json"
    value = json.loads(path.read_text())
    from software_factory.core import sha256

    value["generated"]["AGENTS.md"] = {
        "kind": "file",
        "sha256": sha256((tmp_path / "AGENTS.md").read_bytes()),
    }
    path.write_text(json.dumps(value))
    before = (tmp_path / "AGENTS.md").read_bytes()
    with pytest.raises(FactoryError, match="ownership kind"):
        uninstall(tmp_path)
    assert (tmp_path / "AGENTS.md").read_bytes() == before


def test_failed_init_journal_remains_ignored(tmp_path):
    import software_factory.transactions as tx

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    original = tx._replace

    def replace(root, name, value, mode=0o644):
        if name == ".factory/a":
            original(root, name, b"concurrent", mode)
            raise OSError("retain journal")
        original(root, name, value, mode)

    with patch.object(tx, "_replace", side_effect=replace), pytest.raises(OSError):
        apply(tmp_path, {".factory/a": b"new"})
    assert (tmp_path / tx.JOURNAL).exists()
    result = subprocess.run(["git", "-C", str(tmp_path), "check-ignore", "-q", "--", tx.JOURNAL], check=False)
    assert result.returncode == 0


def test_upgrade_adds_skill_from_proposed_payload(tmp_path):
    from software_factory.installation import kernel_payload

    install(tmp_path, selected="claude", skip_sync=True)
    payload = kernel_payload()
    registry = json.loads(payload[".factory/registry.json"])
    registry["skills"].append("factory-new-reviewed")
    payload[".factory/registry.json"] = json.dumps(registry).encode()
    payload[".factory/skills/factory-new-reviewed/SKILL.md"] = (
        b"---\nname: factory-new-reviewed\ndescription: New reviewed skill\n---\nNew skill\n"
    )
    with patch("software_factory.installation.kernel_payload", return_value=payload):
        install(tmp_path, upgrade=True, skip_sync=True)
    assert (tmp_path / ".claude/skills/factory-new-reviewed/SKILL.md").is_file()
    assert render(tmp_path, check=True)["ok"]


def test_configuration_changed_during_staging_aborts(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    original = files(tmp_path)

    def change(_path):
        config = json.loads((tmp_path / "factory.json").read_text())
        config["name"] = "changed during setup"
        (tmp_path / "factory.json").write_text(json.dumps(config))

    with (
        patch("software_factory.installation._sync", side_effect=change),
        pytest.raises(FactoryError, match="changed during planning"),
    ):
        install(tmp_path, upgrade=True)
    after = files(tmp_path)
    assert after.pop("factory.json") != original.pop("factory.json")
    assert after == original


def test_upgrade_refuses_deleted_required_skill(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    (tmp_path / ".factory/skills/factory-start/SKILL.md").unlink()
    before = files(tmp_path)
    with pytest.raises(FactoryError, match="Required skill is missing"):
        install(tmp_path, upgrade=True, skip_sync=True)
    assert files(tmp_path) == before
    assert (tmp_path / ".claude/skills/factory-start/SKILL.md").is_file()


REVIEWER = ".claude/agents/factory-reviewer.md"


def lock(root):
    return json.loads((root / "factory.lock.json").read_text())


def test_deleted_export_is_restored_by_explicit_render(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    original = (tmp_path / REVIEWER).read_bytes()
    (tmp_path / REVIEWER).unlink()
    result = render(tmp_path)
    assert REVIEWER in result["changed"]
    assert (tmp_path / REVIEWER).read_bytes() == original
    assert REVIEWER in lock(tmp_path)["generated"]
    assert "relinquished" not in lock(tmp_path)
    assert render(tmp_path, check=True)["ok"]


def test_deleted_export_is_relinquished_by_upgrade_and_init(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    (tmp_path / REVIEWER).unlink()
    install(tmp_path, upgrade=True, skip_sync=True)
    assert not (tmp_path / REVIEWER).exists()
    assert REVIEWER not in lock(tmp_path)["generated"]
    assert lock(tmp_path)["relinquished"] == [REVIEWER]
    assert render(tmp_path, check=True)["relinquished"] == [REVIEWER]
    before = files(tmp_path)
    install(tmp_path, selected="claude", skip_sync=True)
    install(tmp_path, upgrade=True, skip_sync=True)
    assert files(tmp_path) == before
    render(tmp_path)
    assert (tmp_path / REVIEWER).is_file()
    assert "relinquished" not in lock(tmp_path)


def test_deleted_export_does_not_fail_doctor(tmp_path):
    from software_factory.cli import doctor

    install(tmp_path, selected="claude", skip_sync=True)
    (tmp_path / REVIEWER).unlink()
    before = files(tmp_path)
    report = doctor(tmp_path)
    codes = {issue["code"]: issue for issue in report["issues"]}
    assert "configuration" not in codes, codes
    assert REVIEWER in codes["exports_relinquished"]["message"]
    assert codes["exports_relinquished"]["severity"] == "warning"
    assert files(tmp_path) == before


def test_drift_message_explains_resolution(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    path = tmp_path / REVIEWER
    path.write_text(path.read_text() + "local edit\n")
    with pytest.raises(FactoryError, match="git checkout -- .*delete it to relinquish"):
        render(tmp_path)
    with pytest.raises(FactoryError, match="drift"):
        install(tmp_path, upgrade=True, skip_sync=True)


def test_drift_uninstall_then_init_keeps_user_file(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    path = tmp_path / REVIEWER
    path.write_text(path.read_text() + "local edit\n")
    edited = path.read_bytes()
    result = uninstall(tmp_path)
    assert REVIEWER in result["preserved"]
    assert lock(tmp_path)["generated"] == {}
    install(tmp_path, selected="claude", skip_sync=True)
    assert path.read_bytes() == edited
    assert REVIEWER not in lock(tmp_path)["generated"]
    assert render(tmp_path, check=True)["ok"]
    # An explicit render never overwrites the user-owned copy.
    render(tmp_path)
    assert path.read_bytes() == edited


def test_drift_uninstall_delete_then_init_recreates(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    path = tmp_path / REVIEWER
    original = path.read_bytes()
    path.write_text(path.read_text() + "local edit\n")
    uninstall(tmp_path)
    path.unlink()
    install(tmp_path, selected="claude", skip_sync=True)
    assert path.read_bytes() == original
    assert REVIEWER in lock(tmp_path)["generated"]
    assert render(tmp_path, check=True)["ok"]


def test_kernel_drift_uninstall_then_init_preserves_customization(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    path = tmp_path / ".factory/docs/architecture.md"
    path.write_text("local customization")
    assert ".factory/docs/architecture.md" in uninstall(tmp_path)["preserved"]
    install(tmp_path, selected="claude", skip_sync=True)
    assert path.read_text() == "local customization"
    assert render(tmp_path, check=True)["ok"]


def test_user_skill_without_frontmatter_does_not_abort(tmp_path):
    user = tmp_path / ".claude/skills/notes/SKILL.md"
    user.parent.mkdir(parents=True)
    user.write_text("plain notes without frontmatter\n")
    install(tmp_path, selected="claude", skip_sync=True)
    assert user.read_text() == "plain notes without frontmatter\n"
    assert render(tmp_path, check=True)["ok"]


def test_factory_named_user_skill_collision_names_path(tmp_path):
    user = tmp_path / ".claude/skills/factory-build/SKILL.md"
    user.parent.mkdir(parents=True)
    user.write_text("no frontmatter here\n")
    before = files(tmp_path)
    with pytest.raises(FactoryError, match=r"collision: \.claude/skills/factory-build/SKILL\.md"):
        install(tmp_path, selected="claude", skip_sync=True)
    assert files(tmp_path) == before


def test_gitignore_block_not_reappended_after_user_lines(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    ignore = tmp_path / ".gitignore"
    ignore.write_text(ignore.read_text() + "user-rule/\n")
    before = ignore.read_text()
    install(tmp_path, upgrade=True, skip_sync=True)
    install(tmp_path, selected="claude", skip_sync=True)
    assert ignore.read_text() == before
    assert before.count("# software-factory private/runtime files") == 1


def test_git_is_an_explicit_requirement(tmp_path):
    with (
        patch("software_factory.installation.shutil.which", return_value=None),
        pytest.raises(FactoryError, match="git is required"),
    ):
        install(tmp_path, selected="claude", skip_sync=True)
    assert not (tmp_path / "factory.json").exists()


def test_upgrade_refuses_downgrade_without_opt_in(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    manifest = tmp_path / ".factory/installation.json"
    value = json.loads(manifest.read_text())
    value["version"] = "99.0.0"
    manifest.write_text(json.dumps(value))
    before = files(tmp_path)
    with pytest.raises(FactoryError, match="newer than this release.*--allow-downgrade"):
        install(tmp_path, upgrade=True, skip_sync=True)
    assert files(tmp_path) == before
    install(tmp_path, upgrade=True, skip_sync=True, allow_downgrade=True)
    from software_factory import __version__

    assert json.loads(manifest.read_text())["version"] == __version__


def test_release_ordering():
    from software_factory.installation import release_key

    assert release_key("0.10.0") > release_key("0.2.0")
    assert release_key("0.2") == release_key("0.2.0")
    assert release_key("dev") is None


def test_pending_journal_blocks_writes_and_doctor_reports(tmp_path):
    from software_factory.cli import doctor
    from software_factory.core import write_json
    from software_factory.transactions import JOURNAL

    install(tmp_path, selected="claude", skip_sync=True)
    write_json(tmp_path, JOURNAL, {"schema_version": 1, "files": {}})
    for operation in (
        lambda: install(tmp_path, upgrade=True, skip_sync=True),
        lambda: install(tmp_path, selected="claude", skip_sync=True, dry_run=True),
        lambda: uninstall(tmp_path),
        lambda: render(tmp_path),
    ):
        with pytest.raises(FactoryError, match="interrupted.*recover"):
            operation()
    codes = {i["code"]: i for i in doctor(tmp_path)["issues"]}
    assert codes["interrupted_transaction"]["severity"] == "error"
    assert "software-factory recover" in codes["interrupted_transaction"]["message"]
    recover(tmp_path, apply_recovery=True)
    assert "interrupted_transaction" not in {i["code"] for i in doctor(tmp_path)["issues"]}


def test_installation_manifest_is_schema_validated(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    manifest = tmp_path / ".factory/installation.json"
    value = json.loads(manifest.read_text())
    value["unexpected"] = True
    manifest.write_text(json.dumps(value))
    with pytest.raises(FactoryError, match="Invalid installation manifest"):
        install(tmp_path, upgrade=True, skip_sync=True)


def test_report_separates_model_selection_from_claim_mode(tmp_path):
    from software_factory.cli import doctor

    result = install(tmp_path, selected="claude", skip_sync=True)
    assert result["jev"] == {"enabled": False, "model_selection": "factory-models", "claim_mode": "off"}
    config = json.loads((tmp_path / "factory.json").read_text())
    config["jev"]["enabled"] = True
    (tmp_path / "factory.json").write_text(json.dumps(config, indent=2) + "\n")
    report = doctor(tmp_path)["jev"]
    assert report["model_selection"] == "jev" and report["claim_mode"] == "shadow"


def test_entry_prompts_grouped_per_client(tmp_path):
    install(tmp_path, selected="claude,codex,copilot", skip_sync=True)
    text = (tmp_path / "AGENTS.md").read_text()
    assert "Claude Code/Copilot: /factory-build" in text
    assert "; Codex: $factory-build" in text
    assert "`software-factory models plan`" in text and "`jev.enabled`" in text
    assert len(text.encode()) < 30000


def test_doctor_reports_runtime_drift(tmp_path):
    from software_factory.cli import doctor

    install(tmp_path, selected="claude", skip_sync=True)
    assert "runtime_drift" not in {i["code"] for i in doctor(tmp_path)["issues"]}
    source = tmp_path / ".factory/src/software_factory/core.py"
    source.write_text(source.read_text() + "\n# tampered\n")
    issue = next(i for i in doctor(tmp_path)["issues"] if i["code"] == "runtime_drift")
    assert issue["severity"] == "error" and "core.py" in issue["message"]
    source.write_bytes(
        files(tmp_path)[".factory/src/software_factory/core.py"].replace(b"\n# tampered\n", b"")
    )
    (tmp_path / ".factory/src/software_factory/extra.py").write_text("injected = True\n")
    issue = next(i for i in doctor(tmp_path)["issues"] if i["code"] == "runtime_drift")
    assert "extra.py" in issue["message"]


def test_doctor_version_skew_skips_global_checks(tmp_path):
    from software_factory.cli import doctor

    install(tmp_path, selected="claude", skip_sync=True)
    manifest = tmp_path / ".factory/installation.json"
    value = json.loads(manifest.read_text())
    value["version"] = "0.1.0"
    manifest.write_text(json.dumps(value))
    report = doctor(tmp_path)
    codes = {i["code"] for i in report["issues"]}
    assert report["pinned_version"] == "0.1.0"
    assert report["exports"] == "not_checked" and report["runtime_fingerprint"] == "not_checked"
    assert {"runtime_missing", "version_skew"} <= codes and "configuration" not in codes


def test_failed_offline_sync_writes_no_private_status(tmp_path):
    calls = []

    def sync(path, offline=False):
        calls.append(offline)
        if offline:
            raise FactoryError("offline")

    with (
        patch("software_factory.installation._sync", side_effect=sync),
        pytest.raises(FactoryError, match="runtime setup incomplete"),
    ):
        install(tmp_path, selected="claude")
    assert calls == [False, True]
    assert not (tmp_path / ".factory/local/setup.json").exists()


def installed_with_history(tmp_path):
    """An installation whose recorded mission/evidence schemas predate the proposed release."""
    import shutil

    from test_workflow import begin, make_repo

    from software_factory.checks import verify_mission
    from software_factory.core import sha256
    from software_factory.workflow import transition_mission, transition_task

    source = make_repo(tmp_path / "history")
    id = begin(source)
    transition_task(source, id, "T-ONE", "VERIFYING")
    transition_mission(source, id, "VERIFYING")
    assert verify_mission(source, id, "R-ONE")["reference"]
    root = tmp_path / "project"
    root.mkdir()
    install(root, selected="claude", skip_sync=True)
    shutil.copytree(source / ".factory/missions", root / ".factory/missions")
    manifest = json.loads((root / ".factory/installation.json").read_text())
    for kind, drop in (("evidence", "sequence"), ("mission", None)):
        name = f".factory/schemas/{kind}.schema.json"
        schema = json.loads((root / name).read_text())
        if drop:
            schema["required"].remove(drop)
            del schema["properties"][drop]
        else:
            schema["description"] = "earlier release"
        data = (json.dumps(schema, indent=2) + "\n").encode()
        (root / name).write_bytes(data)
        manifest["files"][name]["sha256"] = sha256(data)
    (root / ".factory/installation.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return root, id


def test_upgrade_validates_history_against_changed_schemas(tmp_path):
    root, id = installed_with_history(tmp_path)
    records = [
        f".factory/missions/{id}/evidence/R-ONE/checks.json",
        f".factory/missions/{id}/mission.json",
    ]
    planned = install(root, upgrade=True, skip_sync=True, dry_run=True)
    assert planned["history"]["validated_records"] == records
    assert planned["history"]["schemas"] == [
        ".factory/schemas/evidence.schema.json",
        ".factory/schemas/mission.schema.json",
    ]
    applied = install(root, upgrade=True, skip_sync=True)
    assert applied["history"] == planned["history"]
    assert "sequence" in json.loads((root / ".factory/schemas/evidence.schema.json").read_text())["required"]


def test_upgrade_blocks_history_that_fails_new_schemas(tmp_path):
    root, id = installed_with_history(tmp_path)
    evidence = root / f".factory/missions/{id}/evidence/R-ONE/checks.json"
    value = json.loads(evidence.read_text())
    del value["sequence"]  # as recorded by 0.2.0
    evidence.write_text(json.dumps(value))
    before = files(root)
    for dry_run in (True, False):
        with pytest.raises(FactoryError) as caught:
            install(root, upgrade=True, skip_sync=True, dry_run=dry_run)
        message = str(caught.value)
        assert f".factory/missions/{id}/evidence/R-ONE/checks.json" in message
        assert "'sequence' is a required property" in message
        assert "mission.json" not in message
        assert "'Schema changes and mission history' in this release's docs/runbooks/upgrading.md" in message
        assert files(root) == before
    from software_factory.core import asset_root

    runbook = (asset_root() / "docs/runbooks/upgrading.md").read_text()
    assert "## Schema changes and mission history" in runbook and "archive" in runbook


def test_upgrade_blocks_schema_changes_without_known_records(tmp_path):
    root, _ = installed_with_history(tmp_path)
    from software_factory.core import sha256

    name = ".factory/schemas/semantic.schema.json"
    data = (root / name).read_bytes() + b"\n"
    (root / name).write_bytes(data)
    manifest = json.loads((root / ".factory/installation.json").read_text())
    manifest["files"][name]["sha256"] = sha256(data)
    (root / ".factory/installation.json").write_text(json.dumps(manifest, indent=2) + "\n")
    with pytest.raises(FactoryError, match="without a known record validation.*semantic.schema.json"):
        install(root, upgrade=True, skip_sync=True, dry_run=True)


def _loosen_installed(root, name, change):
    """Make the installed copy of a schema predate this release, as an older release would."""
    from software_factory.core import sha256

    path = root / name
    schema = json.loads(path.read_text())
    change(schema)
    data = (json.dumps(schema, indent=2) + "\n").encode()
    path.write_bytes(data)
    manifest = json.loads((root / ".factory/installation.json").read_text())
    manifest["files"][name]["sha256"] = sha256(data)
    (root / ".factory/installation.json").write_text(json.dumps(manifest, indent=2) + "\n")


def test_upgrade_validates_history_for_schemas_new_in_this_release(tmp_path):
    root, id = installed_with_history(tmp_path)
    name = ".factory/schemas/result-index.schema.json"
    (root / name).unlink()
    manifest = json.loads((root / ".factory/installation.json").read_text())
    del manifest["files"][name]  # an older release that did not ship this schema
    (root / ".factory/installation.json").write_text(json.dumps(manifest, indent=2) + "\n")
    index = root / f".factory/missions/{id}/results/index.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(json.dumps({"unexpected": True}))
    before = files(root)
    for dry_run in (True, False):
        with pytest.raises(FactoryError) as caught:
            install(root, upgrade=True, skip_sync=True, dry_run=dry_run)
        assert f".factory/missions/{id}/results/index.json (" in str(caught.value)
        assert files(root) == before
    index.unlink()
    planned = install(root, upgrade=True, skip_sync=True, dry_run=True)
    assert name in planned["history"]["schemas"]


def test_upgrade_validates_factory_json_without_missions(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    name = ".factory/schemas/factory.schema.json"
    _loosen_installed(tmp_path, name, lambda s: s["properties"].update({"legacy_option": {}}))
    config = json.loads((tmp_path / "factory.json").read_text())
    config["legacy_option"] = 1
    (tmp_path / "factory.json").write_text(json.dumps(config, indent=2) + "\n")
    assert not (tmp_path / ".factory/missions").exists()
    before = files(tmp_path)
    for dry_run in (True, False):
        with pytest.raises(FactoryError) as caught:
            install(tmp_path, upgrade=True, skip_sync=True, dry_run=dry_run)
        message = str(caught.value)
        assert "Update factory.json" in message and "no changes applied" in message
        assert "factory.json (<root>: Additional properties are not allowed ('legacy_option'" in message
        assert files(tmp_path) == before
    del config["legacy_option"]
    (tmp_path / "factory.json").write_text(json.dumps(config, indent=2) + "\n")
    planned = install(tmp_path, upgrade=True, skip_sync=True, dry_run=True)
    assert planned["history"] == {"schemas": [name], "validated_records": ["factory.json"]}


def test_upgrade_refuses_symlink_in_mission_history(tmp_path):
    root, id = installed_with_history(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "checks.json").write_text(json.dumps({"not": "evidence"}))
    link = root / f".factory/missions/{id}/evidence/R-EVIL"
    link.symlink_to(outside, target_is_directory=True)
    before = files(root)
    for dry_run in (True, False):
        with pytest.raises(FactoryError, match=f"Symlink in managed path: .*{id}/evidence/R-EVIL"):
            install(root, upgrade=True, skip_sync=True, dry_run=dry_run)
        assert files(root) == before
    assert link.is_symlink() and (outside / "checks.json").is_file()


ENTRY = ".claude/skills/factory-build/SKILL.md"


def test_doctor_reports_replaced_and_missing_entry_skills(tmp_path):
    from software_factory.cli import doctor

    install(tmp_path, selected="claude", skip_sync=True)
    original = (tmp_path / ENTRY).read_bytes()
    status = ".claude/skills/factory-status/SKILL.md"
    for name in (ENTRY, status, REVIEWER):
        (tmp_path / name).unlink()
    install(tmp_path, upgrade=True, skip_sync=True)
    assert {ENTRY, status, REVIEWER} <= set(lock(tmp_path)["relinquished"])
    (tmp_path / ENTRY).write_text("---\nname: factory-build\ndescription: mine\n---\n\nUser content.\n")
    before = files(tmp_path)
    report = doctor(tmp_path)
    assert files(tmp_path) == before
    codes = {issue["code"]: issue for issue in report["issues"]}
    assert codes["entry_skill_replaced"]["severity"] == "error" and not report["ok"]
    replaced = codes["entry_skill_replaced"]["message"]
    assert ENTRY in replaced and "delete the file and run software-factory render" in replaced
    assert codes["entry_skill_missing"]["severity"] == "warning"
    assert (
        status in codes["entry_skill_missing"]["message"]
        and ENTRY not in codes["entry_skill_missing"]["message"]
    )
    assert "software-factory render" in codes["entry_skill_missing"]["message"]
    assert codes["exports_relinquished"]["message"].count(".claude/skills/") == 0
    assert REVIEWER in codes["exports_relinquished"]["message"]
    # Identical content is not a replacement; the user file is never overwritten.
    (tmp_path / ENTRY).write_bytes(original)
    codes = {issue["code"]: issue for issue in doctor(tmp_path)["issues"]}
    assert "entry_skill_replaced" not in codes
    assert ENTRY in codes["exports_relinquished"]["message"]
    (tmp_path / ENTRY).unlink()
    render(tmp_path)
    codes = {issue["code"] for issue in doctor(tmp_path)["issues"]}
    assert not codes & {"entry_skill_replaced", "entry_skill_missing", "exports_relinquished"}


def test_doctor_reports_deleted_codex_entry_skill(tmp_path):
    from software_factory.cli import doctor

    install(tmp_path, selected="codex", skip_sync=True)
    name = ".agents/skills/factory-resume/agents/openai.yaml"
    (tmp_path / name).unlink()
    codes = {issue["code"]: issue for issue in doctor(tmp_path)["issues"]}
    assert name in codes["entry_skill_missing"]["message"]
    assert "exports_relinquished" not in codes


START_MARK = "<!-- software-factory:start -->"
END_MARK = "<!-- software-factory:end -->"


@pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md"])
def test_edited_section_is_unmarked_on_uninstall_and_reinstall_adds_block(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"Before user text\n")
    install(tmp_path, selected="claude", skip_sync=True)
    p.write_bytes(p.read_bytes() + b"\nAfter user text\n")
    text = p.read_text()
    first, last = text.index(START_MARK), text.index(END_MARK)
    edited_inner = text[first + len(START_MARK) + 1 : last] + "Mine\n"
    edited = text[:first] + START_MARK + "\n" + edited_inner + END_MARK + text[last + len(END_MARK) :]
    p.write_text(edited)
    before = files(tmp_path)
    planned = uninstall(tmp_path, dry_run=True)
    assert files(tmp_path) == before
    result = uninstall(tmp_path)
    assert planned["unmarked_sections"] == result["unmarked_sections"] == [name]
    assert planned["removed_or_detached"] == result["removed_or_detached"]
    assert name in result["preserved"]
    expected = text[:first] + edited_inner + text[last + len(END_MARK) + 1 :]
    assert p.read_text() == expected
    assert START_MARK not in expected and END_MARK not in expected
    assert expected.startswith("Before user text\n") and expected.endswith("\nAfter user text\n")
    assert expected.count("\nMine\n") == 1
    install(tmp_path, selected="claude", skip_sync=True)
    reinstalled = p.read_text()
    assert reinstalled.startswith(expected)
    assert reinstalled.count(START_MARK) == 1 and reinstalled.count(END_MARK) == 1
    render(tmp_path, check=True)


def test_unchanged_section_still_removed_cleanly(tmp_path):
    (tmp_path / "AGENTS.md").write_bytes(b"User text\n")
    install(tmp_path, selected="claude", skip_sync=True)
    result = uninstall(tmp_path)
    assert result["unmarked_sections"] == []
    assert (tmp_path / "AGENTS.md").read_bytes() == b"User text\n"
    assert not (tmp_path / "CLAUDE.md").exists()


def test_unmark_keeps_text_sharing_a_marker_line(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    p = tmp_path / "AGENTS.md"
    p.write_text(p.read_text().replace(END_MARK, "trailing note " + END_MARK))
    uninstall(tmp_path)
    text = p.read_text()
    assert START_MARK not in text and END_MARK not in text
    assert "trailing note \n" in text


def _codex_toggle(tmp_path, old, new):
    c = tmp_path / ".codex/config.toml"
    c.write_text(c.read_text().replace(old, new))
    return c


def test_conflicting_codex_key_after_uninstall_refuses_init(tmp_path):
    install(tmp_path, selected="codex", skip_sync=True)
    c = _codex_toggle(tmp_path, "enabled = true", "enabled = false")
    result = uninstall(tmp_path)
    assert ".codex/config.toml" in result["preserved"]
    assert "enabled = false" in c.read_text()
    before = files(tmp_path)
    with pytest.raises(FactoryError) as caught:
        install(tmp_path, selected="codex", skip_sync=True)
    message = str(caught.value)
    assert "`.codex/config.toml` [agents].enabled is set by you to false" in message
    assert "the factory needs true" in message and "change or remove it, then rerun" in message
    assert files(tmp_path) == before


@pytest.mark.parametrize("changed", [False, True])
def test_compatible_codex_key_reinstalls(tmp_path, changed):
    install(tmp_path, selected="codex", skip_sync=True)
    if changed:
        _codex_toggle(
            tmp_path, "max_concurrent_threads_per_session = 3", "max_concurrent_threads_per_session = 5"
        )
    uninstall(tmp_path)
    install(tmp_path, selected="codex", skip_sync=True)
    import tomllib

    agents = tomllib.loads((tmp_path / ".codex/config.toml").read_text())["agents"]
    assert agents["enabled"] is True
    assert agents["max_concurrent_threads_per_session"] == (5 if changed else 3)
    render(tmp_path, check=True)


ORCHESTRATOR = ".claude/agents/factory-orchestrator.md"
ALL_PROFILES = [
    "claude",
    "codex",
    "copilot",
    "claude,codex",
    "claude,copilot",
    "codex,copilot",
    "claude,codex,copilot",
]


def set_enforcement(root, **flags):
    config = json.loads((root / "factory.json").read_text())
    config["enforcement"] = {"claude_orchestrator_agent": False, **flags}
    (root / "factory.json").write_text(json.dumps(config, indent=2) + "\n")


def frontmatter(path):
    import yaml

    return yaml.safe_load(path.read_text().split("---\n")[1])


@pytest.mark.parametrize("profile", ALL_PROFILES)
@pytest.mark.parametrize("enforced", [False, True])
def test_specialists_cannot_dispatch_and_orchestrators_cannot_edit(tmp_path, profile, enforced):
    install(tmp_path, selected=profile, skip_sync=True)
    if enforced:
        set_enforcement(tmp_path, claude_orchestrator_agent=True)
        render(tmp_path)
    assert render(tmp_path, check=True)["ok"]
    selected = profile.split(",")
    for path in (tmp_path / ".claude/agents").glob("*.md"):
        tools = [t.strip() for t in frontmatter(path)["tools"].split(", ")]
        if path.name == "factory-orchestrator.md":
            assert not {"Edit", "Write", "MultiEdit", "NotebookEdit"} & set(tools)
            continue
        assert not any(t.startswith(("Agent", "Task")) for t in tools), path
    for path in (tmp_path / ".github/agents").glob("*.agent.md"):
        header = frontmatter(path)
        if path.name == "factory.agent.md":
            assert "edit" not in header["tools"] and "agent" in header["tools"]
            continue
        assert "agent" not in header["tools"] and header["agents"] == [], path
    for path in (tmp_path / ".codex/agents").glob("*.toml"):
        assert "agent" not in path.read_text().split("developer_instructions")[0].lower().replace("name", "")
    assert (tmp_path / ORCHESTRATOR).is_file() == (enforced and "claude" in selected)
    assert not (tmp_path / ".github/hooks").exists()
    assert (tmp_path / ".factory/hooks/orchestrator_guard.py").is_file()


def test_orchestrator_agent_export(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    assert not (tmp_path / ORCHESTRATOR).exists()
    assert json.loads((tmp_path / "factory.json").read_text())["model_selection"] == {"mode": "inherit"}
    set_enforcement(tmp_path, claude_orchestrator_agent=True)
    assert ORCHESTRATOR in render(tmp_path)["changed"]
    header = frontmatter(tmp_path / ORCHESTRATOR)
    assert header["name"] == "factory-orchestrator"
    assert header["tools"] == (
        "Agent(factory-planner, factory-implementer, factory-verifier, factory-reviewer), Read, Glob, Grep, Bash"
    )
    [entry] = header["hooks"]["PreToolUse"]
    assert entry["matcher"] == "*"
    [hook] = entry["hooks"]
    assert hook["type"] == "command"
    assert '"${CLAUDE_PROJECT_DIR}/.factory/.venv/bin/python"' in hook["command"]
    assert "-I -B" in hook["command"] and "--mode" not in hook["command"]
    assert hook["command"].endswith("|| exit 2")
    text = (tmp_path / ORCHESTRATOR).read_text()
    # No YAML line folding: the tools list and the hook command are each one physical line.
    lines = text.split("---\n")[1].splitlines()
    assert f"tools: {header['tools']}" in lines
    [command_line] = [line for line in lines if line.lstrip().startswith("command:")]
    assert yaml.safe_load(command_line.strip())["command"] == hook["command"]
    assert "Skill" not in header["tools"]
    role = (tmp_path / ".factory/roles/orchestrator.md").read_text().strip()
    assert role in text and "claude --agent factory-orchestrator" in text
    assert "claude --agent factory-orchestrator" in (tmp_path / "CLAUDE.md").read_text()
    assert "PreToolUse guard via" in (tmp_path / "AGENTS.md").read_text()
    lock_data = lock(tmp_path)
    assert lock_data["generated"][ORCHESTRATOR]["kind"] == "file"
    assert ".factory/hooks/orchestrator_guard.py" in lock_data["sources"]
    assert render(tmp_path, check=True)["ok"]


def test_exported_agent_frontmatter_is_not_folded(tmp_path):
    install(tmp_path, selected="claude,copilot", skip_sync=True)
    set_enforcement(tmp_path, claude_orchestrator_agent=True)
    render(tmp_path)
    paths = [*(tmp_path / ".claude/agents").glob("*.md"), *(tmp_path / ".github/agents").glob("*.agent.md")]
    assert len(paths) >= 9
    for path in paths:
        header = frontmatter(path)
        lines = path.read_text().split("---\n")[1].splitlines()
        for key in ("name", "description", "tools"):
            value = header[key]
            if isinstance(value, str):
                [line] = [line for line in lines if line.startswith(f"{key}:")]
                assert yaml.safe_load(line)[key] == value, (path, key)


@pytest.mark.parametrize("name", ['we"ird $HOME `id` $(id) dir', "quote'd $1 dir"])
def test_orchestrator_hook_command_is_safe_for_unusual_project_paths(tmp_path, name):
    """$CLAUDE_PROJECT_DIR is expanded once inside double quotes; its value is never re-parsed."""
    import os
    import sys

    project = tmp_path / name
    project.mkdir()
    install(project, selected="claude", skip_sync=True)
    set_enforcement(project, claude_orchestrator_agent=True)
    render(project)
    command = frontmatter(project / ORCHESTRATOR)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    stubs = tmp_path / "stub-bin"
    stubs.mkdir()
    (stubs / "python3").symlink_to(sys.executable)
    # If the path were re-expanded, $HOME/$1/$(id) would change it and the guard would not be found.
    env = {
        "PATH": f"{stubs}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "CLAUDE_PROJECT_DIR": str(project),
        "HOME": "/x",
    }

    def call(tool, tool_input):
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input}
        return subprocess.run(
            ["sh", "-c", command],
            cwd=tmp_path,
            env=env,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    denied = call("Write", {"file_path": "src/a.py", "content": ""})
    assert denied.returncode == 2
    assert json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    allowed = call("Bash", {"command": "software-factory status"})
    assert (allowed.returncode, allowed.stdout, allowed.stderr) == (0, "", "")
    # The guard really ran from the unusual path: without it the same call fails closed.
    (project / ".factory/hooks/orchestrator_guard.py").rename(tmp_path / "guard.saved")
    missing = call("Bash", {"command": "software-factory status"})
    assert missing.returncode == 2 and name in missing.stderr


def test_orchestrator_hook_command_runs_guard(tmp_path):
    import os
    import sys

    install(tmp_path, selected="claude", skip_sync=True)
    set_enforcement(tmp_path, claude_orchestrator_agent=True)
    render(tmp_path)
    command = frontmatter(tmp_path / ORCHESTRATOR)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    stubs = tmp_path.parent / "stub-bin"
    stubs.mkdir()
    (stubs / "python3").symlink_to(sys.executable)
    env = {"PATH": f"{stubs}{os.pathsep}/usr/bin{os.pathsep}/bin", "CLAUDE_PROJECT_DIR": str(tmp_path)}

    def call(tool, tool_input):
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input}
        return subprocess.run(
            ["sh", "-c", command],
            cwd=tmp_path,
            env=env,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    denied = call("Write", {"file_path": "src/a.py", "content": ""})
    assert denied.returncode == 2
    assert json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert call("Bash", {"command": "software-factory status"}).returncode == 0
    # A missing guard fails closed.
    (tmp_path / ".factory/hooks/orchestrator_guard.py").rename(tmp_path / "guard.saved")
    assert call("Read", {"file_path": "a"}).returncode == 2


def test_enforcement_toggle_removes_and_relinquishes_exports(tmp_path):
    install(tmp_path, selected="claude,copilot", skip_sync=True)
    before = files(tmp_path)
    set_enforcement(tmp_path, claude_orchestrator_agent=True)
    render(tmp_path)
    assert ORCHESTRATOR in lock(tmp_path)["generated"]
    set_enforcement(tmp_path)
    changed = render(tmp_path)["changed"]
    assert ORCHESTRATOR in changed
    assert not (tmp_path / ORCHESTRATOR).exists()
    assert ORCHESTRATOR not in lock(tmp_path)["generated"]
    after = files(tmp_path)
    after.pop("factory.json"), before.pop("factory.json")
    assert after == before
    # An edited export blocks the toggle until restored or deleted.
    set_enforcement(tmp_path, claude_orchestrator_agent=True)
    render(tmp_path)
    (tmp_path / ORCHESTRATOR).write_text((tmp_path / ORCHESTRATOR).read_text() + "local edit\n")
    set_enforcement(tmp_path)
    with pytest.raises(FactoryError, match="drift"):
        render(tmp_path)
    # A deleted export is relinquished by upgrade and recreated by an explicit render.
    set_enforcement(tmp_path, claude_orchestrator_agent=True)
    (tmp_path / ORCHESTRATOR).unlink()
    install(tmp_path, upgrade=True, skip_sync=True)
    assert lock(tmp_path)["relinquished"] == [ORCHESTRATOR]
    render(tmp_path)
    assert (tmp_path / ORCHESTRATOR).is_file() and "relinquished" not in lock(tmp_path)
    # Uninstall removes unchanged enforcement exports.
    uninstall(tmp_path)
    assert not (tmp_path / ORCHESTRATOR).exists()


def test_user_owned_orchestrator_agent_collides(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    (tmp_path / ORCHESTRATOR).write_text("user-owned agent\n")
    set_enforcement(tmp_path, claude_orchestrator_agent=True)
    with pytest.raises(FactoryError, match="Unowned file collision"):
        render(tmp_path)
    assert (tmp_path / ORCHESTRATOR).read_text() == "user-owned agent\n"


def test_doctor_reports_enforcement_read_only(tmp_path):
    from software_factory.cli import doctor

    install(tmp_path, selected="claude,codex,copilot", skip_sync=True)
    set_enforcement(tmp_path, claude_orchestrator_agent=True)
    render(tmp_path)
    before = files(tmp_path)
    report = doctor(tmp_path)
    assert files(tmp_path) == before
    assert report["enforcement"]["claude"]["layer"] == "hook" and report["enforcement"]["claude"]["enabled"]
    assert report["enforcement"]["claude"]["capabilities"]["blocking_hooks"] == "fail_closed"
    assert report["enforcement"]["codex"]["layer"] == "instructions"
    assert report["enforcement"]["codex"]["opt_in"] is None
    assert report["enforcement"]["copilot"]["layer"] == "tool_allowlist"
    assert report["enforcement"]["copilot"]["capabilities"]["blocking_hooks"] == "none"
    assert "enforcement_inactive" not in {i["code"] for i in report["issues"]}
    # Without the claude profile the flag has no effect and doctor says so.
    config = json.loads((tmp_path / "factory.json").read_text())
    config["profile"] = ["codex", "copilot"]
    (tmp_path / "factory.json").write_text(json.dumps(config, indent=2) + "\n")
    codes = {i["code"]: i for i in doctor(tmp_path)["issues"]}
    assert "claude_orchestrator_agent" in codes["enforcement_inactive"]["message"]
    assert codes["enforcement_inactive"]["severity"] == "warning"


def test_legacy_factory_config_remains_valid(tmp_path):
    from software_factory.core import validate

    install(tmp_path, selected="claude", skip_sync=True)
    config = json.loads((tmp_path / "factory.json").read_text())
    legacy = {k: v for k, v in config.items() if k != "enforcement"}
    legacy["model_selection"] = {"mode": "recommend"}
    legacy["limits"] = {k: v for k, v in config["limits"].items() if k != "high_risk_lines"}
    validate(tmp_path, "factory", legacy)
    for bad in ({"high_risk_lines": 49}, {"high_risk_lines": "400"}):
        with pytest.raises(FactoryError, match="Invalid factory"):
            validate(tmp_path, "factory", {**config, "limits": {**config["limits"], **bad}})
    with pytest.raises(FactoryError, match="Invalid factory"):
        validate(tmp_path, "factory", {**config, "enforcement": {"copilot_hooks": True}})
