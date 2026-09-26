import pytest

from factory.agent_output import AgentOutputError, parse_agent_output, parse_last_json_block, render_json_block


def test_last_block_wins():
    text = render_json_block({"a": 1}) + "\nmore\n" + render_json_block({"a": 2})
    assert parse_last_json_block(text) == {"a": 2}


@pytest.mark.parametrize("text", ["", "no block", "```json\nnot json\n```", "```json\n[1, 2]\n```"])
def test_unparseable_is_none(text):
    assert parse_last_json_block(text) is None


def test_valid_reviewer_output():
    out = parse_agent_output("reviewer", render_json_block({"verdict": "fail", "findings": [
        {"severity": "blocking", "path": "a.py", "line": 1, "message": "bug"}]}))
    assert out["verdict"] == "fail"


@pytest.mark.parametrize("role,text", [
    ("reviewer", "nothing here"),
    ("reviewer", render_json_block({"verdict": "maybe", "findings": []})),
    ("intake", render_json_block({"lane": "patch"})),
    ("unknown-role", render_json_block({})),
])
def test_invalid_output_fails_closed(role, text):
    with pytest.raises(AgentOutputError):
        parse_agent_output(role, text)
