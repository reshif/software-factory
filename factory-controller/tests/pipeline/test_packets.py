"""`pipeline.packets`: decision-packet construction (final draft §10)."""
from datetime import datetime, timedelta, timezone

from factory.models import MissionRecord
from factory.pipeline import packets


def _mission() -> MissionRecord:
    return MissionRecord(mission_id="MIS-1", product="backend-service", repo="org/backend-service",
                         work_item_id="org/backend-service#1", lane="feature", risk_profile="standard",
                         autonomy_level="L3", kit_version="factory-kit@0.1.0", policy_version="p-2026.10.2")


def test_new_request_id_is_scoped_to_the_gate():
    assert packets.new_request_id("H1").startswith("REQ-H1-")
    assert packets.new_request_id("HX").startswith("REQ-HX-")


def test_build_packet_binds_mission_and_extra_mission_ids():
    mission = _mission()
    expires = datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(days=2)
    packet = packets.build_packet(
        request_id="REQ-H2-1", gate="H2", mission=mission, title="Release", recommendation="APPROVE: x",
        summary="a release", required="1", expires=expires, content_hash="sha256:abc",
        extra_mission_ids=("MIS-2",))
    assert packet.mission_ids == ("MIS-1", "MIS-2")
    assert packet.gate == "H2"
    assert packet.required == "1"


def test_hx_title_includes_mission_id_and_reason():
    title = packets.hx_title(_mission(), "budget exhausted")
    assert "MIS-1" in title
    assert "budget exhausted" in title
