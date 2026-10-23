import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.jobs.queue import NORMALIZE_BATCH
from app.models import IngestionBatch, Job, JobStatus, RawRecord, RecordStatus
from app.schemas import IngestionStatus, JobOut, RawRecordList, RawRecordOut

router = APIRouter(prefix="/v1/ingestions", tags=["ingestion"])


def job_out(job: Job) -> JobOut:
    return JobOut(
        job_id=job.id,
        kind=job.kind,
        status=job.status,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        next_attempt_at=job.run_after if job.status == JobStatus.QUEUED else None,
        last_error=job.last_error,
        payload=job.payload,
        created_at=job.created_at,
        finished_at=job.finished_at,
    )


def _batch_or_404(session: Session, ingestion_id: uuid.UUID) -> IngestionBatch:
    batch = session.get(IngestionBatch, ingestion_id)
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"ingestion {ingestion_id} not found")
    return batch


@router.get("/{ingestion_id}", response_model=IngestionStatus, responses={404: {"description": "Unknown ingestion"}})
def get_ingestion(ingestion_id: uuid.UUID, session: Annotated[Session, Depends(get_session)]) -> IngestionStatus:
    """Processing status of an accepted batch: counts per outcome and its background job."""
    batch = _batch_or_404(session, ingestion_id)
    job = session.scalar(
        select(Job)
        .where(Job.kind == NORMALIZE_BATCH, Job.payload["ingestion_id"].astext == str(batch.id))
        .order_by(Job.id.desc())
        .limit(1)
    )
    pending = session.scalar(
        select(func.count()).where(RawRecord.batch_id == batch.id, RawRecord.status == RecordStatus.PENDING)
    )
    return IngestionStatus(
        ingestion_id=batch.id,
        vendor=batch.vendor,
        status=batch.status,
        received_at=batch.received_at,
        processed_at=batch.processed_at,
        record_count=batch.record_count,
        accepted_count=batch.accepted_count,
        rejected_count=batch.rejected_count,
        duplicate_count=batch.duplicate_count,
        pending_count=pending,
        error=batch.error,
        job=job_out(job) if job else None,
        records_url=f"/v1/ingestions/{batch.id}/records",
    )


@router.get("/{ingestion_id}/records", response_model=RawRecordList, responses={404: {"description": "Unknown ingestion"}})
def list_records(
    ingestion_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    record_status: Annotated[RecordStatus | None, Query(alias="status", description="e.g. rejected")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RawRecordList:
    """The batch's records with their outcome. `?status=rejected` lists what was refused and why."""
    batch = _batch_or_404(session, ingestion_id)
    conditions = [RawRecord.batch_id == batch.id]
    if record_status is not None:
        conditions.append(RawRecord.status == record_status)
    records = session.scalars(
        select(RawRecord).where(*conditions).order_by(RawRecord.record_index).limit(limit).offset(offset)
    ).all()
    return RawRecordList(
        items=[
            RawRecordOut(
                record_index=r.record_index,
                source_record_id=r.source_record_id,
                status=r.status,
                reason_codes=r.reason_codes,
                event_id=r.event_id,
                raw=r.raw,
            )
            for r in records
        ],
        total=session.scalar(select(func.count()).where(*conditions)),
    )
