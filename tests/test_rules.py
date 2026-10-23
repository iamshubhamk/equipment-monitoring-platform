"""Unit tests for the pure derivation rules (no database)."""

import itertools
from datetime import UTC, date, datetime, timedelta

import pytest

from app.derivation.rules import Freshness, Status, derive_state, freshness
from app.models import Event, Machine, Vendor
from app.normalization.canonical import EventType as T

_ids = itertools.count(1)


def machine(**overrides) -> Machine:
    fields = dict(
        machine_id="EQ-001",
        plant_id="PLANT_01",
        line_id="LINE-A",
        machine_type="Press",
        criticality="high",
        installed_date=date(2021, 2, 10),
        rated_max_temp_c=85.0,
        rated_max_vibration_mm_s=9.0,
        baseline_power_kw=36.0,
        asset_status="active",
    )
    return Machine(**(fields | overrides))


def at(minute: int) -> datetime:
    return datetime(2026, 4, 18, 8, 0, tzinfo=UTC) + timedelta(minutes=minute)


def event(minute: int, event_type: T, vendor: Vendor = Vendor.PULSEFORGE, **fields) -> Event:
    n = next(_ids)
    attributes = fields.pop("attributes", {})
    return Event(
        id=n,
        vendor=vendor,
        source_record_id=f"R-{n}",
        machine_id="EQ-001",
        event_time=at(minute),
        event_type=event_type,
        attributes={"vendor_event_type": str(event_type).upper(), **attributes},
        **fields,
    )


def codes(state) -> list[str]:
    return [r.code for r in state.reasons]


def work_order(minute: int, wo_id: str, status: str, check: str = "pending") -> Event:
    return event(
        minute,
        T.WORK_ORDER,
        Vendor.WORKORDERS,
        work_order_status=status,
        post_maintenance_check=check,
        attributes={"work_order_id": wo_id},
    )


# --- basics ---------------------------------------------------------------------------


def test_no_events_is_unknown():
    state = derive_state(machine(), [])

    assert (state.status, state.attention_level, state.needs_attention) == (Status.UNKNOWN, 0, False)


def test_readings_within_limits_are_normal():
    state = derive_state(machine(), [event(0, T.RECOVERY, temperature_c=70, vibration_mm_s=3, severity=1)])

    assert (state.status, state.reasons, state.needs_attention) == (Status.NORMAL, [], False)
    assert [e.event_time for e in state.source_events] == [at(0)]  # still traceable


@pytest.mark.parametrize(
    ("severity", "status"),
    [(1, Status.WATCH), (2, Status.WATCH), (3, Status.WARNING), (4, Status.CRITICAL)],
)
def test_highest_severity_decides_status(severity, status):
    state = derive_state(machine(), [event(0, T.VIBRATION_ALERT, severity=severity)])

    assert (state.status, state.attention_level) == (status, severity)
    assert state.needs_attention


# --- latest evidence per dimension ----------------------------------------------------


def test_later_recovery_clears_earlier_alert_even_if_it_arrived_first():
    recovery = event(2, T.RECOVERY, vibration_mm_s=3.9, severity=1)
    alert = event(1, T.VIBRATION_ALERT, vibration_mm_s=12.2, severity=3)

    state = derive_state(machine(), [recovery, alert])  # arrival order: recovery first

    assert state.status == Status.NORMAL


def test_later_alert_overrides_earlier_recovery():
    state = derive_state(
        machine(),
        [event(1, T.RECOVERY, vibration_mm_s=3.9, severity=1), event(2, T.VIBRATION_ALERT, vibration_mm_s=9.5, severity=2)],
    )

    assert codes(state) == ["VIBRATION_ABOVE_RATED", "VIBRATION_ALERT"]
    assert state.status == Status.WARNING


def test_dimensions_are_independent():
    # A newer temperature reading doesn't clear an older vibration alert.
    state = derive_state(
        machine(),
        [event(1, T.VIBRATION_ALERT, severity=3), event(2, T.TEMPERATURE_ALERT, temperature_c=70, severity=1)],
    )

    assert set(codes(state)) == {"VIBRATION_ALERT", "TEMPERATURE_ALERT"}
    assert state.status == Status.WARNING


