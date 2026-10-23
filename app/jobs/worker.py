"""Background worker: python -m app.jobs.worker

Each job runs in two transactions:
1. claim: mark it running (committed at once, so other workers skip it);
2. work: run the handler and mark the job succeeded, committed together.
If step 2 fails, it is rolled back and the failure is recorded in a third transaction.
If the process dies during step 2, the job stays 'running' until recover_stale() re-queues
it, so work is at-least-once; handlers are written to be safe to re-run.
"""

import logging
import os
import signal
import socket
import time
from collections.abc import Mapping
from datetime import timedelta

from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock, utc_now
from app.config import Settings, get_settings
from app.jobs.errors import PermanentError
from app.jobs.handlers import HANDLERS, Handler
from app.jobs.queue import claim_next, mark_failed, mark_succeeded, recover_stale
from app.models import Job, JobStatus

log = logging.getLogger("worker")


def run_once(
    make_session: sessionmaker[Session],
    worker_id: str,
    *,
    clock: Clock = utc_now,
    handlers: Mapping[str, Handler] = HANDLERS,
    settings: Settings | None = None,
) -> bool:
    """Process at most one job. Returns False if no job was due."""
    settings = settings or get_settings()

    with make_session() as session:
        job = claim_next(session, worker_id)
        if job is None:
            return False
        session.commit()
        job_id, kind, payload, attempt = job.id, job.kind, dict(job.payload), job.attempts

    handler = handlers.get(kind)
    with make_session() as session:
        try:
            if handler is None:
                raise PermanentError(f"no handler for job kind {kind!r}")
            handler.run(session, payload, clock())
            mark_succeeded(session.get(Job, job_id))
            session.commit()
            log.info("job %s (%s) succeeded on attempt %s", job_id, kind, attempt)
        except Exception as exc:
            session.rollback()
            error = f"{type(exc).__name__}: {exc}"
            job = session.get(Job, job_id)
            mark_failed(
                job,
                error,
                permanent=isinstance(exc, PermanentError),
                backoff_base_seconds=settings.job_backoff_base_seconds,
                backoff_max_seconds=settings.job_backoff_max_seconds,
            )
            dead = job.status == JobStatus.DEAD
            if handler is not None and handler.on_failure is not None:
                handler.on_failure(session, payload, error, dead, clock())
            session.commit()
            if dead:
                log.error("job %s (%s) dead-lettered after attempt %s: %s", job_id, kind, attempt, error)
            else:
                log.warning("job %s (%s) attempt %s failed, retry at %s: %s", job_id, kind, attempt, job.run_after, error)
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from app.db import SessionLocal

    settings = get_settings()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    stopping = False

    def stop(signum, _frame):
        nonlocal stopping
        log.info("received signal %s, finishing current job then stopping", signum)
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    log.info("worker %s started", worker_id)
    lock_timeout = timedelta(seconds=settings.job_lock_timeout_seconds)
    last_recovery = 0.0
    while not stopping:
        if time.monotonic() - last_recovery > settings.job_lock_timeout_seconds / 2:
            with SessionLocal() as session:
                if recovered := recover_stale(session, lock_timeout):
                    log.warning("recovered %s stale jobs", recovered)
                session.commit()
            last_recovery = time.monotonic()
        try:
            processed = run_once(SessionLocal, worker_id, settings=settings)
        except Exception:
            # E.g. the database is briefly unreachable: keep the worker alive and retry.
            log.exception("worker loop error")
            processed = False
        if not processed:
            time.sleep(settings.worker_poll_interval_seconds)
    log.info("worker %s stopped", worker_id)


if __name__ == "__main__":
    main()
