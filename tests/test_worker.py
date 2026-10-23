from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app.config import get_settings
from app.jobs.errors import PermanentError, TransientError
from app.jobs.handlers import HANDLERS, Handler, normalize_batch, normalize_batch_failed
from app.jobs.queue import NORMALIZE_BATCH, claim_next, enqueue, recover_stale
from app.jobs.worker import run_once
from app.models import (
    BatchStatus,
    Event,
    IngestionBatch,
    Job,
    JobStatus,
    RawRecord,
    RecordStatus,
    Vendor,
)
from tests.samples import ALL_SAMPLE_BATCHES, load_sample

PF_SAMPLE = load_sample("pulseforge_sample.json")


def ingest(client, vendor: Vendor, payload) -> str:
    headers = {"X-API-Key": get_settings().api_key_for(vendor)}
    response = client.post(f"/v1/ingest/{vendor.value}", json=payload, headers=headers)
    assert response.status_code == 202, response.text
    return response.json()["ingestion_id"]


def drain(make_session, **kwargs) -> int:
    """Run the worker until no job is due. Returns how many jobs it processed."""
    processed = 0
    while run_once(make_session, "test-worker", **kwargs):
        processed += 1
    return processed


def make_due(make_session) -> None:
    """Skip the backoff wait: queue timing uses the database clock, so move run_after."""
    with make_session() as session:
        session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(run_after=func.now()))
        session.commit()


def normalize_job(session) -> Job:
    return session.scalar(select(Job).where(Job.kind == NORMALIZE_BATCH))


def records_by_id(session, ingestion_id) -> dict[str, RawRecord]:
    rows = session.scalars(select(RawRecord).where(RawRecord.batch_id == ingestion_id)).all()
    return {r.source_record_id: r for r in rows}


# --- processing the provided samples --------------------------------------------------


@pytest.mark.parametrize(
    ("sample", "vendor", "batch"),
    ALL_SAMPLE_BATCHES,
    ids=[f"{name}:{vendor.value}" for name, vendor, _ in ALL_SAMPLE_BATCHES],
)
def test_every_sample_batch_is_fully_processed(client, make_session, reference_data, sample, vendor, batch):
    ingestion_id = ingest(client, vendor, batch)

    drain(make_session)

    with make_session() as session:
        stored = session.get(IngestionBatch, ingestion_id)
        assert stored.status == BatchStatus.COMPLETED
        assert stored.processed_at is not None
        assert stored.accepted_count + stored.rejected_count + stored.duplicate_count == stored.record_count
        statuses = session.scalars(select(RawRecord.status).where(RawRecord.batch_id == ingestion_id)).all()
        assert RecordStatus.PENDING not in statuses
        assert session.scalar(select(func.count()).select_from(Event)) == stored.accepted_count
        assert set(session.scalars(select(Job.status))) == {JobStatus.SUCCEEDED}


def test_bad_records_are_rejected_without_affecting_the_rest(client, make_session, reference_data):
    ingestion_id = ingest(client, Vendor.THERMEXWATCH, load_sample("malformed.json")["thermexwatch"])

    drain(make_session)

    with make_session() as session:
        batch = session.get(IngestionBatch, ingestion_id)
        assert (batch.accepted_count, batch.rejected_count) == (1, 1)
        records = records_by_id(session, ingestion_id)
        assert records["TW-9911"].status == RecordStatus.REJECTED
        assert records["TW-9911"].reason_codes == ["MISSING_MACHINE_ID", "INVALID_EVENT_TIME"]
        accepted = session.get(Event, records["TW-9912"].event_id)
        assert accepted.quality_flags == ["LINE_MISMATCH", "UNKNOWN_EVENT_TYPE"]
        assert accepted.raw_record_id == records["TW-9912"].id  # traceable back to the raw record


def test_duplicate_record_id_within_a_batch(client, make_session, reference_data):
    ingestion_id = ingest(client, Vendor.PULSEFORGE, load_sample("duplicates.json")["pulseforge"])

    drain(make_session)

    with make_session() as session:
        first, second = session.scalars(
            select(RawRecord).where(RawRecord.batch_id == ingestion_id).order_by(RawRecord.record_index)
        ).all()
        assert first.status == RecordStatus.ACCEPTED
        assert second.status == RecordStatus.DUPLICATE
        assert second.reason_codes == ["DUPLICATE_RECORD_ID"]
        assert second.event_id == first.event_id
        assert session.scalar(select(func.count()).select_from(Event)) == 1


