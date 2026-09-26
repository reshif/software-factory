#!/usr/bin/env python3
"""Black-box holdout runner (final draft §9.2, §13.1 #4).

Runs every scenarios/*.yaml against --staging-url and writes holdout-result.json
containing ONLY a pass/fail count -- never scenario names, request/response
bodies, or anything else that could tell an agent (or a human reading the
evidence bundle) *which* hidden scenario failed. That's deliberate: the
holdout repo and its scenarios are never visible to the agents whose work it
grades (§13.1 #4), and the count is all the evidence bundle's `holdout` field
carries (factory-kit/schemas/evidence-bundle.schema.json).

Stdlib only: no `requests`, no PyYAML. Scenario files are the small YAML
subset `miniyaml.parse` understands -- see that module's docstring.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from miniyaml import parse as parse_scenario


def load_scenarios(scenarios_dir: Path) -> list[dict]:
    return [parse_scenario(path.read_text()) for path in sorted(scenarios_dir.glob("*.yaml"))]


def run_scenario(base_url: str, scenario: dict, timeout: float) -> bool:
    request = scenario["request"]
    expect = scenario["expect"]
    url = base_url.rstrip("/") + request["path"]
    data = None
    headers = {}
    if "json" in request:
        data = json.dumps(request["json"]).encode()
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, method=request.get("method", "GET"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body = resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read().decode(errors="replace")
    except (urllib.error.URLError, OSError):
        return False

    if "status" in expect and status != int(expect["status"]):
        return False
    if "body_contains" in expect and str(expect["body_contains"]) not in body:
        return False
    return True


def run_all(staging_url: str, scenarios: list[dict], timeout: float) -> tuple[int, int]:
    total = len(scenarios)
    passed = sum(1 for scenario in scenarios if run_scenario(staging_url, scenario, timeout))
    return passed, total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging-url", required=True)
    parser.add_argument(
        "--scenarios-dir", type=Path, default=Path(__file__).resolve().parent.parent / "scenarios",
    )
    parser.add_argument("--out", type=Path, default=Path("holdout-result.json"))
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)

    scenarios = load_scenarios(args.scenarios_dir)
    if not scenarios:
        print("no scenarios found", file=sys.stderr)
        return 2

    passed, total = run_all(args.staging_url, scenarios, args.timeout)

    # Pass/fail counts ONLY -- see the module docstring.
    args.out.write_text(json.dumps({"passed": passed, "total": total}))
    print(f"{passed}/{total} scenarios passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
