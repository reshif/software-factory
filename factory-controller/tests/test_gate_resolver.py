import pytest

from factory.controller.gate_resolver import hx_requirement, release_requirement, resolve
from factory.policy import PolicyError, Requirement, parse_requirement, strictest


def req(text):
    return parse_requirement(text)


def test_strictest_merges_counts_and_security():
    assert strictest(req("2"), req("1+sec")) == req("2+sec")
    assert strictest(req("auto"), req("standing")) == req("standing")
    assert strictest(req("1"), req("hx")) == req("hx")
    assert strictest(req("hx"), req("blocked")) == req("blocked")
    assert strictest(req("auto"), req("auto_sampled")) == req("auto_sampled")


@pytest.mark.parametrize("cls,profile,h1,hm,h2", [
    ("AC1", "experimental", "standing", "auto", "1"),
    ("AC1", "standard", "standing", "1", "1"),
    ("AC3", "regulated", "standing", "1", "2"),
    ("AC4", "standard", "1", "1", "1"),
    ("AC4", "regulated", "2+sec", "2", "2"),
    ("AC6", "standard", "1+sec", "1+sec", "1"),
    ("AC7", "standard", "hx", "hx", "hx"),
    ("AC8", "experimental", "blocked", "blocked", "blocked"),
])
def test_base_table_at_l3(policy, cls, profile, h1, hm, h2):
    d = resolve(policy, action_class=cls, risk_profile=profile, covered_by_standing=True)
    assert (str(d.h1), str(d.hm), str(d.h2)) == (h1, hm, h2)


def test_protected_paths_pull_in_ac6_row(policy):
    d = resolve(policy, action_class="AC4", risk_profile="standard", protected_touched=True)
    assert str(d.h1) == "1+sec" and str(d.hm) == "1+sec"
    assert "protected_paths->AC6" in d.rules


def test_standing_h1_requires_actual_coverage(policy):
    d = resolve(policy, action_class="AC3", risk_profile="standard", covered_by_standing=False)
    assert str(d.h1) == "1"
    assert "not_covered->mission_h1" in d.rules


def test_l4_relaxes_merge_for_standard(policy):
    d = resolve(policy, action_class="AC4", risk_profile="standard", autonomy_level="L4")
    assert str(d.hm) == "auto_sampled"
    assert str(d.h2) == "1"


def test_l5_standing_release_only_for_small_classes(policy):
    small = resolve(policy, action_class="AC2", risk_profile="standard", autonomy_level="L5",
                    covered_by_standing=True)
    feature = resolve(policy, action_class="AC4", risk_profile="standard", autonomy_level="L5")
    assert str(small.h2) == "standing"
    assert str(feature.h2) == "1"


def test_relaxations_never_apply_to_protected_work(policy):
    d = resolve(policy, action_class="AC3", risk_profile="standard", autonomy_level="L5",
                protected_touched=True)
    assert str(d.hm) == "1+sec"
    assert str(d.h2) == "1"


def test_regulated_is_capped_at_l4(policy):
    with pytest.raises(PolicyError, match="capped at L4"):
        resolve(policy, action_class="AC1", risk_profile="regulated", autonomy_level="L5")


def test_regulated_l4_relaxes_only_ac1_ac2(policy):
    assert str(resolve(policy, action_class="AC2", risk_profile="regulated", autonomy_level="L4",
                       covered_by_standing=True).hm) == "auto_sampled"
    assert str(resolve(policy, action_class="AC3", risk_profile="regulated", autonomy_level="L4",
                       covered_by_standing=True).hm) == "1"


def test_overrides_only_tighten(policy):
    tighter = resolve(policy, action_class="AC4", risk_profile="standard", overrides={"AC4": {"h2": "2"}})
    looser = resolve(policy, action_class="AC4", risk_profile="standard", overrides={"AC4": {"hm": "auto"}})
    assert str(tighter.h2) == "2"
    assert str(looser.hm) == "1"


def test_release_takes_strictest_mission(policy):
    patch = resolve(policy, action_class="AC1", risk_profile="standard", autonomy_level="L5",
                    covered_by_standing=True)
    feature = resolve(policy, action_class="AC4", risk_profile="standard")
    assert str(release_requirement([patch])) == "standing"
    assert str(release_requirement([patch, feature])) == "1"


def test_hx_quorum(policy):
    assert hx_requirement(policy, "standard") == Requirement("approve", approvals=1)
    assert hx_requirement(policy, "standard", destructive_or_credentials=True).approvals == 2
    assert hx_requirement(policy, "regulated").approvals == 2
