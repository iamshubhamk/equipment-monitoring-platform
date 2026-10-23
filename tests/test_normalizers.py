"""Normalizers are pure functions, so these tests need no database."""

import copy
import csv
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.models import Vendor
from app.normalization import NORMALIZERS
from app.normalization.canonical import EventType, MachineRef, NormalizationContext
from app.vendors import VENDORS, source_record_id
from tests.conftest import DATA_DIR
from tests.samples import (
    ALL_SAMPLE_BATCHES,
    SEED_FILES,
    load_sample,
    load_seed_csv,
)


def _context(tz: str = "UTC") -> NormalizationContext:
    with (DATA_DIR / "asset_reference.csv").open(newline="", encoding="utf-8") as f:
        machines = {row["machine_id"]: MachineRef(row["plant_id"], row["line_id"]) for row in csv.DictReader(f)}
    return NormalizationContext(machines=machines, maintaflow_tz=ZoneInfo(tz))


CTX = _context()


def normalize(vendor: Vendor, record, envelope=None, ctx=CTX):
    return NORMALIZERS[vendor](record, envelope or {}, ctx)


def sample_record(sample: str, vendor: Vendor, record_id: str) -> tuple[dict, dict]:
    """(record, envelope) for one record of a sample file."""
    for name, v, batch in ALL_SAMPLE_BATCHES:
        if name == sample and v == vendor:
            for record in batch[VENDORS[v].records_key]:
                if source_record_id(VENDORS[v], record) == record_id:
                    return record, batch
    raise KeyError(record_id)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


# --- every record in every sample file ------------------------------------------------

EXPECTED_REJECTIONS = {
    ("malformed.json", "PF-1201"): ["MISSING_MACHINE_ID"],  # machine_id ""
    ("malformed.json", "PF-1202"): ["INVALID_EVENT_TIME"],  # "18-04-2026 08:28:20"
    ("malformed.json", "TW-9911"): ["MISSING_MACHINE_ID", "INVALID_EVENT_TIME"],  # null asset, "bad-ts"
    ("retry_cases.json", "PF-1502"): ["INVALID_EVENT_TIME"],  # "bad-time"
    ("retry_cases.json", "MF-902"): ["MISSING_MACHINE_ID"],  # machine_ref ""
}
EXPECTED_FLAGS = {
    ("malformed.json", "TW-9912"): ["LINE_MISMATCH", "UNKNOWN_EVENT_TYPE"],  # line "ZZ", UNKNOWN_ALERT
}
ALL_SAMPLE_RECORDS = [
    (name, vendor, record, batch)
    for name, vendor, batch in ALL_SAMPLE_BATCHES
    for record in batch[VENDORS[vendor].records_key]
]


@pytest.mark.parametrize(
    ("sample", "vendor", "record", "envelope"),
    ALL_SAMPLE_RECORDS,
    ids=[f"{n}:{source_record_id(VENDORS[v], r)}" for n, v, r, _ in ALL_SAMPLE_RECORDS],
)
def test_every_sample_record_has_the_expected_outcome(sample, vendor, record, envelope):
    key = (sample, source_record_id(VENDORS[vendor], record))

    result = normalize(vendor, record, envelope)

    if key in EXPECTED_REJECTIONS:
        assert not result.accepted
        assert result.reject_reasons == EXPECTED_REJECTIONS[key]
    else:
        assert result.accepted, result.reject_reasons
        assert result.event.quality_flags == EXPECTED_FLAGS.get(key, [])
        assert result.event.event_time.tzinfo == UTC


@pytest.mark.parametrize(
    ("csv_name", "vendor", "row"),
    [(name, vendor, row) for name, vendor in SEED_FILES.items() for row in load_seed_csv(name)],
)
def test_every_seed_csv_row_is_accepted_without_flags(csv_name, vendor, row):
    # The CSVs reuse the JSON normalizers through field aliases.
    result = normalize(vendor, row)

    assert result.accepted, result.reject_reasons
    assert result.event.quality_flags == []


