"""0.3.0 request-bearing missions: request records, criteria, briefs, risk and review kinds."""

from __future__ import annotations

import argparse
import io
import json
import sys
from types import SimpleNamespace

import pytest
from test_workflow import commit, make_repo, result_for

from software_factory.checks import verify_mission
from software_factory.core import FactoryError, asset_root, hash_file, read_json, sha256, write_json
from software_factory.workflow import (
    LEGACY_WARNING,
    add_parser,
    assess_gate,
    create_packet,
    load_mission,
    record_decision,
    record_result,
    transition_mission,
    transition_task,
)

REQUEST = "Please change VALUE to 2 in src/app.py.\n\nKeep   the module importable.\n"
SPEC = "# Specification\n\nChange VALUE to 2.\n\n## Risks\n\nNo external services or persistent data.\n"
PLAN = (
    "# Plan\n\nChange the value and verify it.\n\n## Architecture\n\n```mermaid\nflowchart LR\n"
    "  app[src/app.py] --> unit[unit check]\n```\n\n## Risks\n\nA caller may rely on the old constant.\n"
)
CONTEXT = "# Context\n\nsrc/app.py defines VALUE; the unit check runs a Python one-liner.\n"
CRITERIA = {
    "items": [
        {
            "id": "AC-1",
            "text": "VALUE equals 2",
            "excerpts": ["change VALUE to 2 in src/app.py"],
            "route": "check",
            "checks": ["unit"],
        }
    ]
}


def cli(root, *argv, stdin=None, monkeypatch=None):
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(list(argv))
    args.root = root
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(stdin)))
    return args.handler(args)


def put(root, relative, value):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, str):
        target.write_text(value)
    else:
        write_json(root, relative, value)
    return relative


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "project")


def create(root, id="M-REQ", kind="patch", request=REQUEST):
    put(root, ".factory/local/request.md", request)
    return cli(
        root,
        "mission",
        "create",
        "--id",
        id,
        "--title",
        "Change value",
        "--kind",
        kind,
        "--request-file",
        ".factory/local/request.md",
    )


def author(root, id, plan=PLAN, context=CONTEXT, spec=SPEC):
    directory = root / ".factory/missions" / id
    for name, text in (("spec.md", spec), ("plan.md", plan), ("context.md", context)):
        if text is not None:
            (directory / name).write_text(text)
    (directory / "recovery.md").write_text("# Recovery\n\nRevert the constant; no migration is involved.\n")
    record_decision(
        root,
        id,
        {
            "id": "D-SCOPE-" + hash_file(root, f".factory/missions/{id}/spec.md")[:8],
            "kind": "scope",
            "reference": "User authorized the specification",
            "subject_hash": hash_file(root, f".factory/missions/{id}/spec.md"),
        },
    )


def criteria(root, id, value=CRITERIA):
    return cli(
        root, "mission", "criteria", "--mission", id, "--input", put(root, ".factory/local/c.json", value)
    )


def plan_mission(root, id="M-REQ", kind="patch", owned=("src/**",), value=CRITERIA):
    create(root, id, kind)
    author(root, id)
    criteria(root, id, value)
    cli(root, "mission", "accept-scope", "--mission", id)
    task = {"id": "T-ONE", "title": "Change app", "owned_paths": list(owned), "checks": ["unit"]}
    task["criteria"] = [i["id"] for i in value["items"]]
    cli(root, "mission", "task-add", "--mission", id, "--input", put(root, ".factory/local/t.json", task))
    return id


def implement(root, id, change=None, evidence=None, changed=None):
    transition_mission(root, id, "IMPLEMENTING")
    transition_task(root, id, "T-ONE", "RUNNING")
    (change or (lambda: (root / "src/app.py").write_text("VALUE = 2\n")))()
    transition_task(root, id, "T-ONE", "VERIFYING")
    transition_mission(root, id, "VERIFYING")
    verified = verify_mission(root, id, "R-ONE")
    assert verified["pass"], verified
    result = result_for(root, id, verified, changed)
    result["criteria_evidence"] = evidence or {"AC-1": ["check:unit"]}
    record_result(root, id, result)
    transition_task(root, id, "T-ONE", "DONE")
    transition_mission(root, id, "REVIEWING")
    return verified


def review(root, id, kind="code", verdict="pass", status="pass", **extra):
    record = {
        "id": f"V-{kind}-{len(load_mission(root, id)['reviews']) + 1}",
        "kind": kind,
        "author": "independent-reviewer",
        "status": status,
        "fingerprint": load_mission_fingerprint(root, id),
        "findings": [],
        "criteria_verdicts": {"AC-1": verdict},
        **extra,
    }
    return cli(
        root, "mission", "review", "--mission", id, "--input", put(root, ".factory/local/r.json", record)
    )


def load_mission_fingerprint(root, id):
    from software_factory.evidence import fingerprint

    return fingerprint(root, load_mission(root, id))["fingerprint"]


def brief(root, id, kind):
    return cli(root, "mission", "brief", "--mission", id, "--kind", kind)


def test_small_lane_reaches_ready_pr_with_traceable_packet(repo):
    id = plan_mission(repo)
    mission = load_mission(repo, id)
    assert (repo / f".factory/missions/{id}/request.md").read_text() == REQUEST
    assert mission["request"]["sha256"] == sha256(REQUEST) == mission["request"]["chain"]
    assert mission["criteria_hash"] and mission["tasks"][0]["criteria"] == ["AC-1"]
    implement(repo, id)
    assert any("independent code review" in r for r in assess_gate(repo, id)["reasons"])
    review(repo, id)
    gate = assess_gate(repo, id)
    assert gate["pass"], gate
    assert gate["lane"] == "small" and gate["risk"]["tier"] == "low" and gate["required_reviews"] == ["code"]
    assert gate["warnings"] == []
    transition_mission(repo, id, "READY_PR")
    text = (repo / create_packet(repo, id)["path"]).read_text()
    assert "## Architecture" in text and "```mermaid\nflowchart LR" in text
    assert "| Request | Criterion | Evidence | Verdict |" in text
    assert "| change VALUE to 2 in src/app.py | AC-1: VALUE equals 2 | check:unit | pass |" in text
    assert "tier: low" in text and "code review V-code" in text


