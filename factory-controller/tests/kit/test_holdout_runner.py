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


def _load_sample_app(backend_service):
    """Import templates/backend-service/app/{__init__,store,server}.py directly
    from their file paths, under a private module name, without touching
    `sys.path` (a global, order-dependent mutation shared with every other
    test in the process). `server.py` does `from .store import ...`, so the
    three modules are registered under one dotted package name first and
    loaded in dependency order, exactly as Python's own import machinery
    would for a real package on the path -- just without putting it there.
    """
    package_name = "_kit_test_backend_service_app"
    app_dir = backend_service / "app"

    package_spec = importlib.util.spec_from_file_location(
        package_name, app_dir / "__init__.py", submodule_search_locations=[str(app_dir)],
    )
    package = importlib.util.module_from_spec(package_spec)
    sys.modules[package_name] = package
    package_spec.loader.exec_module(package)

    store_spec = importlib.util.spec_from_file_location(f"{package_name}.store", app_dir / "store.py")
    store = importlib.util.module_from_spec(store_spec)
    sys.modules[store_spec.name] = store
    store_spec.loader.exec_module(store)

    server_spec = importlib.util.spec_from_file_location(f"{package_name}.server", app_dir / "server.py")
    server = importlib.util.module_from_spec(server_spec)
    sys.modules[server_spec.name] = server
    server_spec.loader.exec_module(server)

    return package_name, server, store


@pytest.fixture
def sample_server(kit_dir):
    backend_service = kit_dir / "templates" / "backend-service"
    package_name, server, store = _load_sample_app(backend_service)
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(store.Store()))
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
        for name in (f"{package_name}.server", f"{package_name}.store", package_name):
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


@pytest.fixture(scope="module")
def miniyaml(kit_dir):
    holdouts = kit_dir / "templates" / "holdouts-repo"
    spec = importlib.util.spec_from_file_location("miniyaml", holdouts / "runner" / "miniyaml.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_miniyaml_parses_all_shipped_scenarios(kit_dir, miniyaml):
    holdouts = kit_dir / "templates" / "holdouts-repo"
    for path in sorted((holdouts / "scenarios").glob("*.yaml")):
        parsed = miniyaml.parse(path.read_text())
        assert "request" in parsed and "expect" in parsed, path.name
        assert "path" in parsed["request"], path.name
        assert "status" in parsed["expect"], path.name


def test_miniyaml_does_not_strip_a_hash_inside_a_quoted_value(miniyaml):
    """A prior bug did `line.split('#', 1)[0]` unconditionally, silently
    truncating any quoted value containing a literal `#` (low-priority fix
    item: `#` inside quotes must survive, a real trailing comment must not)."""
    text = (
        'name: quoting check\n'
        'request:\n'
        '  method: GET\n'
        '  path: /items\n'
        'expect:\n'
        '  status: 200\n'
        '  body_contains: "no #1 result"  # a real trailing comment\n'
    )
    parsed = miniyaml.parse(text)
    assert parsed["expect"]["body_contains"] == "no #1 result"
    assert parsed["expect"]["status"] == 200


def test_miniyaml_strips_a_real_comment_on_its_own_line(miniyaml):
    text = (
        "# a full-line comment\n"
        "name: x  # trailing comment\n"
        "request:\n"
        "  method: GET\n"
        "  path: /x\n"
        "expect:\n"
        "  status: 200\n"
    )
    parsed = miniyaml.parse(text)
    assert parsed["name"] == "x"
