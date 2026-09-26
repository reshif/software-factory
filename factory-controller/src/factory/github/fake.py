"""In-memory GitHub double for tests and `factory demo` (same semantics as `RestGitHub`).

Keeps issues, branches, PRs, reviews and check runs in memory. `push_diff`
classifies the diff against the security floor and refuses AC8, exactly like
the real adapter (final draft §13.1 #3).
"""
import hashlib
import itertools
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from ..models import CheckResult, Diff, WorkItem
from ..policy import Floor
from ..policy.action_classes import classify
from ..ports import NotFound
from .client import ForbiddenDiff, MergeConflict


def _fake_sha(*parts: str) -> str:
    return hashlib.sha1("\x00".join(parts).encode()).hexdigest()


@dataclass
class _PullRequest:
    number: int
    repo: str
    head: str
    base: str
    title: str
    body: str
    merged: bool = False
    merge_sha: str | None = None


@dataclass
class _Issue:
    item: WorkItem
    comments: list = field(default_factory=list)
    labels: set = field(default_factory=set)


class FakeGitHub:
    """Semantically equivalent to `RestGitHub`, backed by dicts instead of the network."""

    def __init__(self, *, floor: Floor, default_branch: str = "main"):
        self._floor = floor
        self._default_branch = default_branch
        self._issues: dict[tuple[str, int], _Issue] = {}
        self._branches: dict[tuple[str, str], str] = {}
        self._commits: dict[tuple[str, str], dict] = {}
        self._prs: dict[tuple[str, int], _PullRequest] = {}
        self._pr_numbers = itertools.count(1)
        self._reviews: dict[tuple[str, int], list[dict]] = defaultdict(list)
        self._check_runs: dict[tuple[str, str], list[CheckResult]] = {}

    # ── test/demo helpers ────────────────────────────────────────────────────
    def add_issue(self, item: WorkItem) -> None:
        self._issues[(item.repo, item.number)] = _Issue(item=item)

    def set_checks(self, repo: str, sha: str, checks: list[CheckResult]) -> None:
        self._check_runs[(repo, sha)] = list(checks)

    def add_review(self, repo: str, number: int, *, login: str, state: str, commit_id: str) -> None:
        self._reviews[(repo, number)].append({"login": login, "state": state, "commit_id": commit_id})

    def comments(self, repo: str, number: int) -> list[str]:
        return list(self._issues[(repo, number)].comments)

    def labels(self, repo: str, number: int) -> set:
        return set(self._issues[(repo, number)].labels)

    def pr(self, repo: str, number: int) -> _PullRequest:
        return self._get_pr(repo, number)

    def _get_issue(self, repo: str, number: int) -> _Issue:
        try:
            return self._issues[(repo, number)]
        except KeyError:
            raise NotFound(f"no issue {repo}#{number}") from None

    def _get_pr(self, repo: str, number: int) -> _PullRequest:
        try:
            return self._prs[(repo, number)]
        except KeyError:
            raise NotFound(f"no pull request {repo}#{number}") from None

    # ── GitHubPort: read-only ────────────────────────────────────────────────
    def get_issue(self, repo: str, number: int) -> WorkItem:
        return self._get_issue(repo, number).item

    def comment_issue(self, repo: str, number: int, body: str) -> None:
        self._get_issue(repo, number).comments.append(body)

    def mirror_labels(self, repo: str, number: int, *, add: Iterable[str] = (),
                       remove: Iterable[str] = ()) -> None:
        labels = self._get_issue(repo, number).labels
        labels.difference_update(remove)
        labels.update(add)

    def head_commit(self, repo: str, branch: str = "main") -> str:
        key = (repo, branch)
        if key not in self._branches:
            self._branches[key] = _fake_sha("init", repo, branch)
        return self._branches[key]

    def open_pr(self, repo: str, *, head: str, base: str, title: str, body: str) -> int:
        number = next(self._pr_numbers)
        self._prs[(repo, number)] = _PullRequest(number=number, repo=repo, head=head, base=base,
                                                  title=title, body=body)
        return number

    def pr_head_sha(self, repo: str, number: int) -> str:
        pr = self._get_pr(repo, number)
        return self._branches.get((repo, pr.head), pr.head)

    def check_runs(self, repo: str, sha: str) -> list[CheckResult]:
        return list(self._check_runs.get((repo, sha), []))

    def pr_reviews(self, repo: str, number: int) -> list[dict]:
        return list(self._reviews.get((repo, number), []))

    # ── GitHubPort: writes ───────────────────────────────────────────────────
    def push_diff(self, repo: str, *, branch: str, diff: Diff, message: str,
                  product_forbidden: Iterable[str] = (), product_protected: Iterable[str] = ()) -> str:
        classification = classify(diff.changes, self._floor, product_forbidden=product_forbidden,
                                  product_protected=product_protected)
        if classification.blocked:
            raise ForbiddenDiff(
                f"refusing to push AC8 diff to {repo}: {'; '.join(classification.reasons)}"
            )
        sha = _fake_sha(repo, branch, diff.content_hash, message)
        self._branches[(repo, branch)] = sha
        self._commits[(repo, sha)] = {"base_commit": diff.base_commit, "message": message, "diff": diff}
        return sha

    def revert_commit(self, repo: str, sha: str, *, branch: str = "main") -> str:
        revert_sha = _fake_sha("revert", repo, sha, branch)
        self._branches[(repo, branch)] = revert_sha
        self._commits[(repo, revert_sha)] = {"reverts": sha}
        return revert_sha

    def merge_pr(self, repo: str, number: int, *, expected_head_sha: str) -> str:
        pr = self._get_pr(repo, number)
        actual = self.pr_head_sha(repo, number)
        if actual != expected_head_sha:
            raise MergeConflict(
                f"merge of {repo}#{number} rejected: expected head {expected_head_sha}, actual {actual}"
            )
        merge_sha = _fake_sha("merge", repo, str(number), actual)
        pr.merged = True
        pr.merge_sha = merge_sha
        self._branches[(repo, pr.base)] = merge_sha
        return merge_sha
