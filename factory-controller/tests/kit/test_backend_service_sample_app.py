"""The backend-service template's sample app must pass its own tests standalone
using nothing but the stdlib -- no pytest, no PyYAML -- the way a fresh clone
of the golden path would run them (spec §3 B6). It must ALSO run correctly
under pytest with the exact command factory.yaml's checks.unit configures
(R-A7): pytest happily discovers and runs unittest.TestCase suites, so the
sample app's tests don't need rewriting, only invoking differently in CI.
"""
import subprocess
import sys
from xml.etree import ElementTree

import yaml


def test_sample_app_tests_pass_standalone_under_unittest(kit_dir):
    """No third-party dependency required at all -- the golden path works
    even before the sandbox's pytest is available."""
    backend_service = kit_dir / "templates" / "backend-service"
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
        cwd=backend_service, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert " OK" in result.stderr or result.stderr.strip().endswith("OK")


def _unit_check(kit_dir):
    profile = yaml.safe_load((kit_dir / "templates" / "backend-service" / "factory.yaml").read_text())
    return profile["checks"]["unit"]


def test_factory_yaml_unit_check_is_the_object_form_with_junit_and_min_tests(kit_dir):
    unit = _unit_check(kit_dir)
    assert isinstance(unit, dict)
    assert unit["command"].startswith("python -m pytest")
    assert "--junitxml=" + unit["junit"] in unit["command"]
    assert unit["min_tests"] >= 1


def test_sample_app_tests_pass_under_pytest_via_the_configured_command(kit_dir, tmp_path):
    """Run the EXACT command factory.yaml configures (checks.unit.command) and
    check the junit report it writes meets checks.unit.min_tests with zero
    failures/errors -- the same thing the controller's local check runner
    will verify (final draft §9.2, R-A7)."""
    backend_service = kit_dir / "templates" / "backend-service"
    unit = _unit_check(kit_dir)

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", f"--junitxml={unit['junit']}"],
        cwd=backend_service, capture_output=True, text=True, timeout=60,
    )
    junit_path = backend_service / unit["junit"]
    try:
        assert result.returncode == 0, result.stdout + result.stderr
        assert junit_path.is_file(), "checks.unit.junit report was not written"

        root = ElementTree.fromstring(junit_path.read_text())
        suite = root if root.tag == "testsuite" else root.find("testsuite")
        tests = int(suite.get("tests", 0))
        failures = int(suite.get("failures", 0))
        errors = int(suite.get("errors", 0))

        assert tests >= unit["min_tests"], f"only {tests} tests ran, need >= {unit['min_tests']}"
        assert failures == 0 and errors == 0
    finally:
        if junit_path.is_file():
            junit_path.unlink()
        factory_dir = backend_service / ".factory"
        if factory_dir.is_dir() and not any(factory_dir.iterdir()):
            factory_dir.rmdir()


def test_factory_output_dir_is_gitignored_in_the_template(kit_dir):
    backend_service = kit_dir / "templates" / "backend-service"
    gitignore = (backend_service / ".gitignore").read_text()
    assert ".factory/" in gitignore
