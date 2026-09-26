"""`RepoMirror`: a local bare mirror of a product repo that sandboxes clone from (§12.2 M8).

Fetch-only, read identity (the push-bot's installation token — mirroring
never pushes; that's `RestGitHub.push_diff`'s job, on a fresh throwaway
clone). Credentials are never persisted into the mirror's on-disk config:
the token travels through `gitcmd.run_git`'s `auth_header`/`token`, exactly
like `github.client`.

The mirror path convention here is NOT `<owner>/<name>.git` — it's whatever
`sandbox.base.CheckoutSandbox._mirror_path` expects, because that's what
actually clones from it: `repos_root/<owner>__<name>` (no `.git` suffix,
`/` replaced with `__`). `mirror_path()` duplicates that one-line formula
rather than importing a private method off `CheckoutSandbox`; keep the two
in sync if either changes (see the B2 gap-wave report for the check against
`sandbox/base.py` this was verified against).
"""
from __future__ import annotations

import logging
import os
from typing import Callable

from .. import gitcmd
from ..ports import NotFound
from .app_auth import InstallationTokenProvider
from .client import basic_auth_header, default_remote_url

logger = logging.getLogger(__name__)

SYNC_TIMEOUT_S = 600


def mirror_path(repos_root: str, repo: str) -> str:
    """`repos_root/<owner>__<name>` — matches `sandbox.base.CheckoutSandbox._mirror_path`."""
    return os.path.join(repos_root, repo.replace("/", "__"))


def _sync_git(args: list[str], *, cwd: str, token: str) -> str:
    return gitcmd.run_git(args, cwd=cwd, token=token, auth_header=basic_auth_header(token),
                          timeout=SYNC_TIMEOUT_S)


class GitMirror:
    """`RepoMirror` backed by a real `git clone --mirror` / `remote update --prune`."""

    def __init__(self, *, repos_root: str, token_provider: InstallationTokenProvider,
                 remote_url_builder: Callable[[str], str] = default_remote_url):
        self._repos_root = repos_root
        self._token_provider = token_provider
        self._remote_url_builder = remote_url_builder

    def sync(self, repo: str) -> str:
        """Creates the mirror if it's missing, else updates it in place. Returns its path."""
        path = mirror_path(self._repos_root, repo)
        token = self._token_provider.token()
        if os.path.isdir(path):
            _sync_git(["remote", "update", "--prune"], cwd=path, token=token)
        else:
            os.makedirs(self._repos_root, exist_ok=True)
            remote_url = self._remote_url_builder(repo)
            _sync_git(["clone", "--mirror", "--quiet", remote_url, path],
                      cwd=self._repos_root, token=token)
        return path


class FakeMirror:
    """`RepoMirror` test/demo double: `sync` just returns a pre-registered local path."""

    def __init__(self, paths: dict[str, str] | None = None):
        self._paths = dict(paths or {})

    def register(self, repo: str, path: str) -> None:
        self._paths[repo] = path

    def sync(self, repo: str) -> str:
        try:
            return self._paths[repo]
        except KeyError:
            raise NotFound(f"no fake mirror registered for {repo!r}") from None