def test_same_content_under_a_new_id_is_a_duplicate(client, make_session, reference_data):
    ingestion_id = ingest(client, Vendor.THERMEXWATCH, load_sample("duplicates.json")["thermexwatch"])

    drain(make_session)

    with make_session() as session:
        records = records_by_id(session, ingestion_id)
        assert records["TW-8801"].status == RecordStatus.ACCEPTED
        assert records["TW-8801-R"].status == RecordStatus.DUPLICATE
        assert records["TW-8801-R"].reason_codes == ["DUPLICATE_CONTENT"]
        assert records["TW-8801-R"].event_id == records["TW-8801"].event_id


def test_duplicate_across_batches(client, make_session, reference_data):
    ingest(client, Vendor.PULSEFORGE, PF_SAMPLE)
    later = ingest(client, Vendor.PULSEFORGE, load_sample("duplicates.json")["pulseforge"])

    drain(make_session)

    with make_session() as session:
        batch = session.get(IngestionBatch, later)
        assert (batch.accepted_count, batch.duplicate_count) == (0, 2)
        assert session.scalar(select(func.count()).select_from(Event)) == 2  # PF-1001, PF-1002


def test_changed_resend_under_same_id_keeps_the_first_version(client, make_session, reference_data):
    changed = {**PF_SAMPLE, "events": [{**PF_SAMPLE["events"][0], "severity": "critical"}]}
    ingest(client, Vendor.PULSEFORGE, PF_SAMPLE)
    later = ingest(client, Vendor.PULSEFORGE, changed)

    drain(make_session)

    with make_session() as session:
        record = records_by_id(session, later)["PF-1001"]
        assert record.status == RecordStatus.DUPLICATE
        assert record.reason_codes == ["DUPLICATE_RECORD_ID", "CONTENT_DIFFERS"]
        assert session.get(Event, record.event_id).severity == 3  # original "high" kept


def test_never_deduplicated_across_vendors(client, make_session, reference_data):
    # PF-1001 and TW-8801 describe the same moment on EQ-001 but come from different vendors.
    ingest(client, Vendor.PULSEFORGE, PF_SAMPLE)
    ingest(client, Vendor.THERMEXWATCH, load_sample("thermexwatch_sample.json"))

    drain(make_session)

    with make_session() as session:
        vendors = session.scalars(
            select(Event.vendor).where(Event.machine_id == "EQ-001").order_by(Event.vendor)
        ).all()
        assert vendors == [Vendor.PULSEFORGE, Vendor.THERMEXWATCH]


def test_rerunning_a_completed_batch_changes_nothing(client, make_session, reference_data):
    ingestion_id = ingest(client, Vendor.PULSEFORGE, PF_SAMPLE)
    drain(make_session)

    with make_session() as session:
        normalize_batch(session, {"ingestion_id": ingestion_id}, datetime.now(UTC))
        session.commit()
        assert session.scalar(select(func.count()).select_from(Event)) == 2


# --- retries, backoff and dead-lettering ----------------------------------------------


def flaky_handlers(failures: list[Exception]) -> dict[str, Handler]:
    """normalize_batch that raises the given errors first, then works normally."""

    def run(session, payload, now):
        if failures:
            raise failures.pop(0)
        normalize_batch(session, payload, now)

    return {**HANDLERS, NORMALIZE_BATCH: Handler(run=run, on_failure=normalize_batch_failed)}


def test_transient_failure_is_retried_with_exponential_backoff(client, make_session, reference_data):
    ingestion_id = ingest(client, Vendor.PULSEFORGE, PF_SAMPLE)
    handlers = flaky_handlers([TransientError("db blip"), TransientError("db blip")])

    for expected_delay in (2, 4):
        assert run_once(make_session, "w", handlers=handlers)
        with make_session() as session:
            job = normalize_job(session)
            assert job.status == JobStatus.QUEUED
            remaining = session.scalar(select(Job.run_after - func.now()).where(Job.id == job.id))
            assert timedelta(seconds=expected_delay - 1) < remaining <= timedelta(seconds=expected_delay)
            assert job.last_error == "TransientError: db blip"
            assert session.get(IngestionBatch, ingestion_id).error == "TransientError: db blip"
        assert not run_once(make_session, "w", handlers=handlers)  # not due yet
        make_due(make_session)

    assert run_once(make_session, "w", handlers=handlers)
    with make_session() as session:
        job = normalize_job(session)
        assert (job.status, job.attempts) == (JobStatus.SUCCEEDED, 3)
        batch = session.get(IngestionBatch, ingestion_id)
        assert (batch.status, batch.error) == (BatchStatus.COMPLETED, None)