def test_vendor_severity_only_counts_for_its_own_dimension():
    # A critical temperature alert says nothing about vibration.
    state = derive_state(machine(), [event(0, T.TEMPERATURE_ALERT, temperature_c=96, vibration_mm_s=7, severity=4)])

    assert {r.dimension for r in state.reasons} == {"temperature"}


def test_recovery_with_reading_above_limit_still_counts():
    state = derive_state(machine(), [event(0, T.RECOVERY, temperature_c=90, severity=1)])

    assert codes(state) == ["TEMPERATURE_ABOVE_RATED"]
    assert state.status == Status.WARNING


def test_same_timestamp_from_two_vendors_more_severe_wins():
    state = derive_state(
        machine(),
        [
            event(0, T.TEMPERATURE_ALERT, Vendor.THERMEXWATCH, temperature_c=80, severity=4),
            event(0, T.RECOVERY, Vendor.PULSEFORGE, temperature_c=70, severity=1),
        ],
    )

    assert state.status == Status.CRITICAL


# --- thresholds against asset_reference -----------------------------------------------


def test_thermexwatch_vibration_in_g_is_not_compared_to_mm_s_limit():
    state = derive_state(machine(), [event(0, T.NOMINAL, Vendor.THERMEXWATCH, vibration_g=20.0, severity=0)])

    assert state.status == Status.NORMAL


@pytest.mark.parametrize(("power", "flagged"), [(36.0, False), (25.3, False), (25.0, True), (47.0, True)])
def test_power_more_than_30_percent_from_baseline(power, flagged):
    state = derive_state(machine(), [event(0, T.NOMINAL, Vendor.THERMEXWATCH, power_kw=power, severity=0)])

    assert ("POWER_DEVIATION" in codes(state)) is flagged


def test_low_sensor_health_and_poor_signal():
    low = derive_state(machine(), [event(0, T.RECOVERY, sensor_health=0.5)])
    poor = derive_state(machine(), [event(0, T.NOMINAL, Vendor.THERMEXWATCH, signal_quality="poor", severity=0)])

    assert codes(low) == ["SENSOR_HEALTH_LOW"] and low.status == Status.WATCH
    assert codes(poor) == ["SIGNAL_QUALITY_POOR"] and poor.status == Status.WATCH


def test_unknown_alert_is_watch_whatever_its_level():
    state = derive_state(machine(), [event(0, T.UNKNOWN, Vendor.THERMEXWATCH, severity=3)])

    assert codes(state) == ["UNMAPPED_VENDOR_ALERT"]
    assert state.status == Status.WATCH


# --- inspections, maintenance and work orders -----------------------------------------


@pytest.mark.parametrize(
    ("result", "status"),
    [
        ("major_defect_found", Status.WARNING),
        ("minor_defect_found", Status.WATCH),
        ("passed_with_observation", Status.WATCH),
        ("calibration_pending", Status.WATCH),
        ("passed", Status.NORMAL),
    ],
)
def test_latest_inspection_result(result, status):
    state = derive_state(machine(), [event(0, T.INSPECTION, Vendor.MAINTAFLOW, inspection_result=result, manual_confidence=0.9)])

    assert state.status == status
    assert all(r.confidence == 0.9 for r in state.reasons)


def test_overdue_maintenance_is_watch():
    state = derive_state(machine(), [event(0, T.MAINTENANCE_UPDATE, Vendor.MAINTAFLOW, maintenance_status="overdue")])

    assert codes(state) == ["MAINTENANCE_OVERDUE"]
    assert state.attention_level == 2


def test_operator_note_does_not_change_status():
    state = derive_state(machine(), [event(0, T.OPERATOR_NOTE, Vendor.MAINTAFLOW, attributes={"note": "stalled"})])

    assert state.status == Status.NORMAL


def test_open_work_order_on_running_machine_is_watch():
    state = derive_state(machine(), [work_order(0, "WO-1", "open"), event(1, T.RECOVERY, machine_state="running")])

    assert codes(state) == ["OPEN_WORK_ORDER"]
    assert state.status == Status.WATCH


