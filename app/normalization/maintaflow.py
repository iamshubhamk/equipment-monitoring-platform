"""MaintaFlow: manual inspection/maintenance records, zone-less local timestamps.

Timestamps like '2026/04/18 08:14:05' carry no timezone; they are read in the configured
MAINTAFLOW_TIMEZONE (default UTC). The qualitative manual_confidence goes into its own
field and is never mixed with the sensor vendors' numeric confidence.
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
from app.normalization.common import RecordReader, local_time_parser

TIME_FORMAT = "%Y/%m/%d %H:%M:%S"
EVENT_TYPES = {
    "inspection": EventType.INSPECTION,
    "maintenance_update": EventType.MAINTENANCE_UPDATE,
    "operator_note": EventType.OPERATOR_NOTE,
    "calibration": EventType.CALIBRATION,
}
INSPECTION_RESULTS = {
    v: v
    for v in (
        "passed",
        "passed_with_observation",
        "minor_defect_found",
        "major_defect_found",
        "calibration_pending",
        "failed",
    )
}
# "unknown" is a known value meaning "no information", so it becomes null without a flag.
MAINTENANCE_STATUSES = {
    **{v: v for v in ("not_due", "due_soon", "overdue", "in_progress", "completed")},
    "unknown": None,
}
MANUAL_CONFIDENCE = {"high": 0.9, "medium": 0.6, "low": 0.3}


def normalize(record: Any, envelope: dict[str, Any], ctx: NormalizationContext) -> NormalizationResult:
    if not isinstance(record, dict):
        return NormalizationResult(reject_reasons=[RejectReason.INVALID_RECORD_SHAPE])
    r = RecordReader(record)

    record_id = r.record_id("record_id")
    machine = r.machine(ctx, "machine_ref")
    event_time = r.event_time(local_time_parser(TIME_FORMAT, ctx.maintaflow_tz), "recorded_at")
    if r.reject_reasons:
        return NormalizationResult(reject_reasons=r.reject_reasons)

    machine_id, ref = machine
    plant_id, line_id = r.location(ref, r.get("factory_id") or envelope.get("factory_id"), r.get("line_ref"))

    vendor_type = r.get("record_type")
    if vendor_type is None:
        r.flag("MISSING_EVENT_TYPE")
    r.attributes["vendor_event_type"] = vendor_type
    confidence_label = r.get("manual_confidence")
    r.attributes["manual_confidence_label"] = confidence_label
    days = r.number("DAYS_SINCE_LAST_SERVICE", "days_since_last_service", lo=0, hi=3650)
    if days is not None:
        r.attributes["days_since_last_service"] = days
    if note := r.get("technician_note"):
        r.attributes["note"] = note

    return NormalizationResult(
        event=NormalizedEvent(
            vendor=Vendor.MAINTAFLOW,
            source_record_id=record_id,
            machine_id=machine_id,
            plant_id=plant_id,
            line_id=line_id,
            event_time=event_time,
            event_type=r.code("EVENT_TYPE", vendor_type, EVENT_TYPES) or EventType.UNKNOWN,
            inspection_result=r.code(
                "INSPECTION_RESULT", r.get("inspection_result"), INSPECTION_RESULTS
            ),
            maintenance_status=r.code(
                "MAINTENANCE_STATUS", r.get("maintenance_status"), MAINTENANCE_STATUSES
            ),
            manual_confidence=r.code("MANUAL_CONFIDENCE", confidence_label, MANUAL_CONFIDENCE),
            attributes=r.attributes,
            quality_flags=r.flags,
        )
    )
