"""Postgres-backed job queue.

Lifecycle: queued -> running -> succeeded
                             -> queued again (retry after a backoff delay)
                             -> dead (permanent error or out of attempts)

These functions change rows but don't commit; the caller owns the transaction.
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Job, JobStatus

NORMALIZE_BATCH = "normalize_batch"


def enqueue(session: Session, kind: str, payload: dict[str, Any]) -> Job:
    """Add a job in the caller's transaction.

    Because the job is committed together with the data it refers to, there is no
    window where data is saved but its job is lost (or the reverse).
    """
    job = Job(kind=kind, payload=payload)
    session.add(job)
    return job


def claim_next(session: Session, worker_id: str, now: datetime) -> Job | None:
    """Take the oldest due job.

    FOR UPDATE SKIP LOCKED lets several workers poll at once: a row another worker is
    claiming is skipped instead of waited on, so no job is handed out twice.
    """
    job = session.scalars(
        select(Job)
        .where(Job.status == JobStatus.QUEUED, Job.run_after <= now)
        .order_by(Job.run_after, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if job is not None:
        job.status = JobStatus.RUNNING
        job.attempts += 1
        job.locked_at = now
        job.locked_by = worker_id
    return job


def mark_succeeded(job: Job, now: datetime) -> None:
    job.status = JobStatus.SUCCEEDED
    job.finished_at = now
    job.locked_at = job.locked_by = None


def backoff_delay(attempts: int, base_seconds: float, max_seconds: float) -> timedelta:
    """Exponential: base, 2*base, 4*base, ... capped at max_seconds."""
    return timedelta(seconds=min(max_seconds, base_seconds * 2 ** (attempts - 1)))


def mark_failed(
    job: Job,
    error: str,
    *,
    permanent: bool,
    now: datetime,
    backoff_base_seconds: float,
    backoff_max_seconds: float,
) -> None:
    job.last_error = error
    job.locked_at = job.locked_by = None
    if permanent or job.attempts >= job.max_attempts:
        job.status = JobStatus.DEAD
        job.finished_at = now
    else:
        job.status = JobStatus.QUEUED
        job.run_after = now + backoff_delay(job.attempts, backoff_base_seconds, backoff_max_seconds)


def recover_stale(session: Session, now: datetime, lock_timeout: timedelta) -> int:
    """Return jobs whose worker died mid-run to the queue (or dead-letter them if out of
    attempts). Returns how many jobs were recovered."""
    stale = (Job.status == JobStatus.RUNNING, Job.locked_at < now - lock_timeout)
    common = {"locked_at": None, "locked_by": None, "last_error": "worker stopped before finishing"}
    dead = session.execute(
        update(Job)
        .where(*stale, Job.attempts >= Job.max_attempts)
        .values(status=JobStatus.DEAD, finished_at=now, **common)
    ).rowcount
    requeued = session.execute(
        update(Job).where(*stale).values(status=JobStatus.QUEUED, run_after=now, **common)
    ).rowcount
    return dead + requeued
