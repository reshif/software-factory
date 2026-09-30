"""Optional verification of a recorded CI success against GitHub (``ci.verify: "gh"``).

By default ``mission ci-result`` records the URL and conclusion the user supplies, unchecked.
With ``"ci": {"verify": "gh"}`` in factory.json, a success is recorded only when the GitHub CLI
confirms that the GitHub Actions run at that URL completed successfully for exactly the
candidate commit; the record notes the verification, and MERGED requires it. It reads through
the user's own authenticated ``gh``; nothing is published.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

from .core import FactoryError, now

RUN_URL = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/actions/runs/(\d+)(?:[/?#].*)?$"
)


def verification_mode(config: dict) -> str:
    return (config.get("ci") or {}).get("verify", "none")


def verify_with_gh(root, url: str, head: str) -> dict:
    match = RUN_URL.match(url)
    if not match:
        raise FactoryError(
            "ci.verify is gh, so the CI URL must be a GitHub Actions run URL "
            "(https://github.com/OWNER/REPO/actions/runs/ID)"
        )
    owner, repo, run = match.groups()
    gh = shutil.which("gh")
    if not gh:
        raise FactoryError("ci.verify is gh but the GitHub CLI (gh) is not installed")
    try:
        result = subprocess.run(
            [
                gh,
                "run",
                "view",
                run,
                "--repo",
                f"{owner}/{repo}",
                "--json",
                "status,conclusion,headSha,workflowName",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise FactoryError(f"gh could not be run to verify CI: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()[:1]
        raise FactoryError("gh could not read the CI run" + (f": {detail[0]}" if detail else ""))
    try:
        run_info = json.loads(result.stdout)
    except ValueError as exc:
        raise FactoryError("gh returned unreadable run details") from exc
    if run_info.get("status") != "completed":
        raise FactoryError(f"CI run {run} has not completed (status {run_info.get('status')})")
    if run_info.get("conclusion") != "success":
        raise FactoryError(f"CI run {run} concluded {run_info.get('conclusion')}, not success")
    if run_info.get("headSha") != head:
        raise FactoryError(f"CI run {run} tested {run_info.get('headSha')}, not the candidate {head}")
    return {"method": "gh", "workflow": run_info.get("workflowName"), "checked_at": now()}
