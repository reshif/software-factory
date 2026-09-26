"""The backend-service template's sample app must pass its own tests standalone
(spec §3 B6: "make sure the template's sample-app tests pass on their own"),
using nothing but the stdlib -- no pytest, no PyYAML -- the way a fresh clone
of the golden path would run them."""
import subprocess
import sys


def test_sample_app_tests_pass_standalone(kit_dir):
    backend_service = kit_dir / "templates" / "backend-service"
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
        cwd=backend_service, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert " OK" in result.stderr or result.stderr.strip().endswith("OK")