def test_job_is_dead_lettered_after_five_attempts(client, make_session, reference_data):
    ingestion_id = ingest(client, Vendor.PULSEFORGE, PF_SAMPLE)
    handlers = flaky_handlers([RuntimeError("bug")] * 10)  # unexpected errors count as transient

    for _ in range(5):
        assert run_once(make_session, "w", handlers=handlers)
        make_due(make_session)

    assert not run_once(make_session, "w", handlers=handlers)
    with make_session() as session:
        job = normalize_job(session)
        assert (job.status, job.attempts, job.last_error) == (JobStatus.DEAD, 5, "RuntimeError: bug")
        batch = session.get(IngestionBatch, ingestion_id)
        assert batch.status == BatchStatus.FAILED
        statuses = session.scalars(select(RawRecord.status)).all()
        assert set(statuses) == {RecordStatus.PENDING}  # raw data kept for a later replay


def test_permanent_error_is_dead_lettered_immediately(make_session):
    with make_session() as session:
        enqueue(session, NORMALIZE_BATCH, {"ingestion_id": "00000000-0000-0000-0000-000000000000"})
        session.commit()

    drain(make_session)

    with make_session() as session:
        job = session.scalar(select(Job))
        assert (job.status, job.attempts) == (JobStatus.DEAD, 1)
        assert job.last_error.startswith("PermanentError: ingestion")


def test_unknown_job_kind_is_dead_lettered(make_session):
    with make_session() as session:
        enqueue(session, "no_such_job", {})
        session.commit()

    drain(make_session)

    with make_session() as session:
        assert session.scalar(select(Job.status)) == JobStatus.DEAD


def test_failed_attempt_leaves_no_partial_results(client, make_session, reference_data):
    ingestion_id = ingest(client, Vendor.PULSEFORGE, PF_SAMPLE)

    def fail_after_work(session, payload, now):
        normalize_batch(session, payload, now)  # writes events...
        raise TransientError("crash before commit")  # ...then fails

    handlers = {NORMALIZE_BATCH: Handler(run=fail_after_work, on_failure=normalize_batch_failed)}
    run_once(make_session, "w", handlers=handlers)

    with make_session() as session:
        assert session.scalar(select(func.count()).select_from(Event)) == 0
        assert session.get(IngestionBatch, ingestion_id).status == BatchStatus.QUEUED
        assert set(session.scalars(select(RawRecord.status)).all()) == {RecordStatus.PENDING}


# --- claiming -------------------------------------------------------------------------


def test_concurrent_workers_skip_a_job_being_claimed(make_session):
    with make_session() as session:
        enqueue(session, "a", {})
        enqueue(session, "b", {})
        session.commit()

    with make_session() as worker_1, make_session() as worker_2:
        job_1 = claim_next(worker_1, "w1")  # row locked, not yet committed
        job_2 = claim_next(worker_2, "w2")  # skips the locked row instead of waiting
        assert {job_1.kind, job_2.kind} == {"a", "b"}
        assert claim_next(worker_2, "w2") is None
        worker_1.commit()
        worker_2.commit()


def claim_and_abandon(session, **fields) -> None:
    """Claim a job as a worker that then died ten minutes ago."""
    job = enqueue(session, "a", {})
    for name, value in fields.items():
        setattr(job, name, value)
    session.commit()
    claim_next(session, "dead-worker").locked_at = func.now() - timedelta(minutes=10)
    session.commit()


def test_stale_running_job_is_recovered(make_session):
    with make_session() as session:
        claim_and_abandon(session)

        assert recover_stale(session, timedelta(minutes=5)) == 1
        session.commit()

        job = session.scalar(select(Job))
        assert (job.status, job.locked_by, job.attempts) == (JobStatus.QUEUED, None, 1)


def test_recently_claimed_job_is_not_recovered(make_session):
    with make_session() as session:
        claim_and_abandon(session)

        assert recover_stale(session, timedelta(minutes=15)) == 0


def test_stale_job_out_of_attempts_is_dead_lettered(make_session):
    with make_session() as session:
        claim_and_abandon(session, max_attempts=1)

        recover_stale(session, timedelta(minutes=5))
        session.commit()

        assert session.scalar(select(Job.status)) == JobStatus.DEAD