# --- PulseForge -----------------------------------------------------------------------


def test_pulseforge_sample_is_mapped_to_canonical_fields():
    record, envelope = sample_record("pulseforge_sample.json", Vendor.PULSEFORGE, "PF-1001")

    event = normalize(Vendor.PULSEFORGE, record, envelope).event

    assert (event.machine_id, event.plant_id, event.line_id) == ("EQ-001", "PLANT_01", "LINE-A")
    assert event.event_time == utc(2026, 4, 18, 7, 59, 12)
    assert event.event_type == EventType.VIBRATION_ALERT
    assert event.severity == 3
    assert (event.vibration_mm_s, event.temperature_c) == (11.8, 83.2)
    assert (event.sensor_health, event.vendor_confidence) == (0.91, 0.87)
    assert event.machine_state == "running"
    assert event.attributes["vendor_event_type"] == "HIGH_VIBRATION"


@pytest.mark.parametrize(("vendor_severity", "severity"), [("low", 1), ("medium", 2), ("high", 3), ("critical", 4)])
def test_pulseforge_severity_maps_to_0_4_scale(vendor_severity, severity):
    record, envelope = sample_record("pulseforge_sample.json", Vendor.PULSEFORGE, "PF-1001")

    event = normalize(Vendor.PULSEFORGE, {**record, "severity": vendor_severity}, envelope).event

    assert event.severity == severity


def test_bad_optional_values_become_null_with_flags():
    # PF-1202 from malformed.json, with a valid time so the rest of it can be checked.
    record, envelope = sample_record("malformed.json", Vendor.PULSEFORGE, "PF-1202")
    record = {**record, "event_time": "2026-04-18T08:28:20Z"}

    event = normalize(Vendor.PULSEFORGE, record, envelope).event

    assert event.severity is None  # "urgent"
    assert event.attributes["vendor_severity"] == "urgent"
    assert event.temperature_c is None  # -999
    assert event.sensor_health is None  # "bad"
    assert event.vendor_confidence is None  # 1.2
    assert event.vibration_mm_s == 6.0
    assert event.quality_flags == [
        "UNKNOWN_SEVERITY",
        "TEMPERATURE_C_OUT_OF_RANGE",
        "INVALID_SENSOR_HEALTH",
        "VENDOR_CONFIDENCE_OUT_OF_RANGE",
    ]


def test_timestamp_without_timezone_is_rejected():
    record, envelope = sample_record("pulseforge_sample.json", Vendor.PULSEFORGE, "PF-1001")

    result = normalize(Vendor.PULSEFORGE, {**record, "event_time": "2026-04-18T07:59:12"}, envelope)

    assert result.reject_reasons == ["INVALID_EVENT_TIME"]


# --- ThermexWatch ---------------------------------------------------------------------


def test_thermexwatch_reading_is_converted_to_utc_and_celsius():
    record, envelope = sample_record("thermexwatch_sample.json", Vendor.THERMEXWATCH, "TW-8801")

    event = normalize(Vendor.THERMEXWATCH, record, envelope).event

    assert event.event_time == utc(2026, 4, 18, 7, 59, 12)  # same instant as PF-1001
    assert event.line_id == "LINE-A"  # vendor sends "A"
    assert event.temperature_c == 82.89  # 181.2 F
    assert event.event_type == EventType.VIBRATION_ALERT
    assert event.severity == 3  # level 4
    assert event.vibration_g == 0.81
    assert event.vibration_mm_s is None  # g is not converted to mm/s
    assert (event.power_kw, event.machine_active, event.signal_quality) == (37.8, True, "good")


@pytest.mark.parametrize(("level", "severity"), [(1, 0), (2, 1), (3, 2), (4, 3), (5, 4), ("4", 3)])
def test_thermexwatch_level_1_5_maps_to_severity_0_4(level, severity):
    record, envelope = sample_record("thermexwatch_sample.json", Vendor.THERMEXWATCH, "TW-8801")

    event = normalize(Vendor.THERMEXWATCH, {**record, "level": level}, envelope).event

    assert event.severity == severity


