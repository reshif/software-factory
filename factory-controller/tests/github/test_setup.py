import json

import httpx
import pytest
import respx

from factory.github.setup import (apply_repo_settings, build_plan, main, recommended_ruleset,
                                  workflow_permissions_payload)


def test_recommended_ruleset_shape():
    ruleset = recommended_ruleset("acme/demo", merge_bot_app_id=999,
                                  required_checks=["lint", "unit"])
    assert ruleset["target"] == "branch"
    assert ruleset["enforcement"] == "active"
    assert ruleset["conditions"]["ref_name"]["include"] == ["refs/heads/main"]
    assert ruleset["bypass_actors"] == [
        {"actor_id": 999, "actor_type": "Integration", "bypass_mode": "always"}
    ]
    rule_types = {r["type"] for r in ruleset["rules"]}
    assert {"deletion", "non_fast_forward", "pull_request", "required_status_checks"} <= rule_types

    pr_rule = next(r for r in ruleset["rules"] if r["type"] == "pull_request")
    assert pr_rule["parameters"]["require_code_owner_review"] is True
    assert pr_rule["parameters"]["required_approving_review_count"] >= 1

    checks_rule = next(r for r in ruleset["rules"] if r["type"] == "required_status_checks")
    contexts = [c["context"] for c in checks_rule["parameters"]["required_status_checks"]]
    assert contexts == ["lint", "unit"]


def test_recommended_ruleset_without_codeowners():
    ruleset = recommended_ruleset("acme/demo", merge_bot_app_id=1, require_codeowners=False)
    pr_rule = next(r for r in ruleset["rules"] if r["type"] == "pull_request")
    assert pr_rule["parameters"]["require_code_owner_review"] is False


def test_workflow_permissions_disables_actions_pr_approval():
    payload = workflow_permissions_payload()
    assert payload["can_approve_pull_request_reviews"] is False
    assert payload["default_workflow_permissions"] == "read"


def test_build_plan_bundles_both():
    plan = build_plan("acme/demo", merge_bot_app_id=1)
    assert set(plan) == {"ruleset", "actions_permissions"}


def test_dry_run_returns_plan_and_prints_nothing_and_makes_no_request(capsys):
    # apply_repo_settings never prints (that's main()'s job) and, in a dry run,
    # never touches the network either -- no client/token is even required.
    plan = apply_repo_settings(repo="acme/demo", merge_bot_app_id=1, dry_run=True)
    assert plan == build_plan("acme/demo", merge_bot_app_id=1)
    assert capsys.readouterr().out == ""


def test_apply_requires_a_token_when_not_dry_run():
    with pytest.raises(ValueError):
        apply_repo_settings(repo="acme/demo", merge_bot_app_id=1, dry_run=False, token=None)


@respx.mock
def test_apply_posts_ruleset_and_puts_permissions():
    ruleset_route = respx.post("https://api.github.com/repos/acme/demo/rulesets").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )
    perms_route = respx.put("https://api.github.com/repos/acme/demo/actions/permissions/workflow").mock(
        return_value=httpx.Response(204)
    )
    plan = apply_repo_settings(repo="acme/demo", merge_bot_app_id=1, token="tok-abc",
                               client=httpx.Client())
    assert ruleset_route.called
    assert perms_route.called
    assert ruleset_route.calls.last.request.headers["Authorization"] == "Bearer tok-abc"
    assert json.loads(ruleset_route.calls.last.request.content) == plan["ruleset"]


def test_cli_dry_run_needs_no_credentials(capsys):
    rc = main(["acme/demo", "--merge-bot-app-id", "1", "--dry-run"])
    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["ruleset"]["bypass_actors"][0]["actor_id"] == 1


def test_cli_without_dry_run_requires_credentials():
    with pytest.raises(SystemExit):
        main(["acme/demo", "--merge-bot-app-id", "1"])
