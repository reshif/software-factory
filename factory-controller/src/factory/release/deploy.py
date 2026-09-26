"""Deploy targets (final draft §7, §11).

"Release / deploy controller: deterministic. Digest, staging deploy, prod promotion.
The only holder of prod credentials; consumes H2." `CommandDeployTarget` is the
production adapter: it shells out to operator-supplied commands and never touches
git, GitHub or LLM credentials.

Fencing: an approval is single-use and consumed with a compare-and-swap plus a
monotonically increasing fencing token (final draft §10, `controller.approvals`).
`CommandDeployTarget` enforces the same rule at the point of effect: it keeps the
highest fencing token it has ever accepted **per environment**, persisted to a small
JSON file so a restarted controller doesn't forget it, and rejects anything not
strictly greater. This is a second, independent check -- the approval store already
guards `consume()` -- defense in depth against a stale or replayed deploy.
"""
import json
import logging
import os
import subprocess
from pathlib import Path

from ..controller.approvals import StaleApproval
from ..models import DeployReceipt

logger = logging.getLogger(__name__)

REQUIRED_COMMANDS = ("build", "deploy", "health", "rollback", "url")


class DeployCommandError(Exception):
    """A configured shell hook exited non-zero."""


class CommandDeployTarget:
    """Runs configured shell commands for build, deploy, health, rollback and url.

    `commands` maps each of REQUIRED_COMMANDS to a shell command string. Each command
    receives the artifact digest, environment, and related identifiers as **environment
    variables** (ARTIFACT, ENVIRONMENT, REPO, SHA, OPERATION_ID, TO_ARTIFACT) -- never as
    interpolated shell arguments, so a value can't break out of the command.
    """

    def __init__(self, *, commands: dict[str, str], fencing_state_path: str, timeout_s: int = 900):
        missing = [name for name in REQUIRED_COMMANDS if name not in commands]
        if missing:
            raise ValueError(f"CommandDeployTarget is missing commands for: {', '.join(missing)}")
        self._commands = dict(commands)
        self._fencing_path = Path(fencing_state_path)
        self._timeout_s = timeout_s

    def build(self, repo: str, sha: str) -> str:
        out = self._run("build", {"REPO": repo, "SHA": sha})
        digest = out.strip().splitlines()[-1].strip() if out.strip() else ""
        if not digest.startswith("sha256:"):
            raise DeployCommandError(
                f"build command must print an artifact digest starting with 'sha256:' on its last "
                f"line, got {digest!r}")
        return digest

    def deploy(self, artifact: str, *, environment: str, operation_id: str, fencing_token: int) -> DeployReceipt:
        self._check_fencing(environment, fencing_token)
        out = self._run("deploy", {
            "ARTIFACT": artifact, "ENVIRONMENT": environment, "OPERATION_ID": operation_id})
        self._record_fencing(environment, fencing_token)
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

    def _load_fencing(self) -> dict[str, int]:
        if not self._fencing_path.exists():
            return {}
        try:
            return json.loads(self._fencing_path.read_text())
        except json.JSONDecodeError:
            logger.warning("fencing state file %s is corrupt; treating as empty", self._fencing_path)
            return {}

    def _check_fencing(self, environment: str, fencing_token: int) -> None:
        highest = self._load_fencing().get(environment, 0)
        if fencing_token <= highest:
            raise StaleApproval(
                f"deploy to {environment!r}: fencing token {fencing_token} <= {highest}")

    def _record_fencing(self, environment: str, fencing_token: int) -> None:
        state = self._load_fencing()
        state[environment] = max(fencing_token, state.get(environment, 0))
        self._fencing_path.parent.mkdir(parents=True, exist_ok=True)
        self._fencing_path.write_text(json.dumps(state))

    # -- shelling out --------------------------------------------------------------------

    def _run(self, name: str, extra_env: dict[str, str]) -> str:
        env = {**os.environ, **extra_env}
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