def test_create_requires_request_file_and_accepts_stdin(repo, monkeypatch):
    with pytest.raises(FactoryError, match="--request-file"):
        cli(repo, "mission", "create", "--id", "M-NONE", "--title", "No request")
    with pytest.raises(FactoryError, match="empty"):
        cli(
            repo,
            "mission",
            "create",
            "--id",
            "M-EMPTY",
            "--title",
            "T",
            "--request-file",
            "-",
            stdin=b"  \n",
            monkeypatch=monkeypatch,
        )
    with pytest.raises(FactoryError, match="UTF-8"):
        cli(
            repo,
            "mission",
            "create",
            "--id",
            "M-BAD",
            "--title",
            "T",
            "--request-file",
            "-",
            stdin=b"\xff\xfe",
            monkeypatch=monkeypatch,
        )
    with pytest.raises(FactoryError, match="256 KiB"):
        cli(
            repo,
            "mission",
            "create",
            "--id",
            "M-BIG",
            "--title",
            "T",
            "--request-file",
            "-",
            stdin=b"a" * 262145,
            monkeypatch=monkeypatch,
        )
    raw = "Exact bytes\r\nwith CRLF and ünïcode\n".encode()
    cli(
        repo,
        "mission",
        "create",
        "--id",
        "M-STDIN",
        "--title",
        "T",
        "--request-file",
        "-",
        stdin=raw,
        monkeypatch=monkeypatch,
    )
    assert (repo / ".factory/missions/M-STDIN/request.md").read_bytes() == raw
    assert load_mission(repo, "M-STDIN")["request"]["sha256"] == sha256(raw)
    assert not (repo / ".factory/missions/M-NONE").exists()


def test_stdin_inputs_and_record_doc(repo, monkeypatch):
    create(repo)
    id = "M-REQ"
    for doc, text in (("spec", SPEC), ("plan", PLAN), ("context", CONTEXT)):
        out = cli(
            repo,
            "mission",
            "record-doc",
            "--mission",
            id,
            "--doc",
            doc,
            "--input",
            "-",
            stdin=text.encode(),
            monkeypatch=monkeypatch,
        )
        assert out == {"path": f".factory/missions/{id}/{doc}.md", "sha256": sha256(text)}
        assert (repo / out["path"]).read_text() == text
    recovery = put(repo, ".factory/local/recovery.md", "# Recovery\n\nRevert the constant.\n")
    cli(repo, "mission", "record-doc", "--mission", id, "--doc", "recovery", "--input", recovery)
    decision = {
        "id": "D-SCOPE",
        "kind": "scope",
        "reference": "User authorized",
        "subject_hash": hash_file(repo, f".factory/missions/{id}/spec.md"),
    }
    cli(
        repo,
        "mission",
        "decision",
        "--mission",
        id,
        "--input",
        "-",
        stdin=json.dumps(decision).encode(),
        monkeypatch=monkeypatch,
    )
    cli(
        repo,
        "mission",
        "criteria",
        "--mission",
        id,
        "--input",
        "-",
        stdin=json.dumps(CRITERIA).encode(),
        monkeypatch=monkeypatch,
    )
    with pytest.raises(FactoryError, match="Cannot read JSON from stdin"):
        cli(
            repo,
            "mission",
            "criteria",
            "--mission",
            id,
            "--input",
            "-",
            stdin=b"{not json",
            monkeypatch=monkeypatch,
        )
    with pytest.raises(FactoryError, match="empty"):
        cli(repo, "mission", "criteria", "--mission", id, "--input", "-", stdin=b"", monkeypatch=monkeypatch)
    cli(repo, "mission", "accept-scope", "--mission", id)
    task = {
        "id": "T-ONE",
        "title": "Change app",
        "owned_paths": ["src/**"],
        "checks": ["unit"],
        "criteria": ["AC-1"],
    }
    cli(
        repo,
        "mission",
        "task-add",
        "--mission",
        id,
        "--input",
        "-",
        stdin=json.dumps(task).encode(),
        monkeypatch=monkeypatch,
    )
    transition_mission(repo, id, "IMPLEMENTING")
    with pytest.raises(FactoryError, match="PROPOSED or PLANNED"):
        cli(
            repo,
            "mission",
            "record-doc",
            "--mission",
            id,
            "--doc",
            "plan",
            "--input",
            "-",
            stdin=b"# Plan\n",
            monkeypatch=monkeypatch,
        )
    out = cli(
        repo,
        "mission",
        "record-doc",
        "--mission",
        id,
        "--doc",
        "handoff",
        "--input",
        "-",
        stdin=b"# Handoff\n",
        monkeypatch=monkeypatch,
    )
    assert (repo / out["path"]).read_bytes() == b"# Handoff\n"
    cli(
        repo,
        "mission",
        "clarify",
        "--mission",
        id,
        "--input",
        "-",
        stdin=b"Also keep VALUE an int.",
        monkeypatch=monkeypatch,
    )
    assert load_mission(repo, id)["state"] == "PROPOSED"


