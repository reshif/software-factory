"""templates/holdouts-repo/runner/run_blackbox.py against a local stdlib HTTP
server (spec §3 B6: "the holdout runner against a local stdlib HTTP server").

Uses the golden-path sample app itself (templates/backend-service/app) as the
server under test, and the shipped scenarios/*.yaml as the scenario set --
exercising the whole golden path end to end, not just the runner in isolation.
"""
import importlib.util
import json
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture
def sample_server(kit_dir):
    backend_service = kit_dir / "templates" / "backend-service"
    sys.path.insert(0, str(backend_service))
    try:
        from app.server import make_handler
        from app.store import Store

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Store()))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        port = httpd.server_address[1]
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
    finally:
        sys.path.remove(str(backend_service))
        for name in ("app.server", "app.store", "app"):
            sys.modules.pop(name, None)


def test_runner_passes_all_scenarios_against_the_sample_app(kit_dir, sample_server, tmp_path):
    holdouts = kit_dir / "templates" / "holdouts-repo"
    out = tmp_path / "holdout-result.json"
    result = subprocess.run(
        [
            sys.executable, str(holdouts / "runner" / "run_blackbox.py"),
            "--staging-url", sample_server,
            "--scenarios-dir", str(holdouts / "scenarios"),
            "--out", str(out),
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr

    payload = json.loads(out.read_text())
    assert set(payload.keys()) == {"passed", "total"}, "holdout-result.json must carry ONLY pass/fail counts"
    assert payload["total"] == 3
    assert payload["passed"] == 3


def test_runner_reports_failures_without_leaking_which_scenario(kit_dir, sample_server, tmp_path):
    """Stop the server so every scenario fails, and confirm the result is still
    just counts -- never scenario names or bodies (final draft §13.1 #4)."""
    holdouts = kit_dir / "templates" / "holdouts-repo"
    out = tmp_path / "holdout-result.json"
    dead_url = "http://127.0.0.1:1"  # nothing listens here
    result = subprocess.run(
        [
            sys.executable, str(holdouts / "runner" / "run_blackbox.py"),
            "--staging-url", dead_url,
            "--scenarios-dir", str(holdouts / "scenarios"),
            "--out", str(out),
            "--timeout", "1",
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1
    payload = json.loads(out.read_text())
    assert payload == {"passed": 0, "total": 3}


def test_miniyaml_parses_all_shipped_scenarios(kit_dir):
    holdouts = kit_dir / "templates" / "holdouts-repo"
    spec = importlib.util.spec_from_file_location("miniyaml", holdouts / "runner" / "miniyaml.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for path in sorted((holdouts / "scenarios").glob("*.yaml")):
        parsed = module.parse(path.read_text())
        assert "request" in parsed and "expect" in parsed, path.name
        assert "path" in parsed["request"], path.name
        assert "status" in parsed["expect"], path.name
