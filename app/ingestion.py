"""Accepting vendor batches.

The API only checks the envelope (is there a list of records?) and stores everything.
Each record is validated later by the worker, so one bad record never rejects the batch.
"""

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.jobs.queue import NORMALIZE_BATCH, enqueue
from app.models import IngestionBatch, RawRecord
from app.vendors import VendorSpec, source_record_id


class InvalidEnvelope(Exception):
    """The batch itself is unusable (as opposed to individual bad records)."""


class BatchTooLarge(Exception):
    pass


class IdempotencyConflict(Exception):
    """The Idempotency-Key was already used for a different payload."""


def payload_sha256(payload: dict[str, Any]) -> str:
    # Canonical JSON, so key order and whitespace don't change the hash.
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def extract_records(spec: VendorSpec, payload: dict[str, Any], max_records: int) -> list[Any]:
    sender = payload.get(spec.name_field) if spec.name_field else None
    if sender is not None and str(sender).strip().lower() != spec.display_name.lower():
        raise InvalidEnvelope(
            f"envelope field '{spec.name_field}' is '{sender}', expected '{spec.display_name}'"
        )
    records = payload.get(spec.records_key)
    if not isinstance(records, list):
        raise InvalidEnvelope(f"expected a list of records under '{spec.records_key}'")
    if not records:
        raise InvalidEnvelope(f"'{spec.records_key}' is empty")
    if len(records) > max_records:
        raise BatchTooLarge(f"{len(records)} records; the limit is {max_records} per batch")
    return records


def _find_batch(session: Session, spec: VendorSpec, key: str) -> IngestionBatch | None:
    return session.scalar(
        select(IngestionBatch).where(
            IngestionBatch.vendor == spec.vendor, IngestionBatch.idempotency_key == key
        )
    )


def _replay(batch: IngestionBatch, payload_hash: str) -> tuple[IngestionBatch, bool]:
    if batch.payload_sha256 != payload_hash:
        raise IdempotencyConflict(
            f"Idempotency-Key already used for a different payload (ingestion {batch.id})"
        )
    return batch, True


def accept_batch(
    session: Session,
    spec: VendorSpec,
    payload: dict[str, Any],
    idempotency_key: str | None,
    max_records: int,
) -> tuple[IngestionBatch, bool]:
    """Store a batch and its records and enqueue normalization, all in one transaction.

    Returns (batch, replayed). replayed=True means this exact request was seen before
    and nothing new was stored.
    """
    records = extract_records(spec, payload, max_records)
    payload_hash = payload_sha256(payload)
    key = idempotency_key or payload_hash

    if existing := _find_batch(session, spec, key):
        return _replay(existing, payload_hash)

    batch = IngestionBatch(
        vendor=spec.vendor,
        idempotency_key=key,
        payload_sha256=payload_hash,
        raw_payload=payload,
        record_count=len(records),
    )
    session.add(batch)
    try:
        session.flush()
    except IntegrityError:
        # A concurrent request with the same key committed first; answer as a replay.
        session.rollback()
        return _replay(_find_batch(session, spec, key), payload_hash)

    session.add_all(
        RawRecord(
            batch_id=batch.id,
            record_index=index,
            source_record_id=source_record_id(spec, record),
            raw=record,
        )
        for index, record in enumerate(records)
    )
    enqueue(session, NORMALIZE_BATCH, {"ingestion_id": str(batch.id)})
    session.commit()
    return batch, False
