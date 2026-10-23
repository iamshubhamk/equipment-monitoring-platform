"""Postgres-backed job queue."""

from typing import Any

from sqlalchemy.orm import Session

from app.models import Job

NORMALIZE_BATCH = "normalize_batch"


def enqueue(session: Session, kind: str, payload: dict[str, Any]) -> Job:
    """Add a job in the caller's transaction.

    Because the job is committed together with the data it refers to, there is no
    window where data is saved but its job is lost (or the reverse).
    """
    job = Job(kind=kind, payload=payload)
    session.add(job)
    return job
