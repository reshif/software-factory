from datetime import date
from pathlib import Path

import yaml

from factory.controller.coverage import StandingMandate, check_diff, check_request
from factory.policy.action_classes import FileChange, classify
from factory.policy.loader import default_kit_dir

EXAMPLES = default_kit_dir() / "examples"
TODAY = date(2026, 10, 1)


def mandate():
    product = yaml.safe_load((EXAMPLES / "factory.yaml").read_text())
    doc = yaml.safe_load((EXAMPLES / "standing-mandate.yaml").read_text())
    return StandingMandate.from_dict(doc, product_protected=product["protected_paths"],
                                     product_forbidden=product["forbidden_paths"])


def test_request_needs_label_and_class():
    m = mandate()
    assert check_request(m, labels=["factory:patch"], suggested_class="AC3", today=TODAY).covered
    assert not check_request(m, labels=["bug"], suggested_class="AC3", today=TODAY).covered
    assert not check_request(m, labels=["factory:patch"], suggested_class="AC4", today=TODAY).covered


def test_expired_mandate_covers_nothing():
    result = check_request(mandate(), labels=["factory:patch"], suggested_class="AC1", today=date(2027, 2, 1))
    assert not result.covered and "expired" in result.reasons[0]


def test_small_diff_is_covered(policy):
    changes = [FileChange("src/app/util.py", "modified", 20, 5)]
    result = check_diff(mandate(), classification=classify(changes, policy.floor), changes=changes, today=TODAY)
    assert result.covered, result.reasons


def test_diff_too_large_is_not_covered(policy):
    changes = [FileChange("src/app/util.py", "modified", 140, 60)]
    result = check_diff(mandate(), classification=classify(changes, policy.floor), changes=changes, today=TODAY)
    assert not result.covered
    assert any("lines" in r for r in result.reasons)


def test_protected_path_is_not_covered(policy):
    changes = [FileChange("src/auth/token.py", "modified", 3, 1)]
    classification = classify(changes, policy.floor)
    result = check_diff(mandate(), classification=classification, changes=changes, today=TODAY)
    assert not result.covered
    assert any("AC6" in r for r in result.reasons)


def test_path_outside_mandate_is_not_covered(policy):
    changes = [FileChange("scripts/deploy.sh", "modified", 3, 1)]
    result = check_diff(mandate(), classification=classify(changes, policy.floor), changes=changes, today=TODAY)
    assert not result.covered
    assert any("outside mandate" in r for r in result.reasons)
