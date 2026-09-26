"""Real GitHub adapter: two App identities (push bot, merge bot) over REST + local git.

Final draft §11, §13.1 #5 and #9, §13.2. Push and merge use separate GitHub App
identities so no single credential can both push code and merge it. `push_diff`
classifies the diff against the security floor and refuses AC8 before doing
anything else (defense in depth: the coordinator should never have offered an
AC8 diff, but this is the last gate before bytes land in the remote) — and
does it again after `git apply`, against the *actual* staged paths, because
the diff a caller hands in can lie (e.g. a non-ASCII path that got C-quoted
somewhere upstream and no longer matches a forbidden-path glob).

The installation token is only ever held in memory and passed to git through
`GIT_CONFIG_*` environment variables (never on the command line, where it
would show up in `/proc/<pid>/cmdline` or `ps`), so it never touches argv,
the on-disk git config, a saved remote URL, or an error message this module
raises.
"""
import base64
import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote

import httpx

from ..models import CheckResult, Diff, WorkItem
from ..policy import Floor
from ..policy.action_classes import FileChange, classify
from ..ports import NotFound
from ._http import github_headers, next_link_url
from .app_auth import AppCredentials, InstallationTokenProvider

logger = logging.getLogger(__name__)

GIT_TIMEOUT_S = 300
PAGE_SIZE = 100

# Config forced on every git subprocess this module runs, regardless of token:
# no hooks, no fsmonitor, and no global/user gitconfig leaking in from the host.
_HARDENING_CONFIG = {
    "core.hooksPath": "/dev/null",
    "core.fsmonitor": "false",
}

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

_STATUS_LETTER_MAP = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "C": "renamed"}


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


def _git_env(*, token: str | None, config: dict[str, str] | None) -> dict:
    """Builds the environment for a git subprocess, config included.

    Config (including the bearer token, base64-encoded into a Basic auth
    header) travels entirely through `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/
    `GIT_CONFIG_VALUE_n`, never as a `-c key=value` argv element, so nothing
    secret is ever visible in the process's command line.
    """
    settings = dict(_HARDENING_CONFIG)
    settings.update(config or {})
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        settings["http.extraHeader"] = f"AUTHORIZATION: basic {basic}"
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_COUNT"] = str(len(settings))
    for i, (key, value) in enumerate(settings.items()):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


def _run_git(args: list[str], *, cwd: Path, token: str | None = None,
             config: dict[str, str] | None = None) -> str:
    cmd = ["git", *args]
    env = _git_env(token=token, config=config)
    try:
        result = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                                timeout=GIT_TIMEOUT_S, env=env, encoding="utf-8")
    except subprocess.TimeoutExpired:
        raise GitCommandError(f"git {' '.join(args)} timed out after {GIT_TIMEOUT_S}s") from None
    if result.returncode != 0:
        raise GitCommandError(f"git {' '.join(args)} failed: {_redact(result.stderr, token).strip()}")
    return result.stdout


def _parse_name_status_z(output: str) -> list[tuple[str, str]]:
    """Parses `git diff --name-status -z` output into (status, path) pairs.

    Renames/copies carry an old and a new path; we only need the new one.
    """
    parts = output.split("\x00")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    pairs: list[tuple[str, str]] = []
    i = 0
    while i < len(parts):
        status = parts[i]
        if status[:1] in ("R", "C"):
            new_path = parts[i + 2]
            pairs.append((status, new_path))
            i += 3
        else:
            pairs.append((status, parts[i + 1]))
            i += 2
    return pairs


def _real_staged_changes(workdir: Path, base_commit: str) -> list[FileChange]:
    """Re-derives the actually-staged paths from git itself, unquoted (§13.1 #3).

    This is the R-A2 backstop: `diff.changes` (built upstream, outside this
    module) is never trusted on its own for the AC8 decision. `core.quotePath
    =false` stops git from C-quoting non-ASCII bytes into an escaped string
    that wouldn't match a forbidden-path glob.
    """
    output = _run_git(["diff", "--cached", "--name-status", "-z", base_commit],
                      cwd=workdir, config={"core.quotePath": "false"})
    return [FileChange(path=path, status=_STATUS_LETTER_MAP.get(status[:1], "modified"))
            for status, path in _parse_name_status_z(output)]


def default_remote_url(repo: str, *, host: str = "github.com") -> str:
    return f"https://{host}/{repo}.git"


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code == 404:
        raise NotFound(str(resp.request.url))
    resp.raise_for_status()