@pytest.mark.parametrize(("level", "flag"), [("four", "INVALID_LEVEL"), (7, "LEVEL_OUT_OF_RANGE"), (3.5, "INVALID_LEVEL")])
def test_thermexwatch_bad_level_is_null_with_flag(level, flag):
    record, envelope = sample_record("thermexwatch_sample.json", Vendor.THERMEXWATCH, "TW-8801")

    event = normalize(Vendor.THERMEXWATCH, {**record, "level": level}, envelope).event

    assert event.severity is None
    assert event.quality_flags == [flag]


def test_unknown_alert_code_is_kept_and_flagged():
    record, envelope = sample_record("malformed.json", Vendor.THERMEXWATCH, "TW-9912")

    event = normalize(Vendor.THERMEXWATCH, record, envelope).event

    assert event.event_type == EventType.UNKNOWN
    assert event.attributes["vendor_event_type"] == "UNKNOWN_ALERT"
    assert event.severity == 3  # the level is still usable
    assert event.line_id == "LINE-E"  # from asset_reference, not "ZZ"
    assert event.attributes["reported_line_id"] == "ZZ"


def test_conflicting_update_critical_reading():
    record, envelope = sample_record("conflicting_updates.json", Vendor.THERMEXWATCH, "TW-9921")

    event = normalize(Vendor.THERMEXWATCH, record, envelope).event

    assert event.event_time == utc(2026, 4, 18, 8, 29, 10)
    assert (event.severity, event.temperature_c) == (4, 93.89)


# --- MaintaFlow -----------------------------------------------------------------------


def test_maintaflow_inspection_is_mapped():
    record, envelope = sample_record("maintaflow_sample.json", Vendor.MAINTAFLOW, "MF-501")

    event = normalize(Vendor.MAINTAFLOW, record, envelope).event

    assert event.event_time == utc(2026, 4, 18, 8, 14, 5)
    assert event.event_type == EventType.INSPECTION
    assert event.inspection_result == "minor_defect_found"
    assert event.maintenance_status == "not_due"
    assert event.manual_confidence == 0.6  # "medium"
    assert event.severity is None and event.vendor_confidence is None
    assert event.attributes["days_since_last_service"] == 18
    assert event.attributes["note"] == "Belt wear visible; machine still operational"


def test_maintaflow_short_line_and_unknown_status():
    record, envelope = sample_record("maintaflow_sample.json", Vendor.MAINTAFLOW, "MF-502")

    event = normalize(Vendor.MAINTAFLOW, record, envelope).event

    assert event.line_id == "LINE-C"  # vendor sends "C"
    assert event.event_type == EventType.OPERATOR_NOTE
    assert event.maintenance_status is None  # "unknown" means no information
    assert event.manual_confidence == 0.3
    assert event.quality_flags == []


def test_maintaflow_completed_maintenance():
    record, envelope = sample_record("out_of_order.json", Vendor.MAINTAFLOW, "MF-701")

    event = normalize(Vendor.MAINTAFLOW, record, envelope).event

    assert (event.event_type, event.maintenance_status) == (EventType.MAINTENANCE_UPDATE, "completed")
    assert event.event_time == utc(2026, 4, 18, 8, 40)


def test_maintaflow_timezone_is_configurable():
    record, envelope = sample_record("maintaflow_sample.json", Vendor.MAINTAFLOW, "MF-501")

    event = normalize(Vendor.MAINTAFLOW, record, envelope, ctx=_context("Asia/Kolkata")).event

    assert event.event_time == utc(2026, 4, 18, 2, 44, 5)


# --- work orders ----------------------------------------------------------------------


def _work_order(work_order_id: str) -> dict:
    return next(r for r in load_seed_csv("maintenance_work_orders.csv") if r["work_order_id"] == work_order_id)


