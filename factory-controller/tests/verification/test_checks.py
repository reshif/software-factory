from factory.models import CheckResult
from factory.verification.checks import evaluate


def test_all_success_passes():
    results = {"lint": CheckResult("lint", "success"), "tests": CheckResult("tests", "success")}
    verdict = evaluate(["lint", "tests"], results)
    assert verdict.ok
    assert verdict.failing == ()
    assert verdict.missing == ()


def test_missing_check_fails_closed():
    results = {"lint": CheckResult("lint", "success")}
    verdict = evaluate(["lint", "tests"], results)
    assert not verdict.ok
    assert verdict.missing == ("tests",)
    assert verdict.failing == ()


def test_non_success_conclusion_fails():
    for conclusion in ("failure", "skipped", "neutral", "cancelled", "timed_out", "missing"):
        results = {"tests": CheckResult("tests", conclusion)}
        verdict = evaluate(["tests"], results)
        assert not verdict.ok, conclusion
        assert verdict.failing == ("tests",), conclusion
        assert verdict.missing == ()


def test_extra_unrequired_checks_are_ignored():
    results = {"lint": CheckResult("lint", "success"), "extra": CheckResult("extra", "failure")}
    verdict = evaluate(["lint"], results)
    assert verdict.ok


def test_empty_required_list_is_trivially_ok():
    verdict = evaluate([], {})
    assert verdict.ok
    assert verdict.failing == () and verdict.missing == ()
