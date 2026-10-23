"""PulseForge: ISO-8601 UTC times, Celsius, mm/s vibration, low..critical severity."""

from typing import Any

from app.models import Vendor
from app.normalization.canonical import (
    EventType,
    NormalizationContext,
    NormalizationResult,
    NormalizedEvent,
    RejectReason,
)
from app.normalization.common import RecordReader, parse_iso_utc

EVENT_TYPES = {
    "high_vibration": EventType.VIBRATION_ALERT,
    "temp_spike": EventType.TEMPERATURE_ALERT,
    "power_fluctuation": EventType.POWER_ALERT,
    "sensor_health_drop": EventType.SENSOR_HEALTH_ALERT,
    "recovery_signal": EventType.RECOVERY,
}
SEVERITIES = {"low": 1, "medium": 2, "high": 3, "critical": 4}
MACHINE_STATES = {s: s for s in ("running", "idle", "stopped", "offline", "maintenance")}


def normalize(record: Any, envelope: dict[str, Any], ctx: NormalizationContext) -> NormalizationResult:
    if not isinstance(record, dict):
        return NormalizationResult(reject_reasons=[RejectReason.INVALID_RECORD_SHAPE])
    r = RecordReader(record)

    record_id = r.record_id("event_id")
    machine = r.machine(ctx, "machine_id")
    event_time = r.event_time(parse_iso_utc, "event_time")
    if r.reject_reasons:
        return NormalizationResult(reject_reasons=r.reject_reasons)

    machine_id, ref = machine
    plant_id, line_id = r.location(ref, r.get("plant_id") or envelope.get("plant_id"), r.get("line_id"))

    vendor_type = r.get("event_type")
    if vendor_type is None:
        r.flag("MISSING_EVENT_TYPE")
    r.attributes["vendor_event_type"] = vendor_type
    vendor_severity = r.get("severity")
    r.attributes["vendor_severity"] = vendor_severity

    return NormalizationResult(
        event=NormalizedEvent(
            vendor=Vendor.PULSEFORGE,
            source_record_id=record_id,
            machine_id=machine_id,
            plant_id=plant_id,
            line_id=line_id,
            event_time=event_time,
            vendor_received_at=r.optional_time("VENDOR_RECEIVED_AT", parse_iso_utc, "ingestion_time"),
            event_type=r.code("EVENT_TYPE", vendor_type, EVENT_TYPES) or EventType.UNKNOWN,
            severity=r.code("SEVERITY", vendor_severity, SEVERITIES),
            temperature_c=r.number("TEMPERATURE_C", "temperature_c", lo=-50, hi=300),
            vibration_mm_s=r.number("VIBRATION_MM_S", "vibration_mm_s", lo=0, hi=100),
            sensor_health=r.number("SENSOR_HEALTH", "sensor_health", lo=0, hi=1),
            vendor_confidence=r.number("VENDOR_CONFIDENCE", "vendor_confidence", lo=0, hi=1),
            machine_state=r.code("MACHINE_STATE", r.get("machine_state"), MACHINE_STATES),
            attributes=r.attributes,
            quality_flags=r.flags,
        )
    )