class RestGitHub:
    """`GitHubPort` over the REST API, with a real git checkout for `push_diff`."""

    def __init__(self, *, push_credentials: AppCredentials, merge_credentials: AppCredentials,
                 floor: Floor, api_url: str = "https://api.github.com",
                 repos_root: str = "/tmp/factory-repos",
                 http_client: httpx.Client | None = None,
                 remote_url_builder: Callable[[str], str] | None = None,
                 push_bot_identity: tuple[str, str] | None = None,
                 merge_bot_identity: tuple[str, str] | None = None,
                 token_provider_factory=InstallationTokenProvider):
        self._api_url = api_url.rstrip("/")
        self._floor = floor
        self._repos_root = Path(repos_root)
        self._owns = http_client is None
        self._rest = http_client or httpx.Client(timeout=30.0)
        self._push_tokens = token_provider_factory(push_credentials, client=self._rest)
        self._merge_tokens = token_provider_factory(merge_credentials, client=self._rest)
        self._remote_url_builder = remote_url_builder or default_remote_url
        self._push_author = push_bot_identity or (
            "factory-push-bot[bot]",
            f"{push_credentials.app_id}+factory-push-bot[bot]@users.noreply.github.com",
        )
        self._merge_author = merge_bot_identity or (
            "factory-merge-bot[bot]",
            f"{merge_credentials.app_id}+factory-merge-bot[bot]@users.noreply.github.com",
        )

    def close(self) -> None:
        if self._owns:
            self._rest.close()

    def __enter__(self) -> "RestGitHub":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ── REST helpers ──────────────────────────────────────────────────────────
    def _push_headers(self) -> dict:
        return github_headers(self._push_tokens.token())

    def _merge_headers(self) -> dict:
        return github_headers(self._merge_tokens.token())

    def _url(self, path: str) -> str:
        return f"{self._api_url}{path}"

    def _get_pages(self, url: str, headers: dict, *, params: dict | None = None) -> Iterable[dict | list]:
        next_url, next_params = url, params
        while next_url:
            resp = self._rest.get(next_url, headers=headers, params=next_params)
            _raise_for_status(resp)
            yield resp.json()
            next_url = next_link_url(resp.headers.get("Link"))
            next_params = None  # already folded into next_url by GitHub

    # ── GitHubPort: read-only, push-bot identity ────────────────────────────
    def get_issue(self, repo: str, number: int) -> WorkItem:
        resp = self._rest.get(self._url(f"/repos/{repo}/issues/{number}"), headers=self._push_headers())
        _raise_for_status(resp)
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
        _raise_for_status(resp)

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
            _raise_for_status(resp)

    def head_commit(self, repo: str, branch: str = "main") -> str:
        resp = self._rest.get(self._url(f"/repos/{repo}/commits/{branch}"), headers=self._push_headers())
        _raise_for_status(resp)
        return resp.json()["sha"]

    def open_pr(self, repo: str, *, head: str, base: str, title: str, body: str) -> int:
        resp = self._rest.post(self._url(f"/repos/{repo}/pulls"),
                                json={"head": head, "base": base, "title": title, "body": body},
                                headers=self._push_headers())
        _raise_for_status(resp)
        return resp.json()["number"]

    def pr_head_sha(self, repo: str, number: int) -> str:
        resp = self._rest.get(self._url(f"/repos/{repo}/pulls/{number}"), headers=self._push_headers())
        _raise_for_status(resp)
        return resp.json()["head"]["sha"]

    def check_runs(self, repo: str, sha: str) -> list[CheckResult]:
        results = []
        pages = self._get_pages(self._url(f"/repos/{repo}/commits/{sha}/check-runs"),
                                self._push_headers(), params={"per_page": PAGE_SIZE})
        for page in pages:
            for run in page.get("check_runs", []):
                if run.get("status") != "completed" or run.get("conclusion") is None:
                    conclusion = "missing"
                else:
                    conclusion = _CONCLUSION_MAP.get(run["conclusion"], "failure")
                detail = (run.get("output") or {}).get("title") or ""
                results.append(CheckResult(name=run["name"], conclusion=conclusion, detail=detail,
                                            url=run.get("html_url")))
        return results

    def pr_reviews(self, repo: str, number: int) -> list[dict]:
        reviews = []
        pages = self._get_pages(self._url(f"/repos/{repo}/pulls/{number}/reviews"),
                                self._push_headers(), params={"per_page": PAGE_SIZE})
        for page in pages:
            reviews.extend({"login": r["user"]["login"], "state": r["state"], "commit_id": r["commit_id"]}
                           for r in page)
        return reviews

    # ── GitHubPort: writes, push-bot identity ───────────────────────────────
    def push_diff(self, repo: str, *, branch: str, diff: Diff, message: str,
                  product_forbidden: Iterable[str] = (), product_protected: Iterable[str] = ()) -> str:
        product_forbidden = tuple(product_forbidden)
        product_protected = tuple(product_protected)
        classification = classify(diff.changes, self._floor, product_forbidden=product_forbidden,
                                  product_protected=product_protected)
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

            # R-A2 backstop: never trust diff.changes alone. Reclassify against
            # the paths git itself says are staged, unquoted.
            real_changes = _real_staged_changes(workdir, diff.base_commit)
            backstop = classify(real_changes, self._floor, product_forbidden=product_forbidden,
                                product_protected=product_protected)
            if backstop.blocked:
                logger.warning(
                    "push_diff backstop caught an AC8 diff for %s that the upstream diff missed: %s",
                    repo, backstop.reasons,
                )
                raise ForbiddenDiff(
                    f"refusing to push AC8 diff to {repo} (real staged paths): "
                    f"{'; '.join(backstop.reasons)}"
                )

            _run_git(["commit", "--quiet", "-m", message], cwd=workdir,
                      config={"user.name": self._push_author[0], "user.email": self._push_author[1]})
            sha = _run_git(["rev-parse", "HEAD"], cwd=workdir).strip()
            _run_git(["push", "--quiet", remote_url, f"HEAD:refs/heads/{branch}"],
                      cwd=workdir, token=token)
            return sha
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def revert_commit(self, repo: str, sha: str, *, branch: str = "main") -> str:
        # Reverts push directly to a protected branch, bypassing PR review, so
        # they use the merge-bot identity: the same one the branch ruleset
        # trusts to bypass required-PR protection (see github.setup).
        token = self._merge_tokens.token()
        remote_url = self._remote_url_builder(repo)
        workdir = self._repos_root / f"revert-{uuid.uuid4().hex}"
        workdir.parent.mkdir(parents=True, exist_ok=True)
        try:
            _run_git(["clone", "--no-tags", "--quiet", remote_url, str(workdir)],
                      cwd=workdir.parent, token=token)
            _run_git(["checkout", "--quiet", branch], cwd=workdir)
            _run_git(["revert", "--no-edit", sha], cwd=workdir,
                      config={"user.name": self._merge_author[0], "user.email": self._merge_author[1]})
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
        _raise_for_status(resp)
        return resp.json()["sha"]
