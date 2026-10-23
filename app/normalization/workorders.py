"""Internal maintenance work orders (maintenance_work_orders.csv).

A work order changes over time (open -> closed) under the same work_order_id. Each state
is a separate event with id '<work_order_id>:<status>', so a later "closed" update is not
discarded as a duplicate of the earlier "open" one, while an identical resend still is.
The event time is when that state began: closed_at for a closed order, else opened_at.
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
from app.normalization.common import RecordReader, parse_iso_utc

STATUSES = {s: s for s in ("open", "in_progress", "closed", "cancelled")}
CHECKS = {c: c for c in ("pending", "passed", "failed")}


def normalize(record: Any, envelope: dict[str, Any], ctx: NormalizationContext) -> NormalizationResult:
    if not isinstance(record, dict):
        return NormalizationResult(reject_reasons=[RejectReason.INVALID_RECORD_SHAPE])
    r = RecordReader(record)

    work_order_id = r.record_id("work_order_id")
    machine = r.machine(ctx, "machine_id")
    opened_at = r.event_time(parse_iso_utc, "opened_at")
    if r.reject_reasons:
        return NormalizationResult(reject_reasons=r.reject_reasons)

    machine_id, ref = machine
    plant_id, line_id = r.location(ref, r.get("plant_id") or envelope.get("plant_id"), r.get("line_id"))

    status = r.code("WORK_ORDER_STATUS", r.get("status"), STATUSES)
    if r.get("status") is None:
        r.flag("MISSING_WORK_ORDER_STATUS")
    closed_at = r.optional_time("CLOSED_AT", parse_iso_utc, "closed_at")
    if status == "closed" and closed_at is None:
        r.flag("MISSING_CLOSED_AT")

    r.attributes.update(
        work_order_id=work_order_id,
        opened_at=opened_at.isoformat(),
        closed_at=closed_at.isoformat() if closed_at else None,
        maintenance_type=r.get("maintenance_type"),
        trigger_source=r.get("trigger_source"),
        downtime_minutes=r.number("DOWNTIME_MINUTES", "downtime_minutes", lo=0, hi=100_000),
        notes=r.get("notes"),
    )

    return NormalizationResult(
        event=NormalizedEvent(
            vendor=Vendor.WORKORDERS,
            source_record_id=f"{work_order_id}:{status or 'unknown'}",
            machine_id=machine_id,
            plant_id=plant_id,
            line_id=line_id,
            event_time=closed_at if status == "closed" and closed_at else opened_at,
            event_type=EventType.WORK_ORDER,
            work_order_status=status,
            post_maintenance_check=r.code(
                "POST_MAINTENANCE_CHECK", r.get("post_maintenance_check"), CHECKS
            ),
            attributes=r.attributes,
            quality_flags=r.flags,
        )
    )
