import pytest

from factory.release.flags import FakeFlagProvider, FileFlagProvider


@pytest.fixture(params=["file", "fake"])
def provider(request, tmp_path):
    if request.param == "file":
        return FileFlagProvider(str(tmp_path / "flags.json"))
    return FakeFlagProvider()


def test_new_flag_defaults_to_off(provider):
    provider.create("mission-1")
    assert provider.rollout("mission-1") == 0


def test_creating_twice_does_not_reset_rollout(provider):
    provider.create("mission-1")
    provider.set_rollout("mission-1", 50)
    provider.create("mission-1")
    assert provider.rollout("mission-1") == 50


def test_set_rollout(provider):
    provider.create("mission-1")
    provider.set_rollout("mission-1", 25)
    assert provider.rollout("mission-1") == 25


def test_set_rollout_rejects_out_of_range(provider):
    provider.create("mission-1")
    with pytest.raises(ValueError):
        provider.set_rollout("mission-1", 101)
    with pytest.raises(ValueError):
        provider.set_rollout("mission-1", -1)


def test_kill_sets_rollout_to_zero(provider):
    provider.create("mission-1")
    provider.set_rollout("mission-1", 100)
    provider.kill("mission-1")
    assert provider.rollout("mission-1") == 0


def test_unknown_flag_is_off_fail_closed(provider):
    assert provider.rollout("never-created") == 0


def test_file_flag_provider_persists_across_instances(tmp_path):
    path = str(tmp_path / "flags.json")
    a = FileFlagProvider(path)
    a.create("mission-1")
    a.set_rollout("mission-1", 40)

    b = FileFlagProvider(path)
    assert b.rollout("mission-1") == 40
    b.kill("mission-1")

    c = FileFlagProvider(path)
    assert c.rollout("mission-1") == 0