def test_accept_scope_refusals(repo):
    create(repo)
    id = "M-REQ"
    author(repo, id)
    with pytest.raises(FactoryError, match="Acceptance criteria are required"):
        cli(repo, "mission", "accept-scope", "--mission", id)
    with pytest.raises(FactoryError, match="excerpt matches nothing"):
        criteria(repo, id, {"items": [{**CRITERIA["items"][0], "excerpts": ["change VALUE to 3"]}]})
    with pytest.raises(FactoryError, match="at least 8 characters after whitespace normalisation"):
        criteria(repo, id, {"items": [{**CRITERIA["items"][0], "excerpts": ["   "]}]})
    with pytest.raises(FactoryError, match="Criterion AC-1 excerpt must be at least 8 characters"):
        criteria(repo, id, {"items": [{**CRITERIA["items"][0], "excerpts": ["VALUE\n   t"]}]})
    with pytest.raises(FactoryError, match="Exclusion excerpt must be at least 8 characters"):
        criteria(repo, id, {**CRITERIA, "exclusions": [{"excerpt": " Keep\n", "decision": "D-NONE"}]})
    with pytest.raises(FactoryError, match="existing decision"):
        criteria(repo, id, {**CRITERIA, "exclusions": [{"excerpt": "Keep the module", "decision": "D-NONE"}]})
    with pytest.raises(FactoryError, match="route check"):
        criteria(repo, id, {"items": [{**CRITERIA["items"][0], "checks": []}]})
    with pytest.raises(FactoryError, match="unique"):
        criteria(repo, id, {"items": [CRITERIA["items"][0], CRITERIA["items"][0]]})
    # Whitespace runs are normalised on both sides.
    criteria(repo, id, {"items": [{**CRITERIA["items"][0], "excerpts": ["Keep the\n module   importable."]}]})
    question = {"id": "Q-1", "text": "Should VALUE be configurable?", "status": "open", "decision": None}
    criteria(repo, id, {**CRITERIA, "ambiguities": [question]})
    with pytest.raises(FactoryError, match="Open ambiguities.*Q-1"):
        cli(repo, "mission", "accept-scope", "--mission", id)
    record_decision(
        repo,
        id,
        {"id": "D-WAIVE", "kind": "exception", "reference": "User: not needed", "subject_hash": "a" * 64},
    )
    criteria(
        repo,
        id,
        {
            **CRITERIA,
            "ambiguities": [{**question, "status": "waived", "decision": "D-WAIVE"}],
            "exclusions": [{"excerpt": "Keep the module importable.", "decision": "D-WAIVE"}],
        },
    )
    directory = repo / ".factory/missions" / id
    template = (asset_root() / "templates/context.md").read_text()
    for name, text, message in (
        ("context.md", template.replace("\n\n", "\n \n"), "context.md is missing or still"),
        ("context.md", "# Context\n\n## Codebase map\n\n", "context.md adds no content"),
        ("context.md", "# Mine\n\n" + template, "context.md adds no content"),
        ("plan.md", PLAN.replace("```mermaid", "```text"), "section has no fenced ```mermaid block"),
        ("plan.md", PLAN.replace("flowchart LR", "pie title nope"), "unknown diagram type 'pie'"),
        ("plan.md", PLAN.replace("## Architecture", "## Design"), "no '## Architecture' section"),
        ("plan.md", PLAN.replace("## Architecture", "### Architecture"), "no '## Architecture' section"),
        (
            "plan.md",
            "# Plan\n\n```text\n## Architecture\n```\n\n```mermaid\nflowchart LR\n  a --> b\n```\n",
            "no '## Architecture' section",
        ),
        (
            "plan.md",
            PLAN.replace("  app[src/app.py] --> unit[unit check]\n", "  %% nothing yet\n\n"),
            "the diagram is empty: add nodes or edges after 'flowchart LR'",
        ),
        ("plan.md", PLAN.replace("flowchart LR\n", "%% only a comment\n"), "unknown diagram type"),
        (
            "plan.md",
            PLAN.replace("flowchart LR\n  app", "---\ntitle: x\nflowchart LR\n  app"),
            "front matter has no closing ---",
        ),
    ):
        directory.joinpath(name).write_text(text)
        with pytest.raises(FactoryError, match=message):
            cli(repo, "mission", "accept-scope", "--mission", id)
        directory.joinpath(name).write_text({"context.md": CONTEXT, "plan.md": PLAN}[name])
    plan_template = (directory / "plan.md").read_text()
    example = (asset_root() / "templates/plan.md").read_text()
    directory.joinpath("plan.md").write_text(example + "\nEdited elsewhere.\n")
    with pytest.raises(FactoryError, match="still the template example"):
        cli(repo, "mission", "accept-scope", "--mission", id)
    # Whitespace changes do not disguise the template example.
    reindented = example.replace("flowchart LR\n  ", "flowchart   LR\n\n      ").replace(" --> ", "  -->  ")
    assert reindented != example
    directory.joinpath("plan.md").write_text(reindented + "\nEdited elsewhere.\n")
    with pytest.raises(FactoryError, match="still the template example"):
        cli(repo, "mission", "accept-scope", "--mission", id)

    directory.joinpath("plan.md").write_text(plan_template)
    request = directory / "request.md"
    request.write_text(REQUEST + "tampered\n")
    with pytest.raises(FactoryError, match="request.md does not match"):
        cli(repo, "mission", "accept-scope", "--mission", id)
    request.unlink()
    with pytest.raises(FactoryError, match="request.md is missing"):
        cli(repo, "mission", "accept-scope", "--mission", id)
    request.write_text(REQUEST)
    assert cli(repo, "mission", "accept-scope", "--mission", id)["state"] == "PLANNED"


