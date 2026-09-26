import pytest

from factory.policy.action_classes import FileChange, classify


def fc(path, status="modified", added=5, removed=0, text=""):
    return FileChange(path, status, added, removed, text)


@pytest.mark.parametrize("changes,expected", [
    ([fc("README.md")], "AC1"),
    ([fc("docs/guide.md"), fc("tests/test_new.py", "added", 40)], "AC1"),
    ([fc("pyproject.toml"), fc("uv.lock", added=80, removed=70)], "AC2"),
    ([fc("src/app/service.py", added=30, removed=10)], "AC3"),
    ([fc("src/app/service.py", added=200, removed=10)], "AC4"),
    ([fc("migrations/0042_add_col.sql", "added", 12)], "AC5"),
    ([fc("migrations/0001_init.sql", "modified", 3)], "AC6"),
    ([fc("src/auth/session.py")], "AC6"),
    ([fc("tests/test_existing.py", "modified")], "AC6"),
    ([fc("tests/test_existing.py", "deleted", 0, 40)], "AC6"),
    ([fc("tests/conftest.py", "modified")], "AC6"),
    ([fc(".github/workflows/ci.yml")], "AC8"),
    ([fc("policies/gate-table.yaml")], "AC8"),
    ([fc("config/.env")], "AC8"),
])
def test_classify(policy, changes, expected):
    assert classify(changes, policy.floor).action_class == expected


def test_weakening_patterns_are_forbidden(policy):
    change = fc("tests/test_new_feature.py", "added", 10, text="@pytest.mark.skip(reason='flaky')")
    result = classify([change], policy.floor)
    assert result.action_class == "AC8"
    assert result.forbidden_touched == ("tests/test_new_feature.py",)


def test_declared_class_can_only_make_it_stricter(policy):
    assert classify([fc("README.md")], policy.floor, declared="AC7").action_class == "AC7"
    assert classify([fc("src/auth/x.py")], policy.floor, declared="AC3").action_class == "AC6"


def test_product_protected_paths_apply(policy):
    result = classify([fc("src/billing/ledger.py")], policy.floor, product_protected=["src/billing/**"])
    assert result.action_class == "AC6"
    assert result.protected_touched == ("src/billing/ledger.py",)


def test_mixed_docs_and_code_is_not_ac1(policy):
    assert classify([fc("README.md"), fc("src/x.py")], policy.floor).action_class == "AC3"


def test_empty_diff_rejected(policy):
    with pytest.raises(ValueError):
        classify([], policy.floor)