def test_closed_work_order_is_no_longer_open():
    state = derive_state(machine(), [work_order(0, "WO-1", "open"), work_order(5, "WO-1", "closed", "failed")])

    assert "OPEN_WORK_ORDER" not in codes(state)


# --- maintenance reset ----------------------------------------------------------------


@pytest.mark.parametrize(
    "reset",
    [
        lambda m: event(m, T.MAINTENANCE_UPDATE, Vendor.MAINTAFLOW, maintenance_status="completed"),
        lambda m: work_order(m, "WO-2", "closed", "passed"),
    ],
    ids=["maintaflow-completed", "work-order-closed-passed"],
)
def test_completed_maintenance_clears_older_evidence(reset):
    state = derive_state(
        machine(),
        [
            event(0, T.POWER_ALERT, Vendor.THERMEXWATCH, power_kw=18, severity=2),
            event(1, T.INSPECTION, Vendor.MAINTAFLOW, inspection_result="major_defect_found"),
            reset(5),
        ],
    )

    assert state.status == Status.NORMAL


def test_evidence_after_maintenance_still_counts():
    state = derive_state(
        machine(),
        [
            event(0, T.MAINTENANCE_UPDATE, Vendor.MAINTAFLOW, maintenance_status="completed"),
            event(5, T.VIBRATION_ALERT, severity=3),
        ],
    )

    assert state.status == Status.WARNING


def test_closed_work_order_with_failed_check_does_not_reset():
    state = derive_state(machine(), [event(0, T.VIBRATION_ALERT, severity=3), work_order(5, "WO-1", "closed", "failed")])

    assert state.status == Status.WARNING


def test_reset_does_not_close_other_open_work_orders():
    state = derive_state(
        machine(),
        [
            work_order(0, "WO-1", "open"),
            event(5, T.MAINTENANCE_UPDATE, Vendor.MAINTAFLOW, maintenance_status="completed"),
        ],
    )

    assert codes(state) == ["OPEN_WORK_ORDER"]


# --- status overrides -----------------------------------------------------------------


def test_asset_in_maintenance_is_under_maintenance():
    state = derive_state(machine(asset_status="maintenance"), [event(0, T.INSPECTION, Vendor.MAINTAFLOW, inspection_result="calibration_pending")])

    assert state.status == Status.UNDER_MAINTENANCE
    assert not state.needs_attention
    assert state.attention_level == 1  # the underlying evidence is still reported
    assert "ASSET_IN_MAINTENANCE" in codes(state)


def test_inactive_machine_with_open_work_order_is_under_maintenance():
    state = derive_state(
        machine(),
        [work_order(0, "WO-1", "open"), event(1, T.NOMINAL, Vendor.THERMEXWATCH, machine_active=False, severity=0)],
    )

    assert state.status == Status.UNDER_MAINTENANCE
    assert "INACTIVE_WITH_OPEN_WORK_ORDER" in codes(state)


def test_critical_beats_under_maintenance():
    state = derive_state(machine(asset_status="maintenance"), [event(0, T.TEMPERATURE_ALERT, severity=4)])

    assert state.status == Status.CRITICAL


# --- traceability and freshness -------------------------------------------------------


def test_reasons_point_to_their_source_events():
    alert = event(0, T.TEMPERATURE_ALERT, temperature_c=96, severity=4)
    later_ok = event(5, T.NOMINAL, Vendor.THERMEXWATCH, power_kw=36, severity=0)

    state = derive_state(machine(), [alert, later_ok])

    refs = {ref["source_record_id"] for r in state.reasons for ref in r.to_dict()["source_event_refs"]}
    assert refs == {alert.source_record_id}
    assert state.latest_relevant_event_time == at(0)
    assert state.latest_event_time == at(5)
    assert state.event_count == 2


def test_freshness_uses_the_injected_clock():
    stale_after = timedelta(minutes=60)

    assert freshness(at(0), now=at(60), stale_after=stale_after) == Freshness.FRESH
    assert freshness(at(0), now=at(61), stale_after=stale_after) == Freshness.STALE
    assert freshness(None, now=at(0), stale_after=stale_after) == Freshness.STALE
