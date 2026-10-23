from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_ops_key
from app.api.ingestions import job_out
from app.db import get_session
from app.jobs.errors import PermanentError
from app.jobs.handlers import ReplayConflict, replay_dead_job
from app.models import Job, JobStatus
from app.schemas import JobOut

router = APIRouter(prefix="/v1/dead-letters", tags=["dead letters"])


@router.get("", response_model=list[JobOut])
def list_dead_letters(
    session: Annotated[Session, Depends(get_session)],
    kind: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[JobOut]:
    """Jobs that failed permanently or ran out of attempts, newest first, with their last error."""
    query = select(Job).where(Job.status == JobStatus.DEAD)
    if kind:
        query = query.where(Job.kind == kind)
    return [job_out(j) for j in session.scalars(query.order_by(Job.finished_at.desc(), Job.id.desc()).limit(limit))]


@router.post(
    "/{job_id}/replay",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobOut,
    dependencies=[Depends(require_ops_key)],
    responses={
        401: {"description": "Missing or invalid operations key"},
        404: {"description": "Unknown job"},
        409: {"description": "Job is not dead, or an equivalent job is already queued"},
    },
)
def replay_dead_letter(job_id: int, session: Annotated[Session, Depends(get_session)]) -> JobOut:
    """Re-queue a dead job with fresh attempts (after fixing whatever made it fail).
    Requires the operations X-API-Key."""
    job = session.get(Job, job_id, with_for_update=True)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"job {job_id} not found")
    try:
        replay_dead_job(session, job)
    except ReplayConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except PermanentError as exc:  # e.g. the batch it refers to no longer exists
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    session.commit()
    session.refresh(job)
    return job_out(job)
