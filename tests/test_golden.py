"""End-to-end golden cases: all seed CSVs and all sample files go through the real ingest
API and worker, then the derived machine states are checked.

Run twice, with batches in file order and in reverse, to show that arrival order
doesn't change the result.
"""

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models import Job, JobStatus, MachineState
from tests.samples import ALL_BATCHES
from tests.test_worker import drain

EXPECTED_STATUS = {
    "EQ-001": "WATCH",
    "EQ-002": "CRITICAL",
    "EQ-003": "NORMAL",
    "EQ-004": "NORMAL",
    "EQ-005": "WARNING",
    "EQ-006": "NORMAL",
    "EQ-007": "UNDER_MAINTENANCE",
    "EQ-008": "WARNING",
}


@pytest.fixture(params=["file-order", "reverse-order"])
def states(request, client, make_session, reference_data) -> dict[str, MachineState]:
    batches = ALL_BATCHES if request.param == "file-order" else list(reversed(ALL_BATCHES))
    for vendor, batch in batches:
        headers = {"X-API-Key": get_settings().api_key_for(vendor)}
        response = client.post(f"/v1/ingest/{vendor.value}", json=batch, headers=headers)
        assert response.status_code == 202, response.text
    drain(make_session)
    with make_session() as session:
        assert set(session.scalars(select(Job.status))) == {JobStatus.SUCCEEDED}
        return {s.machine_id: s for s in session.scalars(select(MachineState))}


def codes(state: MachineState) -> list[str]:
    return [r["code"] for r in state.reasons]


def refs(state: MachineState, code: str) -> list[str]:
    reason = next(r for r in state.reasons if r["code"] == code)
    return [ref["source_record_id"] for ref in reason["source_event_refs"]]


def test_all_machines_have_the_expected_status(states):
    assert {m: s.derived_status for m, s in states.items()} == EXPECTED_STATUS


def test_eq002_is_critical(states):
    eq002 = states["EQ-002"]

    assert (eq002.derived_status, eq002.attention_level, eq002.needs_attention) == ("CRITICAL", 4, True)
    assert refs(eq002, "TEMPERATURE_ALERT") == ["TW-8803"]  # TEMP_CRIT, level 5
    assert refs(eq002, "TEMPERATURE_ABOVE_RATED") == ["TW-8803"]  # 97 °C vs 90 rated
    assert {"MAINTENANCE_OVERDUE", "OPEN_WORK_ORDER"} <= set(codes(eq002))


def test_eq008_still_needs_attention_after_recovery(states):
    eq008 = states["EQ-008"]

    # PF-1401's recovery (08:39) supersedes TW-9921's TEMP_CRIT (08:29) for temperature...
    assert not any(r["dimension"] == "temperature" for r in eq008.reasons)
    # ...but the 08:41 inspection still reports a major defect, and the work order is open.
    assert eq008.needs_attention
    assert refs(eq008, "INSPECTION_MAJOR_DEFECT") == ["MF-801"]
    assert refs(eq008, "OPEN_WORK_ORDER") == ["WO-1003:open"]


def test_eq001_goes_to_watch_after_out_of_order_recovery(states):
    eq001 = states["EQ-001"]

    # PF-1302 (recovery, 08:32:05) arrives before PF-1301 (alert, 08:31:02); time order wins.
    assert not any(r["dimension"] == "vibration" for r in eq001.reasons)
    assert codes(eq001) == ["INSPECTION_MINOR_DEFECT"]  # MF-501
    assert (eq001.derived_status, eq001.attention_level) == ("WATCH", 1)


def test_eq004_is_normal_after_work_order_closes_and_maintenance_completes(states):
    eq004 = states["EQ-004"]

    assert (eq004.derived_status, eq004.reasons, eq004.needs_attention) == ("NORMAL", [], False)
    assert [r["source_record_id"] for r in eq004.source_event_refs] == ["MF-701"]


def test_eq007_is_under_maintenance(states):
    eq007 = states["EQ-007"]

    assert eq007.derived_status == "UNDER_MAINTENANCE"
    assert not eq007.needs_attention
    assert {"ASSET_IN_MAINTENANCE", "INACTIVE_WITH_OPEN_WORK_ORDER", "OPEN_WORK_ORDER"} <= set(codes(eq007))