def test_task_and_result_criteria_must_exist(repo):
    id = plan_mission(repo)
    bad = {
        "id": "T-TWO",
        "title": "Other",
        "owned_paths": ["docs/**"],
        "checks": ["unit"],
        "criteria": ["AC-9"],
    }
    with pytest.raises(FactoryError, match="unknown acceptance criteria: AC-9"):
        cli(repo, "mission", "task-add", "--mission", id, "--input", put(repo, ".factory/local/t2.json", bad))
    transition_mission(repo, id, "IMPLEMENTING")
    transition_task(repo, id, "T-ONE", "RUNNING")
    (repo / "src/app.py").write_text("VALUE = 2\n")
    transition_task(repo, id, "T-ONE", "VERIFYING")
    transition_mission(repo, id, "VERIFYING")
    verified = verify_mission(repo, id, "R-ONE")
    result = {**result_for(repo, id, verified), "criteria_evidence": {"AC-7": ["note:done"]}}
    with pytest.raises(FactoryError, match="unknown criteria: AC-7"):
        record_result(repo, id, result)


def test_clarify_after_planned_forces_reacceptance(repo):
    id = plan_mission(repo)
    text = put(repo, ".factory/local/clarify.md", "The new VALUE must stay an integer.")
    mission = cli(repo, "mission", "clarify", "--mission", id, "--input", text)
    assert mission["state"] == "PROPOSED" and mission["criteria_hash"] is None
    assert [t["status"] for t in mission["tasks"]] == ["TODO"]
    entry = mission["request"]["clarifications"][0]
    assert entry["prev"] == sha256(REQUEST) and entry["sha256"] == sha256(
        "The new VALUE must stay an integer."
    )
    assert mission["request"]["chain"] == sha256(f"{sha256(REQUEST)}:{entry['sha256']}")
    content = (repo / f".factory/missions/{id}/clarifications.md").read_text()
    assert (
        content
        == f"# Clarifications\n\n## Clarification 1 ({entry['at']})\n\nThe new VALUE must stay an integer.\n\n"
    )
    with pytest.raises(FactoryError, match="Invalid transition PROPOSED -> IMPLEMENTING"):
        transition_mission(repo, id, "IMPLEMENTING")
    # Clarified text is citable.
    extra = {"id": "AC-2", "text": "Integer", "excerpts": ["must stay an integer"], "route": "review"}
    criteria(repo, id, {"items": [*CRITERIA["items"], extra]})
    cli(repo, "mission", "accept-scope", "--mission", id)
    assert load_mission(repo, id)["state"] == "PLANNED"
    # A tampered clarification breaks the chain.
    path = repo / f".factory/missions/{id}/clarifications.md"
    path.write_text(content.replace("integer", "string"))
    with pytest.raises(FactoryError, match="Clarification 1 does not match"):
        transition_mission(repo, id, "IMPLEMENTING")


def test_criteria_change_after_planned_forces_reacceptance(repo):
    id = plan_mission(repo)
    changed = {"items": [{**CRITERIA["items"][0], "text": "VALUE equals two"}]}
    assert criteria(repo, id, changed)["state"] == "PLANNED"
    with pytest.raises(FactoryError, match="Acceptance criteria changed"):
        transition_mission(repo, id, "IMPLEMENTING")
    cli(repo, "mission", "accept-scope", "--mission", id)
    transition_mission(repo, id, "IMPLEMENTING")


def test_feature_lane_requires_acceptance_review_with_current_brief(repo):
    id = plan_mission(repo, kind="feature")
    early = brief(repo, id, "acceptance")
    implement(repo, id)
    review(repo, id)
    gate = assess_gate(repo, id)
    assert gate["required_reviews"] == ["code", "acceptance"]
    assert any("acceptance review" in r for r in gate["reasons"])
    review(repo, id, "acceptance")
    assert any("must record brief_hash" in r for r in assess_gate(repo, id)["reasons"])
    review(repo, id, "acceptance", brief_hash=early["sha256"])
    assert any("brief_hash does not match" in r for r in assess_gate(repo, id)["reasons"])
    current = brief(repo, id, "acceptance")
    assert current["sha256"] != early["sha256"]
    review(repo, id, "acceptance", brief_hash=current["sha256"])
    gate = assess_gate(repo, id)
    assert gate["pass"], gate
    transition_mission(repo, id, "READY_PR")


def test_needs_human_verdict_blocks_gate(repo):
    id = plan_mission(repo)
    implement(repo, id)
    review(repo, id, verdict="needs_human")
    reasons = assess_gate(repo, id)["reasons"]
    assert any("verdict for AC-1 is needs_human" in r for r in reasons)
    review(repo, id)
    assert assess_gate(repo, id)["pass"]


def test_later_code_review_does_not_hide_failing_acceptance_review(repo):
    id = plan_mission(repo, kind="feature")
    implement(repo, id)
    accepted = brief(repo, id, "acceptance")["sha256"]
    review(repo, id, "acceptance", verdict="fail", status="changes_requested", brief_hash=accepted)
    review(repo, id)
    reasons = assess_gate(repo, id)["reasons"]
    assert any("acceptance review" in r for r in reasons)
    assert any("verdict for AC-1 is fail" in r for r in reasons)
    # A failing optional kind is not hidden either.
    review(repo, id, "acceptance", brief_hash=accepted)
    review(repo, id, "adversarial", status="changes_requested", verdict="fail")
    assert any("Latest adversarial review" in r for r in assess_gate(repo, id)["reasons"])


