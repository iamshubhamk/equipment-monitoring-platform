"""ThermexWatch: epoch-ms UTC times, Fahrenheit, vibration in g, alert level 1-5.

The JSON API uses camelCase (readingId); the seed CSV uses snake_case (reading_id).
Vibration in g is stored as-is: converting it to mm/s needs the vibration frequency,
which the vendor doesn't send.
"""

from typing import Any

from app.models import Vendor
from app.normalization.canonical import (
    EventType,
    NormalizationContext,
    NormalizationResult,
    NormalizedEvent,
    RejectReason,
)
from app.normalization.common import RecordReader, fahrenheit_to_celsius, parse_epoch_ms

EVENT_TYPES = {
    "vib_warn": EventType.VIBRATION_ALERT,
    "temp_warn": EventType.TEMPERATURE_ALERT,
    "temp_crit": EventType.TEMPERATURE_ALERT,
    "power_drop": EventType.POWER_ALERT,
    "ok": EventType.NOMINAL,
}
SIGNAL_QUALITIES = {q: q for q in ("good", "fair", "poor")}


def _severity(r: RecordReader) -> int | None:
    """Level 1 (nominal) .. 5 (critical) maps to severity 0 .. 4."""
    raw = r.get("level")
    # As text, so JSON 4 and CSV "4" give the same content hash.
    r.attributes["vendor_level"] = None if raw is None else str(raw)
    level = r.number("LEVEL", "level", lo=1, hi=5)
    if level is None:
        return None
    if not level.is_integer():
        r.flag("INVALID_LEVEL")
        return None
    return int(level) - 1


def normalize(record: Any, envelope: dict[str, Any], ctx: NormalizationContext) -> NormalizationResult:
    if not isinstance(record, dict):
        return NormalizationResult(reject_reasons=[RejectReason.INVALID_RECORD_SHAPE])
    r = RecordReader(record)

    record_id = r.record_id("readingId", "reading_id")
    machine = r.machine(ctx, "assetCode", "asset_code")
    event_time = r.event_time(parse_epoch_ms, "timestampMs", "timestamp_ms")
    if r.reject_reasons:
        return NormalizationResult(reject_reasons=r.reject_reasons)

    machine_id, ref = machine
    plant_id, line_id = r.location(
        ref,
        r.get("site_code", "siteCode") or envelope.get("site_code"),
        r.get("productionLine", "production_line"),
    )

    vendor_type = r.get("alertCode", "alert_code")
    if vendor_type is None:
        r.flag("MISSING_EVENT_TYPE")
    r.attributes["vendor_event_type"] = vendor_type

    temperature_f = r.number("TEMPERATURE_F", "temperature_f", lo=-58, hi=572)

    return NormalizationResult(
        event=NormalizedEvent(
            vendor=Vendor.THERMEXWATCH,
            source_record_id=record_id,
            machine_id=machine_id,
            plant_id=plant_id,
            line_id=line_id,
            event_time=event_time,
            vendor_received_at=r.optional_time(
                "VENDOR_RECEIVED_AT", parse_epoch_ms, "receivedAtMs", "received_at_ms"
            ),
            event_type=r.code("EVENT_TYPE", vendor_type, EVENT_TYPES) or EventType.UNKNOWN,
            severity=_severity(r),
            temperature_c=fahrenheit_to_celsius(temperature_f) if temperature_f is not None else None,
            vibration_g=r.number("VIBRATION_G", "vibration_g", lo=0, hi=50),
            power_kw=r.number("POWER_KW", "power_kw", lo=0, hi=10_000),
            machine_active=r.boolean("IS_ACTIVE", "is_active", "isActive"),
            signal_quality=r.code(
                "SIGNAL_QUALITY", r.get("signal_quality", "signalQuality"), SIGNAL_QUALITIES
            ),
            attributes=r.attributes,
            quality_flags=r.flags,
        )
    )
