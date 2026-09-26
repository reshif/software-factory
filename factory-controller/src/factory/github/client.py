"""Real GitHub adapter: two App identities (push bot, merge bot) over REST + local git.

Final draft §11, §13.1 #5 and #9, §13.2. Push and merge use separate GitHub App
identities so no single credential can both push code and merge it. `push_diff`
classifies the diff against the security floor and refuses AC8 before doing
anything else (defense in depth: the coordinator should never have offered an
AC8 diff, but this is the last gate before bytes land in the remote).

The installation token is only ever held in memory and passed to git via an
ephemeral `-c http.extraHeader=...` config override, so it never touches the
on-disk git config or a saved remote URL, and it is scrubbed from any error
message this module raises.
"""
import base64
import logging
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote

import httpx

from ..models import CheckResult, Diff, WorkItem
from ..policy import Floor
from ..policy.action_classes import classify
from .app_auth import AppCredentials, InstallationTokenProvider

logger = logging.getLogger(__name__)

GIT_TIMEOUT_S = 300

# GitHub check-run conclusions this module recognizes 1:1. Anything else that is
# still "completed" (action_required, stale, ...) fails closed as "failure";
# anything not completed (queued, in_progress) or with a null conclusion is "missing".
_CONCLUSION_MAP = {
    "success": "success",
    "failure": "failure",
    "neutral": "neutral",
    "cancelled": "cancelled",
    "skipped": "skipped",
    "timed_out": "timed_out",
}


class ForbiddenDiff(RuntimeError):
    """Raised when push_diff is asked to push an AC8 (forbidden) diff. It refuses."""


class MergeConflict(RuntimeError):
    """Raised when the PR's actual head sha doesn't match the expected one."""


class GitCommandError(RuntimeError):
    """A local git subprocess failed. The message never contains a token."""


def _redact(text: str, token: str | None) -> str:
    if token:
        text = text.replace(token, "***")
    return text


def _run_git(args: list[str], *, cwd: Path, token: str | None = None,
             config: dict[str, str] | None = None) -> str:
    cmd = ["git"]
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        cmd += ["-c", f"http.extraHeader=AUTHORIZATION: basic {basic}"]
    for key, value in (config or {}).items():
        cmd += ["-c", f"{key}={value}"]
    cmd += args
    try:
        result = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise GitCommandError(f"git {' '.join(args)} timed out after {GIT_TIMEOUT_S}s") from None
    if result.returncode != 0:
        raise GitCommandError(f"git {' '.join(args)} failed: {_redact(result.stderr, token).strip()}")
    return result.stdout


def default_remote_url(repo: str, *, host: str = "github.com") -> str:
    return f"https://{host}/{repo}.git"