def test_high_risk_requires_acceptance_and_adversarial_reviews(repo):
    (repo / "tests").mkdir()
    (repo / "tests/test_old.py").write_text("def test_old():\n    assert True\n")
    commit(repo, "add test")
    id = plan_mission(repo, owned=("src/**", "tests/**"))
    implement(
        repo,
        id,
        change=lambda: (
            (repo / "tests/test_old.py").unlink() or (repo / "src/app.py").write_text("VALUE = 2\n")
        ),
        changed=["src/app.py", "tests/test_old.py"],
    )
    risk = cli(repo, "mission", "risk", "--mission", id)
    assert risk["tier"] == "high" and "Test file deleted: tests/test_old.py" in risk["reasons"]
    review(repo, id)
    gate = assess_gate(repo, id)
    assert gate["required_reviews"] == ["code", "acceptance", "adversarial"]
    assert any("adversarial review" in r for r in gate["reasons"])
    review(repo, id, "acceptance", brief_hash=brief(repo, id, "acceptance")["sha256"])
    review(repo, id, "adversarial")
    gate = assess_gate(repo, id)
    assert gate["pass"], gate
    transition_mission(repo, id, "READY_PR")
    text = (repo / create_packet(repo, id)["path"]).read_text()
    assert "tier: high" in text and "Test file deleted" in text


@pytest.mark.parametrize(
    "path,content,reason",
    [
        (".github/workflows/ci.yml", "on: push\n", "CI/workflow path changed: .github/workflows/ci.yml"),
        ("src/big.py", "".join(f"X{n} = {n}\n" for n in range(401)), "Diff size 401 lines exceeds"),
        ("requirements-dev.txt", "pytest\n", "Dependency manifest or lockfile changed"),
        ("tests/test_new.py", "def test_new():\n    pass\n", None),
    ],
)
def test_risk_is_live_and_read_only(repo, path, content, reason):
    id = plan_mission(repo)
    mission_file = repo / f".factory/missions/{id}/mission.json"
    before, index = mission_file.read_bytes(), (repo / ".git/index").read_bytes()
    assert cli(repo, "mission", "risk", "--mission", id)["tier"] == "low"
    put(repo, path, content)
    risk = cli(repo, "mission", "risk", "--mission", id)
    if reason:
        assert risk["tier"] == "high" and any(r.startswith(reason) for r in risk["reasons"]), risk
    else:
        assert risk["tier"] == "low", risk
    assert mission_file.read_bytes() == before and (repo / ".git/index").read_bytes() == index


def test_check_reference_to_failed_check_blocks(repo):
    config = read_json(repo, "factory.json")
    config["checks"].append(
        {
            "id": "extra",
            "command": [sys.executable, "-c", "raise SystemExit(1)"],
            "cwd": ".",
            "required": False,
            "timeout_seconds": 5,
        }
    )
    write_json(repo, "factory.json", config)
    commit(repo, "optional check")
    id = plan_mission(repo)
    implement(repo, id, evidence={"AC-1": ["check:unit", "check:extra", "evidence:missing.txt", "note:seen"]})
    review(repo, id)
    reasons = assess_gate(repo, id)["reasons"]
    assert any("cites check extra, which did not pass" in r for r in reasons)
    assert any("missing evidence file: missing.txt" in r for r in reasons)
    assert not any("check unit" in r for r in reasons)


def test_tampered_request_blocks_gate(repo):
    id = plan_mission(repo)
    implement(repo, id)
    review(repo, id)
    assert assess_gate(repo, id)["pass"]
    request = repo / f".factory/missions/{id}/request.md"
    request.write_text(REQUEST.replace("2", "3"))
    assert any("request.md does not match" in r for r in assess_gate(repo, id)["reasons"])
    request.write_text(REQUEST)
    (repo / f".factory/missions/{id}/clarifications.md").write_text("# Clarifications\n\n")
    assert any("no clarification is recorded" in r for r in assess_gate(repo, id)["reasons"])


def test_legacy_mission_reaches_ready_pr_with_warning(repo):
    from test_workflow import begin, finish

    id = begin(repo)
    (repo / "src/app.py").write_text("VALUE = 2\n")
    finish(repo, id)
    gate = assess_gate(repo, id)
    assert gate["pass"] and gate["warnings"] == [LEGACY_WARNING] and "risk" not in gate
    assert LEGACY_WARNING in (repo / create_packet(repo, id)["path"]).read_text()
    with pytest.raises(FactoryError, match="legacy mission"):
        cli(repo, "mission", "brief", "--mission", id, "--kind", "code")


def test_briefs_are_deterministic_and_diff_includes_untracked_files(repo):
    id = plan_mission(repo)
    (repo / "src/app.py").write_text("VALUE = 2\n")
    (repo / "src/new_module.py").write_text("NEW = True\n")
    first = brief(repo, id, "code")
    text = (repo / first["path"]).read_bytes()
    patch = (repo / first["diff_path"]).read_bytes()
    second = brief(repo, id, "code")
    assert first == second and (repo / second["path"]).read_bytes() == text
    assert first["path"] == f".factory/local/briefs/{id}/code.md" and first["sha256"] == sha256(text)
    assert (
        first["diff_sha256"] == sha256(patch)
        and first["base_commit"] == load_mission(repo, id)["base_commit"]
    )
    decoded = patch.decode()
    assert "diff --git a/src/app.py b/src/app.py" in decoded and "+VALUE = 2" in decoded
    assert "diff --git a/src/new_module.py b/src/new_module.py" in decoded and "new file mode" in decoded
    assert ".factory/" not in decoded
    assert "Diff: .factory/local/briefs/M-REQ/diff.patch (sha256 " + first["diff_sha256"] in text.decode()
    acceptance = (repo / brief(repo, id, "acceptance")["path"]).read_text()
    assert REQUEST in acceptance and "## Exclusions" in acceptance and "Rubric" not in acceptance
    task = cli(repo, "mission", "brief", "--mission", id, "--task", "T-ONE")
    assert task["path"].endswith("/task-T-ONE.md") and task["diff_path"] is None
    body = (repo / task["path"]).read_text()
    assert "Constitution hash:" in body and "nested agents" in body and "AC-1" in body
    context = (repo / brief(repo, id, "context")["path"]).read_text()
    assert "Citation rules" in context and REQUEST.rstrip("\n") in context
    with pytest.raises(FactoryError, match="exactly one"):
        cli(repo, "mission", "brief", "--mission", id)
    with pytest.raises(FactoryError, match="Unknown task"):
        cli(repo, "mission", "brief", "--mission", id, "--task", "T-NONE")


