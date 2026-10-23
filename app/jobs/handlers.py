"""Job handlers. Each runs inside one transaction that the worker commits together with
marking the job succeeded, so a failed attempt leaves no partial results behind."""

import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.derivation.service import recompute_machine
from app.jobs.errors import PermanentError
from app.jobs.queue import NORMALIZE_BATCH, RECOMPUTE_MACHINE, enqueue_unique
from app.models import BatchStatus, Event, IngestionBatch, Machine, RawRecord, RecordStatus
from app.normalization import NORMALIZERS
from app.normalization.canonical import MachineRef, NormalizationContext, NormalizedEvent

# Reason codes on raw records that were valid but already known.
DUPLICATE_RECORD_ID = "DUPLICATE_RECORD_ID"  # same vendor + record id seen before
CONTENT_DIFFERS = "CONTENT_DIFFERS"  # ...and this copy has different content
DUPLICATE_CONTENT = "DUPLICATE_CONTENT"  # same vendor + same content under a new id


@dataclass(frozen=True)
class Handler:
    run: Callable[[Session, dict[str, Any], datetime], None]
    # Called after a failed attempt, in its own transaction. dead=True: no more retries.
    on_failure: Callable[[Session, dict[str, Any], str, bool, datetime], None] | None = None


def load_context(session: Session) -> NormalizationContext:
    machines = {
        m.machine_id: MachineRef(m.plant_id, m.line_id) for m in session.scalars(select(Machine))
    }
    return NormalizationContext(machines, ZoneInfo(get_settings().maintaflow_timezone))


def _find_duplicate(session: Session, event: NormalizedEvent, content_hash: str) -> tuple[Event, list[str]] | None:
    """Duplicates are only ever looked for within the same vendor."""
    same_id = session.scalar(
        select(Event).where(Event.vendor == event.vendor, Event.source_record_id == event.source_record_id)
    )
    if same_id is not None:
        # First version wins; a changed re-send is kept as raw data and flagged, not applied.
        reasons = [DUPLICATE_RECORD_ID]
        if same_id.content_hash != content_hash:
            reasons.append(CONTENT_DIFFERS)
        return same_id, reasons
    same_content = session.scalar(
        select(Event)
        .where(Event.vendor == event.vendor, Event.content_hash == content_hash)
        .order_by(Event.id)
        .limit(1)
    )
    if same_content is not None:
        return same_content, [DUPLICATE_CONTENT]
    return None


def _batch(session: Session, payload: dict[str, Any]) -> IngestionBatch:
    try:
        batch_id = uuid.UUID(str(payload["ingestion_id"]))
    except (KeyError, ValueError) as exc:
        raise PermanentError(f"invalid job payload: {payload}") from exc
    batch = session.get(IngestionBatch, batch_id, with_for_update=True)
    if batch is None:
        raise PermanentError(f"ingestion {batch_id} not found")
    return batch


def normalize_batch(session: Session, payload: dict[str, Any], now: datetime) -> None:
    batch = _batch(session, payload)
    if batch.status == BatchStatus.COMPLETED:
        return  # already done (e.g. the job was replayed); nothing to redo

    normalizer = NORMALIZERS[batch.vendor]
    ctx = load_context(session)
    records = session.scalars(
        select(RawRecord).where(RawRecord.batch_id == batch.id).order_by(RawRecord.record_index)
    ).all()

    touched: set[str] = set()
    for record in records:
        result = normalizer(record.raw, batch.raw_payload, ctx)
        if not result.accepted:
            record.status = RecordStatus.REJECTED
            record.reason_codes = result.reject_reasons
            continue

        content_hash = result.event.content_hash()
        if duplicate := _find_duplicate(session, result.event, content_hash):
            original, reasons = duplicate
            record.status = RecordStatus.DUPLICATE
            record.reason_codes = reasons
            record.event_id = original.id
            continue

        event = Event(**asdict(result.event), content_hash=content_hash, raw_record_id=record.id)
        session.add(event)
        session.flush()  # so a later record in this batch sees it as a duplicate
        record.status = RecordStatus.ACCEPTED
        record.reason_codes = []
        record.event_id = event.id
        touched.add(event.machine_id)

    # Only machines that actually got new events need their state recomputed.
    for machine_id in sorted(touched):
        enqueue_unique(session, RECOMPUTE_MACHINE, {"machine_id": machine_id}, f"recompute:{machine_id}")

    statuses = [r.status for r in records]
    batch.accepted_count = statuses.count(RecordStatus.ACCEPTED)
    batch.rejected_count = statuses.count(RecordStatus.REJECTED)
    batch.duplicate_count = statuses.count(RecordStatus.DUPLICATE)
    batch.status = BatchStatus.COMPLETED
    batch.processed_at = now
    batch.error = None


def normalize_batch_failed(
    session: Session, payload: dict[str, Any], error: str, dead: bool, now: datetime
) -> None:
    try:
        batch = _batch(session, payload)
    except PermanentError:
        return
    batch.error = error
    if dead:
        batch.status = BatchStatus.FAILED
        batch.processed_at = now


def recompute(session: Session, payload: dict[str, Any], now: datetime) -> None:
    machine_id = payload.get("machine_id")
    if not machine_id:
        raise PermanentError(f"invalid job payload: {payload}")
    recompute_machine(session, machine_id, now)


HANDLERS: dict[str, Handler] = {
    NORMALIZE_BATCH: Handler(run=normalize_batch, on_failure=normalize_batch_failed),
    RECOMPUTE_MACHINE: Handler(run=recompute),
}
