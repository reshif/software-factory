"""Single hardened git runner, shared by every module that shells out to git.

An agent-writable checkout is untrusted input. Git's own configuration can
execute arbitrary commands from inside a repository the caller doesn't fully
control — `core.fsmonitor`, `core.hooksPath`, `core.pager`, `.gitattributes`
filter/diff drivers, `ext::` remote helpers — so every invocation here:

  - ignores the ambient environment's git config entirely
    (`GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` -> `/dev/null`,
    `GIT_CONFIG_NOSYSTEM=1`), so a stray `~/.gitconfig` on the host can't
    inject anything either;
  - hardens the invocation itself even when the caller also points
    `--git-dir` outside the untrusted working tree (`core.fsmonitor=false`,
    `core.hooksPath=/dev/null`, `core.pager=cat`, `protocol.ext.allow=never`,
    `core.quotePath=false` so `-z` output and plain paths are never
    C-style quoted — see `sandbox/gitutils.py` for why that quoting matters);
  - enforces a timeout instead of hanging forever; and
  - never leaks a credential: `token`, if given, is redacted out of every
    exception message this module raises.

`sandbox/` uses this with an explicit `git_dir` OUTSIDE the mounted/executed
workdir (see `sandbox/base.py`) and no `token` at all — sandboxes get no
credentials. `token`, when given, is passed as an `http.extraHeader` through
git's environment-based config (`GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/
`GIT_CONFIG_VALUE_n`), never on argv, so it doesn't show up in `ps` or
`/proc/<pid>/cmdline`. By default that header is a Bearer token; a caller
whose remote wants a different scheme (GitHub's git-over-HTTPS needs Basic
`x-access-token:<token>`, not Bearer) passes the exact header line via
`auth_header` instead — `token` is still supplied alongside it purely so
this module keeps redacting it from error messages.
"""
from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger(__name__)

_HARDENED_FLAGS = (
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.pager=cat",
    "-c", "protocol.ext.allow=never",
    "-c", "core.quotePath=false",
)

_ISOLATED_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}


class GitError(Exception):
    """A git invocation failed, timed out, or was refused. Never carries a raw token."""


def _redact(text: str, token: str | None) -> str:
    if token:
        text = text.replace(token, "***REDACTED***")
    return text


def run_git(
    args: list[str],
    cwd: str | None = None,
    *,
    git_dir: str | None = None,
    work_tree: str | None = None,
    token: str | None = None,
    auth_header: str | None = None,
    timeout: float = 60.0,
) -> str:
    """Run a hardened `git` invocation and return stdout.

    `git_dir`/`work_tree`, when given, are passed as explicit `--git-dir`/
    `--work-tree` flags rather than relying on git to discover a repository
    from `cwd` — the whole point when `cwd`/`work_tree` is untrusted content.

    `token`/`auth_header`: when `token` alone is given, git's `http.extraHeader`
    is set to `Authorization: Bearer <token>`. When `auth_header` is also (or
    instead) given, it's used as that header's exact value verbatim, so a
    caller needing a different scheme (e.g. GitHub's Basic `x-access-token:
    <token>`) can supply it directly; `token` is still redacted from error
    messages either way.

    Raises `GitError` on a non-zero exit or a timeout; the message includes
    stderr, with `token` redacted if one was given.
    """
    prefix: list[str] = []
    if git_dir is not None:
        prefix.append(f"--git-dir={git_dir}")
    if work_tree is not None:
        prefix.append(f"--work-tree={work_tree}")
    cmd = ["git", *prefix, *_HARDENED_FLAGS, *args]

    env = dict(os.environ)
    env.update(_ISOLATED_ENV)
    if auth_header is not None:
        # Env-based config keeps the credential off argv (visible to `ps`).
        env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                    "GIT_CONFIG_VALUE_0": auth_header})
    elif token:
        env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {token}"})

    logger.debug("running: %s", _redact(" ".join(cmd), token))
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=True,
            text=True, encoding="utf-8", errors="surrogateescape", timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr or ""
        raise GitError(_redact(f"git {' '.join(args)} timed out after {timeout}s: {stderr}", token)) from None

    if proc.returncode != 0:
        raise GitError(_redact(f"git {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr}", token))
    return proc.stdout