def test_request_mission_merge_rechecks_review_kinds(tmp_path):
    from test_workflow import git

    from software_factory.workflow import assess_merged, record_ci, record_delivery

    root = make_repo(tmp_path / "delivery")
    git(root, "switch", "-qc", "feature")
    id = plan_mission(root, kind="feature")
    verified = implement(
        root, id, change=lambda: ((root / "src/app.py").write_text("VALUE = 2\n"), commit(root))
    )
    head = git(root, "rev-parse", "HEAD")
    review(root, id)
    review(root, id, "acceptance", brief_hash=brief(root, id, "acceptance")["sha256"])
    transition_mission(root, id, "READY_PR")
    record_ci(root, id, url="https://ci.example.invalid/1", head=head, conclusion="success", trunk="main")
    record_decision(
        root,
        id,
        {"id": "D-MERGE", "kind": "merge", "reference": "Merged", "subject_hash": verified["fingerprint"]},
    )
    record_delivery(root, id, {"merge_ref": head})
    git(root, "branch", "-f", "main", head)
    assert assess_merged(root, id)["pass"], assess_merged(root, id)
    review(root, id, "acceptance", status="changes_requested", verdict="fail")
    reasons = assess_merged(root, id)["reasons"]
    assert any("acceptance review" in r for r in reasons) and any(
        "verdict for AC-1 is fail" in r for r in reasons
    )


def test_exhausted_repair_budget_raises_risk(repo):
    id = plan_mission(repo)
    transition_mission(repo, id, "IMPLEMENTING")
    transition_task(repo, id, "T-ONE", "RUNNING")
    assert cli(repo, "mission", "risk", "--mission", id)["tier"] == "low"
    transition_task(repo, id, "T-ONE", "BLOCKED")
    transition_task(repo, id, "T-ONE", "RUNNING")
    risk = cli(repo, "mission", "risk", "--mission", id)
    assert risk == {
        **risk,
        "tier": "high",
        "reasons": ["Task T-ONE exhausted its repair budget"],
        "lane": "small",
    }


def test_hook_changes_are_governed_and_protected(repo):
    from software_factory.evidence import fingerprint

    id = plan_mission(repo, owned=("src/**", ".factory/hooks/**"))
    put(repo, ".factory/hooks/orchestrator_guard.py", "print('allow')\n")
    candidate = fingerprint(repo, load_mission(repo, id))
    assert ".factory/hooks/orchestrator_guard.py" in candidate["changed_paths"]
    reasons = assess_gate(repo, id)["reasons"]
    assert (
        "Protected factory path requires a maintenance mission: .factory/hooks/orchestrator_guard.py"
        in reasons
    )
    risk = cli(repo, "mission", "risk", "--mission", id)
    assert "Protected path changed: .factory/hooks/orchestrator_guard.py" in risk["reasons"]


def test_create_input_json_requires_request_file(repo, monkeypatch):
    from software_factory.workflow import REQUEST_REQUIRED, create_mission

    value = put(repo, ".factory/local/m.json", {"id": "M-JSON", "title": "No request", "kind": "patch"})
    with pytest.raises(FactoryError, match="requires --request-file PATH") as refused:
        cli(repo, "mission", "create", "--input", value)
    assert str(refused.value) == REQUEST_REQUIRED
    with pytest.raises(FactoryError, match="requires --request-file PATH"):
        cli(
            repo,
            "mission",
            "create",
            "--input",
            "-",
            stdin=b'{"id": "M-JSON", "title": "T"}',
            monkeypatch=monkeypatch,
        )
    with pytest.raises(FactoryError, match="requires --request-file PATH"):
        create_mission(repo, {"id": "M-JSON", "title": "T", "request_file": ""}, require_request=True)
    assert not (repo / ".factory/missions/M-JSON").exists()
    put(repo, ".factory/local/request.md", REQUEST)
    with_request = {
        "id": "M-JSON",
        "title": "T",
        "kind": "patch",
        "request_file": ".factory/local/request.md",
    }
    created = cli(repo, "mission", "create", "--input", put(repo, ".factory/local/m.json", with_request))
    assert created["request"]["sha256"] == sha256(REQUEST) and created["request"]["accepted_chain"] is None
    # --request-file also completes JSON input that lacks it.
    value = put(repo, ".factory/local/m2.json", {"id": "M-JSON2", "title": "T"})
    created = cli(repo, "mission", "create", "--input", value, "--request-file", ".factory/local/request.md")
    assert created["request"]["sha256"] == sha256(REQUEST)


def test_request_chain_is_bound_at_accept_scope(repo):
    id = plan_mission(repo)
    mission = load_mission(repo, id)
    assert mission["request"]["accepted_chain"] == mission["request"]["chain"] == sha256(REQUEST)
    implement(repo, id)
    review(repo, id)
    assert assess_gate(repo, id)["pass"]
    path = f".factory/missions/{id}/mission.json"
    record = read_json(repo, path)
    record["request"]["accepted_chain"] = sha256("an earlier request chain")
    write_json(repo, path, record)
    reasons = assess_gate(repo, id)["reasons"]
    assert any(r.startswith("Request or clarifications changed since scope acceptance") for r in reasons)
    record["request"]["accepted_chain"] = None
    write_json(repo, path, record)
    assert any("request chain not accepted" in r for r in assess_gate(repo, id)["reasons"])


