"""Deploy targets (final draft §7, §11).

"Release / deploy controller: deterministic. Digest, staging deploy, prod promotion.
The only holder of prod credentials; consumes H2." `CommandDeployTarget` is the
production adapter: it shells out to operator-supplied commands and never touches
git, GitHub or LLM credentials.

**Environment.** Each hook runs with a minimal, allowlisted environment: `PATH` and
`HOME` (so the shell and any tool it invokes can find a binary and a home directory),
whatever the caller passes as `extra_env` (its own deploy-specific configuration,
e.g. a kubeconfig path or a registry URL), and the identifiers this module defines
for that call, all under a `FACTORY_DEPLOY_` prefix (`FACTORY_DEPLOY_ARTIFACT`,
`FACTORY_DEPLOY_ENVIRONMENT`, ...). The controller process's own environment is never
inherited wholesale -- these hooks run with agent-adjacent trust (an operator wrote
them, but they're driven by data the pipeline produced), and a stray secret sitting in
the controller's process environment (an API key, a database URL, ...) must not leak
into them just because `os.environ` happened to contain it.

**Fencing.** An approval is single-use and consumed with a compare-and-swap plus a
monotonically increasing fencing token (final draft §10, `controller.approvals`).
`CommandDeployTarget` enforces the same rule at the point of effect: it keeps the
highest fencing token it has ever accepted **per environment**, persisted to a small
JSON file so a restarted controller doesn't forget it, and rejects anything not
strictly greater. This is a second, independent check -- the approval store already
guards `consume()` -- defense in depth against a stale or replayed deploy.

The fencing file is never silently reset: if it exists but doesn't parse, that's
`FencingStateError`, not "assume nothing was ever deployed" -- a reset would let an
already-superseded (stale) fencing token pass the check again. Writes are atomic
(write a temp file, `os.replace` it into place), so a crash mid-write can't leave a
half-written, corrupt file behind. An `fcntl.flock` -- held on a dedicated lock file,
never on the data file itself, so the atomic rename can't invalidate a lock a
concurrent process is still holding -- spans the check, the deploy command, and the
record, so two concurrent `deploy()` calls for the same environment can't both read
the same "highest token so far" and both proceed.
"""
import fcntl
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager, suppress
from pathlib import Path

from ..controller.approvals import StaleApproval
from ..models import DeployReceipt

REQUIRED_COMMANDS = ("build", "deploy", "health", "rollback", "url")
ENV_PREFIX = "FACTORY_DEPLOY_"
ALLOWLISTED_HOST_ENV = ("PATH", "HOME")


class DeployCommandError(Exception):
    """A configured shell hook exited non-zero."""


class FencingStateError(Exception):
    """The persisted fencing state file exists but couldn't be parsed.

    This never falls back to "treat it as empty": that would silently reset every
    environment's highest-seen fencing token to 0, letting an already-superseded
    (stale) deploy token pass the check again.
    """


