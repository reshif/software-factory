"""software-factory serve: a read-only local Mission Deck with a session token."""

from __future__ import annotations

import http.client
import json
import threading

import pytest
from test_mission_030 import plan_mission, repo  # noqa: F401  (fixture)

from software_factory.serve import DeckServer
from software_factory.workflow import load_mission


@pytest.fixture
def deck(repo):  # noqa: F811
    plan_mission(repo)
    server = DeckServer(repo, 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    yield server
    server.stopping.set()
    server.shutdown()
    server.server_close()


def call(server, path, *, method="GET", token=True, host=None):
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if token:
        headers["X-Factory-Token"] = server.token
    conn.request(method, path, headers=headers)
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response.status, body


def test_page_and_mission_api(deck):
    status, page = call(deck, "/", token=False)
    assert status == 200 and b"<title>Mission Deck</title>" in page
    status, body = call(deck, "/api/missions")
    listed = json.loads(body)
    assert status == 200 and [m["id"] for m in listed["missions"]] == ["M-REQ"]
    assert listed["missions"][0]["task_count"] == 1
    status, body = call(deck, "/api/mission/M-REQ")
    detail = json.loads(body)
    assert status == 200 and detail["mission"]["id"] == "M-REQ"
    assert detail["lanes"]["waves"] == [["T-ONE"]]
    assert detail["events"]["present"] and detail["events"]["problems"] == []
    assert detail["docs"]["assessment.md"].startswith("# Assessment")
    assert "Please change VALUE" in detail["docs"]["request.md"]


def test_missions_say_what_they_need_from_the_user(deck, repo):  # noqa: F811
    from software_factory.workflow import transition_mission

    listed = json.loads(call(deck, "/api/missions")[1])
    assert listed["project"] == repo.name and listed["halted"] is None
    transition_mission(repo, "M-REQ", "BLOCKED", reason="Which threshold?", next="Ask the user")
    mission = json.loads(call(deck, "/api/missions")[1])["missions"][0]
    assert {"kind": "blocked", "text": "Which threshold?"} in mission["attention"]


def test_gate_runs_on_request(deck):
    status, body = call(deck, "/api/mission/M-REQ/gate")
    gate = json.loads(body)
    assert status == 200 and gate["pass"] is False and gate["reasons"]


def test_api_needs_the_session_token(deck):
    status, body = call(deck, "/api/missions", token=False)
    assert status == 401 and b"session token" in body
    status, _ = call(deck, f"/api/missions?token={deck.token}", token=False)
    assert status == 200


def test_foreign_host_is_refused(deck):
    status, _ = call(deck, "/api/missions", host="evil.example:80")
    assert status == 421


def test_the_deck_is_read_only(deck):
    for method in ("POST", "PUT", "DELETE"):
        status, body = call(deck, "/api/missions", method=method)
        assert status == 405 and b"read-only" in body


def test_unknown_mission_is_a_clean_error(deck):
    status, body = call(deck, "/api/mission/M-NOPE")
    assert status == 400 and b"Unknown mission" in body


def test_attention_follows_what_the_user_can_do_next(repo):  # noqa: F811
    from test_mission_030 import ASSESSMENT, create

    from software_factory.serve import _attention

    create(repo, id="M-NEW")
    mission = load_mission(repo, "M-NEW")
    assert _attention(repo, mission, {}) == [
        {"kind": "approve", "text": "Waiting for the interview and assessment"}
    ]
    (repo / ".factory/missions/M-NEW/assessment.md").write_text(ASSESSMENT)  # recorded, not yet approved
    (need,) = _attention(repo, load_mission(repo, "M-NEW"), {})
    assert need["text"] == "Review the assessment and spec, then reply `approve M-NEW scope`"
    assert _attention(repo, mission, {"stale": True, "idle_hours": 30})[-1] == {
        "kind": "stale",
        "text": "No change for 30 h",
    }
    ready = {**mission, "state": "READY_PR"}
    assert _attention(repo, ready, {})[0]["text"] == "Open the pull request and record its CI result"
    ready["delivery"] = {"ci_ref": {"url": "x"}}
    assert "approve M-NEW merge" in _attention(repo, ready, {})[0]["text"]
    ready["decisions"] = [{"kind": "merge"}]
    assert _attention(repo, ready, {})[0]["text"].startswith("Merge approved")
