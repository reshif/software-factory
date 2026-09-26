import pytest

from factory.controller.approvals import StaleApproval
from factory.release.deploy import CommandDeployTarget, DeployCommandError, FakeDeployTarget


def make_target(tmp_path, **overrides):
    commands = {
        "build": 'python3 -c "import hashlib,os;'
                 'print(\'sha256:\'+hashlib.sha256((os.environ[\'REPO\']+\'@\'+os.environ[\'SHA\']).encode()).hexdigest())"',
        "deploy": 'echo "deployed $ARTIFACT to $ENVIRONMENT ($OPERATION_ID)"',
        "health": "true",
        "rollback": 'echo "rolled back $ENVIRONMENT to $TO_ARTIFACT"',
        "url": 'echo "https://$ENVIRONMENT.example.com"',
    }
    commands.update(overrides)
    return CommandDeployTarget(commands=commands, fencing_state_path=str(tmp_path / "fencing.json"))


def test_build_returns_digest(tmp_path):
    target = make_target(tmp_path)
    digest = target.build("acme/app", "deadbeef")
    assert digest.startswith("sha256:")


def test_build_rejects_non_digest_output(tmp_path):
    target = make_target(tmp_path, build="echo not-a-digest")
    with pytest.raises(DeployCommandError):
        target.build("acme/app", "deadbeef")


def test_deploy_runs_with_env_vars(tmp_path):
    target = make_target(tmp_path)
    receipt = target.deploy("sha256:abc", environment="staging", operation_id="op-1", fencing_token=1)
    assert receipt.status == "deployed"
    assert "sha256:abc" in receipt.detail
    assert "staging" in receipt.detail
    assert "op-1" in receipt.detail


def test_deploy_rejects_stale_fencing_token(tmp_path):
    target = make_target(tmp_path)
    target.deploy("sha256:abc", environment="staging", operation_id="op-1", fencing_token=5)
    with pytest.raises(StaleApproval):
        target.deploy("sha256:def", environment="staging", operation_id="op-2", fencing_token=5)
    with pytest.raises(StaleApproval):
        target.deploy("sha256:def", environment="staging", operation_id="op-3", fencing_token=3)


def test_fencing_is_persisted_across_instances(tmp_path):
    target1 = make_target(tmp_path)
    target1.deploy("sha256:abc", environment="prod", operation_id="op-1", fencing_token=10)

    target2 = make_target(tmp_path)  # a fresh instance, same fencing_state_path
    with pytest.raises(StaleApproval):
        target2.deploy("sha256:def", environment="prod", operation_id="op-2", fencing_token=10)
    # a higher token still works
    target2.deploy("sha256:def", environment="prod", operation_id="op-3", fencing_token=11)


def test_fencing_is_scoped_per_environment(tmp_path):
    target = make_target(tmp_path)
    target.deploy("sha256:abc", environment="staging", operation_id="op-1", fencing_token=5)
    # same token is fine in a different environment
    target.deploy("sha256:abc", environment="prod", operation_id="op-2", fencing_token=5)


def test_healthy_reflects_command_exit_code(tmp_path):
    healthy_target = make_target(tmp_path, health="true")
    assert healthy_target.healthy("prod") is True

    unhealthy_target = make_target(tmp_path, health="false")
    assert unhealthy_target.healthy("prod") is False


def test_rollback_runs_with_to_artifact(tmp_path):
    target = make_target(tmp_path)
    receipt = target.rollback("prod", to_artifact="sha256:old", operation_id="op-9")
    assert receipt.status == "rolled_back"
    assert "sha256:old" in receipt.detail


def test_url_returns_command_output(tmp_path):
    target = make_target(tmp_path)
    assert target.url("staging") == "https://staging.example.com"


def test_missing_required_command_raises_at_construction(tmp_path):
    with pytest.raises(ValueError):
        CommandDeployTarget(commands={"build": "echo sha256:x"}, fencing_state_path=str(tmp_path / "f.json"))


def test_failing_deploy_command_raises_and_does_not_advance_fencing(tmp_path):
    target = make_target(tmp_path, deploy="exit 1")
    with pytest.raises(DeployCommandError):
        target.deploy("sha256:abc", environment="staging", operation_id="op-1", fencing_token=1)
    # fencing wasn't recorded, so the same token can be retried
    target2 = make_target(tmp_path)
    target2.deploy("sha256:abc", environment="staging", operation_id="op-2", fencing_token=1)


# -- FakeDeployTarget --------------------------------------------------------------------

def test_fake_deploy_target_basic_flow():
    target = FakeDeployTarget()
    digest = target.build("acme/app", "sha1")
    assert digest.startswith("sha256:")
    receipt = target.deploy(digest, environment="prod", operation_id="op-1", fencing_token=1)
    assert receipt.status == "deployed"
    assert target.healthy("prod") is True


def test_fake_deploy_target_health_toggle_simulates_regression():
    target = FakeDeployTarget()
    target.deploy("sha256:a", environment="prod", operation_id="op-1", fencing_token=1)
    assert target.healthy("prod") is True
    target.set_healthy("prod", False)
    assert target.healthy("prod") is False


def test_fake_deploy_target_rollback_restores_health():
    target = FakeDeployTarget()
    target.deploy("sha256:a", environment="prod", operation_id="op-1", fencing_token=1)
    target.set_healthy("prod", False)
    receipt = target.rollback("prod", to_artifact="sha256:old", operation_id="op-2")
    assert receipt.status == "rolled_back"
    assert target.healthy("prod") is True


def test_fake_deploy_target_rejects_stale_fencing_token():
    target = FakeDeployTarget()
    target.deploy("sha256:a", environment="prod", operation_id="op-1", fencing_token=5)
    with pytest.raises(StaleApproval):
        target.deploy("sha256:b", environment="prod", operation_id="op-2", fencing_token=5)