class CommandDeployTarget:
    """Runs configured shell commands for build, deploy, health, rollback and url.

    `commands` maps each of REQUIRED_COMMANDS to a shell command string. Each command
    receives the artifact digest, environment, and related identifiers as **environment
    variables**, prefixed `FACTORY_DEPLOY_` (`FACTORY_DEPLOY_ARTIFACT`,
    `FACTORY_DEPLOY_ENVIRONMENT`, `FACTORY_DEPLOY_REPO`, `FACTORY_DEPLOY_SHA`,
    `FACTORY_DEPLOY_OPERATION_ID`, `FACTORY_DEPLOY_TO_ARTIFACT`) -- never interpolated
    into the command string, so a value can't break out of the command. `extra_env`
    (unprefixed, used as-is) carries whatever else the hooks need -- a kubeconfig path,
    a registry URL -- since the full controller process environment is never passed
    through (see the module docstring).
    """

    def __init__(self, *, commands: dict[str, str], fencing_state_path: str, timeout_s: int = 900,
                 extra_env: dict[str, str] | None = None):
        missing = [name for name in REQUIRED_COMMANDS if name not in commands]
        if missing:
            raise ValueError(f"CommandDeployTarget is missing commands for: {', '.join(missing)}")
        self._commands = dict(commands)
        self._fencing_path = Path(fencing_state_path)
        self._lock_path = self._fencing_path.with_name(self._fencing_path.name + ".lock")
        self._timeout_s = timeout_s
        self._extra_env = dict(extra_env or {})

    def build(self, repo: str, sha: str) -> str:
        out = self._run("build", {"REPO": repo, "SHA": sha})
        digest = out.strip().splitlines()[-1].strip() if out.strip() else ""
        if not digest.startswith("sha256:"):
            raise DeployCommandError(
                f"build command must print an artifact digest starting with 'sha256:' on its last "
                f"line, got {digest!r}")
        return digest

    def deploy(self, artifact: str, *, environment: str, operation_id: str, fencing_token: int) -> DeployReceipt:
        with self._fencing_lock():
            highest = self._read_fencing().get(environment, 0)
            if fencing_token <= highest:
                raise StaleApproval(f"deploy to {environment!r}: fencing token {fencing_token} <= {highest}")

            out = self._run("deploy", {
                "ARTIFACT": artifact, "ENVIRONMENT": environment, "OPERATION_ID": operation_id})

            state = self._read_fencing()
            state[environment] = max(fencing_token, state.get(environment, 0))
            self._write_fencing_atomic(state)

        return DeployReceipt(operation_id=operation_id, environment=environment, artifact=artifact,
                              status="deployed", detail=out.strip())

    def healthy(self, environment: str) -> bool:
        try:
            self._run("health", {"ENVIRONMENT": environment})
        except DeployCommandError:
            return False
        return True

    def rollback(self, environment: str, *, to_artifact: str | None, operation_id: str) -> DeployReceipt:
        env = {"ENVIRONMENT": environment, "OPERATION_ID": operation_id, "TO_ARTIFACT": to_artifact or ""}
        out = self._run("rollback", env)
        return DeployReceipt(operation_id=operation_id, environment=environment,
                              artifact=to_artifact or "", status="rolled_back", detail=out.strip())

    def url(self, environment: str) -> str:
        return self._run("url", {"ENVIRONMENT": environment}).strip()

    # -- fencing -----------------------------------------------------------------------

    @contextmanager
    def _fencing_lock(self):
        """Hold an exclusive lock across a check + deploy + record cycle.

        The lock lives on a dedicated `<fencing_state_path>.lock` file, never on the
        data file itself: `_write_fencing_atomic` replaces the data file's inode via
        `os.replace`, and flock is bound to the *file description*, not the path -- a
        lock taken on a path that then gets replaced would silently stop protecting
        the new file. The lock file's own identity never changes, so this is safe.
        """
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path, "a+") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _read_fencing(self) -> dict[str, int]:
        if not self._fencing_path.exists():
            return {}
        content = self._fencing_path.read_text()
        if not content.strip():
            return {}
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise FencingStateError(f"fencing state file {self._fencing_path} is corrupt: {exc}") from exc

    def _write_fencing_atomic(self, state: dict[str, int]) -> None:
        self._fencing_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self._fencing_path.parent), prefix=".fencing-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as tmp:
                tmp.write(json.dumps(state))
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, self._fencing_path)
        except BaseException:
            with suppress(FileNotFoundError):
                os.remove(tmp_name)
            raise

    # -- shelling out --------------------------------------------------------------------

    def _run(self, name: str, hook_vars: dict[str, str]) -> str:
        env = {key: os.environ[key] for key in ALLOWLISTED_HOST_ENV if key in os.environ}
        env.update(self._extra_env)
        env.update({ENV_PREFIX + key: value for key, value in hook_vars.items()})
        try:
            result = subprocess.run(self._commands[name], shell=True, env=env, capture_output=True,
                                     text=True, timeout=self._timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise DeployCommandError(f"{name} command timed out after {self._timeout_s}s") from exc
        if result.returncode != 0:
            raise DeployCommandError(
                f"{name} command exited {result.returncode}: {result.stderr.strip()[-2000:]}")
        return result.stdout


class FakeDeployTarget:
    """In-memory deploy target for tests and `factory demo`, with a health toggle to
    simulate a post-deploy regression."""

    def __init__(self):
        self._deployed: dict[str, str] = {}
        self._healthy: dict[str, bool] = {}
        self._highest_token: dict[str, int] = {}
        self.receipts: list[DeployReceipt] = []

    def build(self, repo: str, sha: str) -> str:
        from ..models import sha256_text
        return sha256_text(f"{repo}@{sha}")

    def deploy(self, artifact: str, *, environment: str, operation_id: str, fencing_token: int) -> DeployReceipt:
        highest = self._highest_token.get(environment, 0)
        if fencing_token <= highest:
            raise StaleApproval(f"deploy to {environment!r}: fencing token {fencing_token} <= {highest}")
        self._highest_token[environment] = fencing_token
        self._deployed[environment] = artifact
        self._healthy.setdefault(environment, True)
        receipt = DeployReceipt(operation_id=operation_id, environment=environment, artifact=artifact,
                                 status="deployed")
        self.receipts.append(receipt)
        return receipt

    def healthy(self, environment: str) -> bool:
        return self._healthy.get(environment, True)

    def set_healthy(self, environment: str, healthy: bool) -> None:
        """Test hook: flip health to simulate a post-deploy regression."""
        self._healthy[environment] = healthy

    def rollback(self, environment: str, *, to_artifact: str | None, operation_id: str) -> DeployReceipt:
        artifact = to_artifact or self._deployed.get(environment, "")
        self._deployed[environment] = artifact
        self._healthy[environment] = True
        receipt = DeployReceipt(operation_id=operation_id, environment=environment, artifact=artifact,
                                 status="rolled_back")
        self.receipts.append(receipt)
        return receipt

    def url(self, environment: str) -> str:
        return f"https://{environment}.fake.local"