def test_closed_work_order_uses_close_time_and_status_in_id():
    event = normalize(Vendor.WORKORDERS, _work_order("WO-1002")).event

    assert event.source_record_id == "WO-1002:closed"
    assert event.event_time == utc(2026, 4, 18, 8, 10)
    assert (event.work_order_status, event.post_maintenance_check) == ("closed", "passed")
    assert event.attributes["opened_at"] == "2026-04-18T07:35:00+00:00"
    assert event.attributes["downtime_minutes"] == 22


def test_open_work_order_uses_open_time():
    event = normalize(Vendor.WORKORDERS, _work_order("WO-1004")).event

    assert event.source_record_id == "WO-1004:open"
    assert event.event_time == utc(2026, 4, 18, 6, 50)
    assert event.work_order_status == "open"


def test_closed_work_order_without_close_time_is_flagged():
    record = {**_work_order("WO-1002"), "closed_at": ""}

    event = normalize(Vendor.WORKORDERS, record).event

    assert event.event_time == utc(2026, 4, 18, 7, 35)
    assert event.quality_flags == ["MISSING_CLOSED_AT"]


# --- entity resolution and shape ------------------------------------------------------


def test_machine_not_in_reference_data_is_rejected():
    record, envelope = sample_record("pulseforge_sample.json", Vendor.PULSEFORGE, "PF-1001")

    result = normalize(Vendor.PULSEFORGE, {**record, "machine_id": "EQ-999"}, envelope)

    assert result.reject_reasons == ["UNKNOWN_MACHINE"]


def test_plant_disagreeing_with_reference_is_flagged():
    record, envelope = sample_record("pulseforge_sample.json", Vendor.PULSEFORGE, "PF-1001")

    event = normalize(Vendor.PULSEFORGE, record, {**envelope, "plant_id": "PLANT_02"}).event

    assert event.plant_id == "PLANT_01"
    assert event.quality_flags == ["PLANT_MISMATCH"]


@pytest.mark.parametrize("vendor", list(Vendor))
def test_non_object_record_is_rejected(vendor):
    assert normalize(vendor, "not-a-record").reject_reasons == ["INVALID_RECORD_SHAPE"]


# --- content hash (catches re-sends under a new id) -----------------------------------


def _event(sample: str, vendor: Vendor, record_id: str):
    record, envelope = sample_record(sample, vendor, record_id)
    return normalize(vendor, record, envelope).event


def test_resend_under_new_id_has_same_content_hash():
    original = _event("duplicates.json", Vendor.THERMEXWATCH, "TW-8801")
    resend = _event("duplicates.json", Vendor.THERMEXWATCH, "TW-8801-R")

    assert original.source_record_id != resend.source_record_id
    assert original.content_hash() == resend.content_hash()


def test_same_reading_from_csv_and_json_has_same_content_hash():
    from_json = _event("thermexwatch_sample.json", Vendor.THERMEXWATCH, "TW-8801")
    row = next(r for r in load_seed_csv("thermexwatch_readings.csv") if r["reading_id"] == "TW-8801")
    from_csv = normalize(Vendor.THERMEXWATCH, row).event

    assert from_csv.vendor_received_at is not None  # CSV has received_at_ms; JSON doesn't
    assert from_csv.content_hash() == from_json.content_hash()


def test_different_readings_have_different_content_hashes():
    record, envelope = sample_record("duplicates.json", Vendor.THERMEXWATCH, "TW-8801")
    changed = copy.deepcopy(record)
    changed["temperature_f"] = 182.0

    a = normalize(Vendor.THERMEXWATCH, record, envelope).event
    b = normalize(Vendor.THERMEXWATCH, changed, envelope).event

    assert a.content_hash() != b.content_hash()


def test_parametrized_tests_cover_all_provided_records():
    # Guards against the parametrized tests above silently collecting nothing.
    assert len(load_sample("happy_path.json")) == 3
    assert len(ALL_SAMPLE_RECORDS) == 30  # every record in all 9 sample files
    assert sum(len(load_seed_csv(name)) for name in SEED_FILES) == 25  # every seed CSV row