def test_removed_request_record_blocks_gate_and_scope(repo):
    id = plan_mission(repo)
    implement(repo, id)
    review(repo, id)
    path = f".factory/missions/{id}/mission.json"
    record = read_json(repo, path)
    for key in ("request", "criteria", "criteria_hash"):
        record.pop(key)
    write_json(repo, path, record)
    gate = assess_gate(repo, id)
    assert not gate["pass"]
    assert any("(request record removed)" in r for r in gate["reasons"])
    with pytest.raises(FactoryError, match="request record removed"):
        cli(repo, "mission", "accept-scope", "--mission", id)


def test_criteria_change_after_planned_resets_like_clarify(repo):
    id = plan_mission(repo)
    transition_mission(repo, id, "IMPLEMENTING")
    transition_task(repo, id, "T-ONE", "RUNNING")
    transition_task(repo, id, "T-ONE", "BLOCKED")
    before = load_mission(repo, id)
    assert before["state"] == "IMPLEMENTING" and before["tasks"][0]["attempts"] == 1
    changed = {"items": [{**CRITERIA["items"][0], "text": "VALUE equals two"}]}
    mission = criteria(repo, id, changed)
    assert mission["state"] == "PROPOSED" and mission["criteria_hash"] is None
    assert mission["request"]["accepted_chain"] is None
    assert [(t["status"], t["attempts"]) for t in mission["tasks"]] == [("TODO", 1)]
    assert mission["criteria"]["items"][0]["text"] == "VALUE equals two"
    with pytest.raises(FactoryError, match="Invalid transition PROPOSED -> IMPLEMENTING"):
        transition_mission(repo, id, "IMPLEMENTING")
    assert cli(repo, "mission", "accept-scope", "--mission", id)["state"] == "PLANNED"
    # A premerge hold beyond PLANNED resets too and resolves the blocker.
    transition_mission(repo, id, "IMPLEMENTING")
    cli(repo, "mission", "block", "--mission", id, "--reason", "Waiting", "--next", "Ask the user")
    mission = criteria(repo, id, CRITERIA)
    assert mission["state"] == "PROPOSED" and mission["blockers"] == []


def test_risk_flags_removed_assertions_and_check_scripts(repo):
    (repo / "tests").mkdir()
    (repo / "tests/test_old.py").write_text("def test_old():\n    assert VALUE == 1\n")
    (repo / "scripts").mkdir()
    (repo / "scripts/check.py").write_text("print('check passed')\n")
    config = read_json(repo, "factory.json")
    config["checks"].append(
        {
            "id": "script",
            "command": [sys.executable, "scripts/check.py", "--config=scripts/check.toml"],
            "cwd": ".",
            "required": False,
            "timeout_seconds": 5,
        }
    )
    write_json(repo, "factory.json", config)
    (repo / "scripts/check.toml").write_text("strict = true\n")
    commit(repo, "tests and check script")
    id = plan_mission(repo)
    assert cli(repo, "mission", "risk", "--mission", id)["tier"] == "low"
    # Same line count, assertion replaced: not net removal, still flagged.
    (repo / "tests/test_old.py").write_text("def test_old():\n    print(VALUE)\n")
    risk = cli(repo, "mission", "risk", "--mission", id)
    assert risk["tier"] == "high" and "Test assertions removed: tests/test_old.py" in risk["reasons"]
    assert not any("net removed" in r for r in risk["reasons"])
    (repo / "tests/test_old.py").write_text(
        "def test_old():\n    assert VALUE == 1\n\n\ndef test_more():\n    pass\n"
    )
    assert cli(repo, "mission", "risk", "--mission", id)["tier"] == "low"
    (repo / "scripts/check.py").write_text("print('check passed')  # weakened\n")
    (repo / "scripts/check.toml").write_text("strict = false\n")
    risk = cli(repo, "mission", "risk", "--mission", id)
    assert risk["tier"] == "high"
    assert "Check command script changed: scripts/check.py" in risk["reasons"]
    assert "Check command script changed: scripts/check.toml" in risk["reasons"]


def test_removed_lines_parser_handles_quoted_and_deleted_paths():
    from software_factory.workflow import removed_lines

    patch = (
        b'diff --git "a/tests/t\\303\\251st.py" "b/tests/t\\303\\251st.py"\n'
        b'--- "a/tests/t\\303\\251st.py"\n+++ "b/tests/t\\303\\251st.py"\n@@ -1,2 +1,1 @@\n'
        b"-    assert x\n--- not a header\n+    pass\n"
        b"diff --git a/tests/gone.py b/tests/gone.py\ndeleted file mode 100644\n"
        b"--- a/tests/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-expect(value)\n"
    )
    assert removed_lines(patch) == {
        "tests/tést.py": ["    assert x", "-- not a header"],
        "tests/gone.py": ["expect(value)"],
    }


def test_check_route_needs_passed_check_evidence_and_safe_evidence_paths(repo):
    from software_factory.workflow import evidence_path_problem

    id = plan_mission(repo)
    (repo / "docs").mkdir()
    (repo / "docs/proof.txt").write_text("VALUE is 2\n")
    implement(repo, id, evidence={"AC-1": ["note:looked at it", "evidence:docs/proof.txt"]})
    review(repo, id)
    reasons = assess_gate(repo, id)["reasons"]
    assert any(
        r.startswith("Criterion AC-1 uses route check and needs check:<id> evidence naming a passed check")
        for r in reasons
    ), reasons
    for path in (
        ".git/config",
        "./.git/HEAD",
        ".factory/local/request.md",
        "src/../.factory/local/x",
        f".factory/missions/{id}/mission.json",
        f".factory/missions/{id}/request.md",
        f".factory/missions/{id}/results/index.json",
    ):
        assert evidence_path_problem(path), path
    assert evidence_path_problem(f".factory/missions/{id}/evidence/R-ONE/checks.json") is None
    assert evidence_path_problem("docs/proof.txt") is None
    verified = load_mission(repo, id)["evidence"][-1]
    result = {
        **result_for(repo, id, {"reference": verified}),
        "criteria_evidence": {"AC-1": ["evidence:.git/config"]},
    }
    with pytest.raises(FactoryError, match="cites evidence under .git/ or .factory/local/"):
        record_result(repo, id, result)
    result["criteria_evidence"] = {"AC-1": [f"evidence:.factory/missions/{id}/mission.json"]}
    with pytest.raises(FactoryError, match="cites a mission record as evidence"):
        record_result(repo, id, result)


