"""Auto-revert on a failing post-merge check (final draft §7).

"POST -->|fail| REV[Auto-revert commit; mission -> REPAIRING] --> EXE". When the
combined checks on `main` fail after a merge, the controller reverts that merge
commit immediately, without waiting for a human -- the mission itself moves to
REPAIRING and re-enters verification (that state transition is the orchestrator's
job; this module only performs the GitHub side effect).
"""
import logging

from ..ports import GitHubPort

logger = logging.getLogger(__name__)


def auto_revert(github: GitHubPort, repo: str, merge_sha: str) -> str:
    """Revert `merge_sha` on `repo`'s default branch and return the revert commit sha."""
    revert_sha = github.revert_commit(repo, merge_sha)
    logger.info("auto-reverted %s on %s -> %s", merge_sha, repo, revert_sha)
    return revert_sha
