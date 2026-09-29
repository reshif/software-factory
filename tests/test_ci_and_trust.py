"""F-9: CI results verified through gh (opt-in). F-10: requests carry a trust source."""

from __future__ import annotations

import json
import os
import stat

import pytest
from test_mission_030 import REQUEST, brief, cli, implement, plan_mission, put, repo, review  # noqa: F401
from test_workflow import commit, git, make_repo

from software_factory.civerify import verify_with_gh
from software_factory.core import FactoryError, read_json, write_json
from software_factory.workflow import load_mission, record_ci, transition_mission

RUN = "https://github.com/acme/app/actions/runs/42"


def fake_gh(tmp_path, monkeypatch, payload, code=0):
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "gh"
    script.write_text(f"#!/bin/sh\necho '{json.dumps(payload)}'\nexit {code}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def test_gh_confirms_a_successful_run_for_the_candidate(tmp_path, monkeypatch):
    head = "a" * 40
    fake_gh(
        tmp_path,
        monkeypatch,
        {"status": "completed", "conclusion": "success", "headSha": head, "workflowName": "ci"},
    )
    verified = verify_with_gh(tmp_path, RUN, head)
    assert verified["method"] == "gh" and verified["workflow"] == "ci"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"status": "in_progress", "conclusion": None, "headSha": "a" * 40}, "has not completed"),
        ({"status": "completed", "conclusion": "failure", "headSha": "a" * 40}, "concluded failure"),
        ({"status": "completed", "conclusion": "success", "headSha": "b" * 40}, "not the candidate"),
    ],
)
def test_gh_refuses_runs_that_do_not_prove_success(tmp_path, monkeypatch, payload, message):
    fake_gh(tmp_path, monkeypatch, payload)
    with pytest.raises(FactoryError, match=message):
        verify_with_gh(tmp_path, RUN, "a" * 40)


def test_gh_verification_needs_a_github_actions_url(tmp_path):
    with pytest.raises(FactoryError, match="must be a GitHub Actions run URL"):
        verify_with_gh(tmp_path, "https://ci.example.invalid/1", "a" * 40)


def ready_for_ci(tmp_path):
    root = make_repo(tmp_path / "delivery")
    config = read_json(root, "factory.json")
    config["ci"] = {"verify": "gh"}
    write_json(root, "factory.json", config)
    commit(root, "verify CI with gh")
    git(root, "switch", "-qc", "feature")
    id = plan_mission(root, kind="feature")
    implement(root, id, change=lambda: ((root / "src/app.py").write_text("VALUE = 2\n"), commit(root)))
    review(root, id)
    review(root, id, "acceptance", brief_hash=brief(root, id, "acceptance")["sha256"])
    transition_mission(root, id, "READY_PR")
    return root, id, git(root, "rev-parse", "HEAD")


def test_ci_result_is_verified_and_recorded_when_configured(tmp_path, monkeypatch):
    root, id, head = ready_for_ci(tmp_path)
    fake_gh(
        tmp_path,
        monkeypatch,
        {"status": "completed", "conclusion": "success", "headSha": head, "workflowName": "ci"},
    )
    record_ci(root, id, url=RUN, head=head, conclusion="success", trunk="main")
    ci = load_mission(root, id)["delivery"]["ci_ref"]
    assert ci["verified"]["method"] == "gh" and ci["verified"]["workflow"] == "ci"


def test_ci_result_is_refused_when_gh_disagrees(tmp_path, monkeypatch):
    root, id, head = ready_for_ci(tmp_path)
    fake_gh(tmp_path, monkeypatch, {"status": "completed", "conclusion": "failure", "headSha": head})
    with pytest.raises(FactoryError, match="concluded failure"):
        record_ci(root, id, url=RUN, head=head, conclusion="success", trunk="main")
    assert "ci_ref" not in load_mission(root, id)["delivery"]


# F-10: request trust


def create_from(root, source, kind="feature", mission_id="M-SRC"):
    request = put(root, ".factory/local/request.md", REQUEST)
    return cli(
        root, "mission", "create", "--id", mission_id, "--title", "Contributor ask", "--kind", kind,
        "--request-file", request, "--source", source,
    )  # fmt: skip


def test_untrusted_request_cannot_take_the_patch_lane(repo):  # noqa: F811
    with pytest.raises(FactoryError, match="untrusted input, so it takes the feature lane"):
        create_from(repo, "contributor", kind="patch")


def test_untrusted_request_is_framed_as_data_and_raises_risk(repo):  # noqa: F811
    create_from(repo, "contributor")
    assert load_mission(repo, "M-SRC")["request"]["source"] == "contributor"
    text = (repo / brief(repo, "M-SRC", "context")["path"]).read_text()
    assert "## Request (verbatim; UNTRUSTED input from a contributor" in text
    risk = cli(repo, "mission", "risk", "--mission", "M-SRC")
    assert "Request from an untrusted source (contributor)" in risk["reasons"] and risk["tier"] == "high"


def test_maintainer_request_is_unchanged(repo):  # noqa: F811
    create_from(repo, "maintainer", kind="patch", mission_id="M-OWN")
    text = (repo / brief(repo, "M-OWN", "context")["path"]).read_text()
    assert "## Request (verbatim)\n" in text