def test_check_evidence_must_name_a_check_of_the_criterion(repo):
    config = read_json(repo, "factory.json")
    config["checks"].append(
        {
            "id": "other",
            "command": [sys.executable, "-c", "pass"],
            "cwd": ".",
            "required": False,
            "timeout_seconds": 5,
        }
    )
    write_json(repo, "factory.json", config)
    commit(repo, "other check")
    id = plan_mission(repo)
    implement(repo, id, evidence={"AC-1": ["check:other"]})
    review(repo, id)
    assert any("needs check:<id> evidence" in r for r in assess_gate(repo, id)["reasons"])


def test_missing_scope_decision_error_names_command_and_spec_hash(repo):
    create(repo)
    id = "M-REQ"
    directory = repo / ".factory/missions" / id
    for name, text in (("spec.md", SPEC), ("plan.md", PLAN), ("context.md", CONTEXT)):
        (directory / name).write_text(text)
    criteria(repo, id)
    spec_hash = hash_file(repo, f".factory/missions/{id}/spec.md")
    with pytest.raises(FactoryError) as refused:
        cli(repo, "mission", "accept-scope", "--mission", id)
    message = str(refused.value)
    assert f"(sha256 {spec_hash})" in message
    assert f"software-factory mission decision --mission {id} --input - <<'EOF'\n" in message
    payload = json.loads(message.split("<<'EOF'\n", 1)[1].split("\nEOF", 1)[0])
    assert payload["kind"] == "scope" and payload["subject_hash"] == spec_hash and payload["id"]
    payload["reference"] = "User accepted the specification in chat"
    cli(repo, "mission", "decision", "--mission", id, "--input", put(repo, ".factory/local/d.json", payload))
    assert cli(repo, "mission", "accept-scope", "--mission", id)["state"] == "PLANNED"


def test_result_error_names_evidence_path_and_brief_states_requirements(repo):
    id = plan_mission(repo)
    brief_text = (
        repo / cli(repo, "mission", "brief", "--mission", id, "--task", "T-ONE")["path"]
    ).read_text()
    assert f".factory/missions/{id}/evidence/<run>/checks.json" in brief_text
    assert f"record-doc --mission {id} --doc recovery" in brief_text
    assert "Before READY_PR, recovery.md must state recovery implications" in brief_text
    verified = implement(repo, id)
    path = f".factory/missions/{id}/results/index.json"
    index = read_json(repo, path)
    record_path = f".factory/missions/{id}/results/records/{index['records']['T-ONE']['file']}"
    stored = read_json(repo, record_path)
    stored["evidence"] = []
    write_json(repo, record_path, stored)
    index["records"]["T-ONE"]["sha256"] = hash_file(repo, record_path)
    write_json(repo, path, index)
    reasons = assess_gate(repo, id)["reasons"]
    expected = (
        "Result does not reference current verification evidence; "
        f"include {verified['reference']} in its evidence[]"
    )
    assert any(expected in r for r in reasons), reasons


def test_trailing_slash_owned_path_reaches_ready_gate(repo):
    id = plan_mission(repo, owned=("src/",))
    implement(repo, id)
    review(repo, id)
    gate = assess_gate(repo, id)
    assert gate["pass"], gate


def test_front_matter_and_comments_before_diagram_type_are_accepted(repo):
    create(repo)
    id = "M-REQ"
    plan = PLAN.replace(
        "```mermaid\nflowchart LR\n",
        "```mermaid\n%% architecture of the change\n---\ntitle: Change\n---\n%% diagram\nflowchart LR\n",
    )
    author(repo, id, plan=plan)
    criteria(repo, id)
    assert cli(repo, "mission", "accept-scope", "--mission", id)["state"] == "PLANNED"


def test_packet_lists_exclusions_with_decisions(repo):
    create(repo)
    id = "M-REQ"
    author(repo, id)
    record_decision(
        repo,
        id,
        {
            "id": "D-OUT",
            "kind": "exception",
            "reference": "User: importability is out of scope",
            "subject_hash": "b" * 64,
        },
    )
    value = {**CRITERIA, "exclusions": [{"excerpt": "Keep   the module importable.", "decision": "D-OUT"}]}
    criteria(repo, id, value)
    cli(repo, "mission", "accept-scope", "--mission", id)
    task = {
        "id": "T-ONE",
        "title": "Change app",
        "owned_paths": ["src/**"],
        "checks": ["unit"],
        "criteria": ["AC-1"],
    }
    cli(repo, "mission", "task-add", "--mission", id, "--input", put(repo, ".factory/local/t.json", task))
    implement(repo, id)
    review(repo, id)
    transition_mission(repo, id, "READY_PR")
    text = (repo / create_packet(repo, id)["path"]).read_text()
    section = text.split("## Request to evidence", 1)[1].split("\n## ", 1)[0]
    assert (
        "- Excluded: Keep the module importable. — decision D-OUT (exception: User: importability is out of scope)"
        in section
    )