class RestGitHub:
    """`GitHubPort` over the REST API, with a real git checkout for `push_diff`."""

    def __init__(self, *, push_credentials: AppCredentials, merge_credentials: AppCredentials,
                 floor: Floor, api_url: str = "https://api.github.com",
                 repos_root: str = "/tmp/factory-repos",
                 http_client: httpx.Client | None = None,
                 remote_url_builder: Callable[[str], str] | None = None,
                 push_bot_identity: tuple[str, str] | None = None,
                 token_provider_factory=InstallationTokenProvider):
        self._api_url = api_url.rstrip("/")
        self._floor = floor
        self._repos_root = Path(repos_root)
        self._rest = http_client or httpx.Client(timeout=30.0)
        self._push_tokens = token_provider_factory(push_credentials, client=self._rest)
        self._merge_tokens = token_provider_factory(merge_credentials, client=self._rest)
        self._remote_url_builder = remote_url_builder or default_remote_url
        self._author = push_bot_identity or (
            "factory-push-bot[bot]",
            f"{push_credentials.app_id}+factory-push-bot[bot]@users.noreply.github.com",
        )

    def close(self) -> None:
        self._rest.close()

    # ── REST helpers ──────────────────────────────────────────────────────────
    def _headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _push_headers(self) -> dict:
        return self._headers(self._push_tokens.token())

    def _merge_headers(self) -> dict:
        return self._headers(self._merge_tokens.token())

    def _url(self, path: str) -> str:
        return f"{self._api_url}{path}"

    # ── GitHubPort: read-only, push-bot identity ────────────────────────────
    def get_issue(self, repo: str, number: int) -> WorkItem:
        resp = self._rest.get(self._url(f"/repos/{repo}/issues/{number}"), headers=self._push_headers())
        resp.raise_for_status()
        data = resp.json()
        labels = tuple(
            label["name"] if isinstance(label, dict) else label for label in data.get("labels", [])
        )
        return WorkItem(
            item_id=f"{repo}#{number}",
            product=repo.rsplit("/", 1)[-1],
            repo=repo,
            number=number,
            title=data.get("title", ""),
            body=data.get("body") or "",
            labels=labels,
            author=data["user"]["login"] if data.get("user") else None,
            url=data.get("html_url"),
        )

    def comment_issue(self, repo: str, number: int, body: str) -> None:
        resp = self._rest.post(self._url(f"/repos/{repo}/issues/{number}/comments"),
                                json={"body": body}, headers=self._push_headers())
        resp.raise_for_status()

    def mirror_labels(self, repo: str, number: int, *, add: Iterable[str] = (),
                       remove: Iterable[str] = ()) -> None:
        for label in remove:
            resp = self._rest.delete(
                self._url(f"/repos/{repo}/issues/{number}/labels/{quote(label, safe='')}"),
                headers=self._push_headers(),
            )
            if resp.status_code not in (200, 404):
                resp.raise_for_status()
        add = list(add)
        if add:
            resp = self._rest.post(self._url(f"/repos/{repo}/issues/{number}/labels"),
                                    json={"labels": add}, headers=self._push_headers())
            resp.raise_for_status()

    def head_commit(self, repo: str, branch: str = "main") -> str:
        resp = self._rest.get(self._url(f"/repos/{repo}/commits/{branch}"), headers=self._push_headers())
        resp.raise_for_status()
        return resp.json()["sha"]

    def open_pr(self, repo: str, *, head: str, base: str, title: str, body: str) -> int:
        resp = self._rest.post(self._url(f"/repos/{repo}/pulls"),
                                json={"head": head, "base": base, "title": title, "body": body},
                                headers=self._push_headers())
        resp.raise_for_status()
        return resp.json()["number"]

    def pr_head_sha(self, repo: str, number: int) -> str:
        resp = self._rest.get(self._url(f"/repos/{repo}/pulls/{number}"), headers=self._push_headers())
        resp.raise_for_status()
        return resp.json()["head"]["sha"]

    def check_runs(self, repo: str, sha: str) -> list[CheckResult]:
        resp = self._rest.get(self._url(f"/repos/{repo}/commits/{sha}/check-runs"),
                               headers=self._push_headers())
        resp.raise_for_status()
        results = []
        for run in resp.json().get("check_runs", []):
            if run.get("status") != "completed" or run.get("conclusion") is None:
                conclusion = "missing"
            else:
                conclusion = _CONCLUSION_MAP.get(run["conclusion"], "failure")
            detail = (run.get("output") or {}).get("title") or ""
            results.append(CheckResult(name=run["name"], conclusion=conclusion, detail=detail,
                                        url=run.get("html_url")))
        return results

    def pr_reviews(self, repo: str, number: int) -> list[dict]:
        resp = self._rest.get(self._url(f"/repos/{repo}/pulls/{number}/reviews"),
                               headers=self._push_headers())
        resp.raise_for_status()
        return [{"login": r["user"]["login"], "state": r["state"], "commit_id": r["commit_id"]}
                for r in resp.json()]

    # ── GitHubPort: writes, push-bot identity ───────────────────────────────
    def push_diff(self, repo: str, *, branch: str, diff: Diff, message: str) -> str:
        classification = classify(diff.changes, self._floor)
        if classification.blocked:
            raise ForbiddenDiff(
                f"refusing to push AC8 diff to {repo}: {'; '.join(classification.reasons)}"
            )
        token = self._push_tokens.token()
        remote_url = self._remote_url_builder(repo)
        workdir = self._repos_root / f"push-{uuid.uuid4().hex}"
        workdir.parent.mkdir(parents=True, exist_ok=True)
        try:
            _run_git(["clone", "--no-tags", "--quiet", remote_url, str(workdir)],
                      cwd=workdir.parent, token=token)
            _run_git(["checkout", "--quiet", diff.base_commit], cwd=workdir)
            _run_git(["checkout", "--quiet", "-B", branch], cwd=workdir)
            patch_path = workdir / ".factory-push.patch"
            patch_path.write_text(diff.patch)
            try:
                _run_git(["apply", "--index", patch_path.name], cwd=workdir)
            finally:
                patch_path.unlink(missing_ok=True)
            _run_git(["commit", "--quiet", "-m", message], cwd=workdir,
                      config={"user.name": self._author[0], "user.email": self._author[1]})
            sha = _run_git(["rev-parse", "HEAD"], cwd=workdir).strip()
            _run_git(["push", "--quiet", remote_url, f"HEAD:refs/heads/{branch}"],
                      cwd=workdir, token=token)
            return sha
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def revert_commit(self, repo: str, sha: str, *, branch: str = "main") -> str:
        token = self._merge_tokens.token()
        remote_url = self._remote_url_builder(repo)
        workdir = self._repos_root / f"revert-{uuid.uuid4().hex}"
        workdir.parent.mkdir(parents=True, exist_ok=True)
        try:
            _run_git(["clone", "--no-tags", "--quiet", remote_url, str(workdir)],
                      cwd=workdir.parent, token=token)
            _run_git(["checkout", "--quiet", branch], cwd=workdir)
            _run_git(["revert", "--no-edit", sha], cwd=workdir,
                      config={"user.name": self._author[0], "user.email": self._author[1]})
            revert_sha = _run_git(["rev-parse", "HEAD"], cwd=workdir).strip()
            _run_git(["push", "--quiet", remote_url, f"HEAD:refs/heads/{branch}"],
                      cwd=workdir, token=token)
            return revert_sha
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # ── GitHubPort: merge, merge-bot identity ───────────────────────────────
    def merge_pr(self, repo: str, number: int, *, expected_head_sha: str) -> str:
        resp = self._rest.put(
            self._url(f"/repos/{repo}/pulls/{number}/merge"),
            json={"sha": expected_head_sha, "merge_method": "squash"},
            headers=self._merge_headers(),
        )
        if resp.status_code in (405, 409):
            raise MergeConflict(
                f"merge of {repo}#{number} rejected (expected head {expected_head_sha}): "
                f"{resp.status_code} {resp.text}"
            )
        resp.raise_for_status()
        return resp.json()["sha"]
