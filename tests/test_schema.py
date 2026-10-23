import uuid
from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models import (
    Event,
    IngestionBatch,
    Job,
    JobStatus,
    Line,
    Machine,
    RawRecord,
    Vendor,
)
from app.reference_data import load_reference_data
from tests.conftest import DATA_DIR, alembic_config


def test_models_match_migrations(engine, database_url):
    # Fails if someone changes app/models.py without adding a migration.
    command.check(alembic_config(database_url))


def test_migrations_downgrade_and_upgrade_cleanly(engine, database_url):
    config = alembic_config(database_url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")


def test_reference_data_loads_all_lines_and_machines(db_session):
    assert load_reference_data(db_session, DATA_DIR) == (5, 8)

    assert db_session.scalar(select(func.count()).select_from(Line)) == 5
    assert db_session.scalar(select(func.count()).select_from(Machine)) == 8
    eq007 = db_session.get(Machine, "EQ-007")
    assert (eq007.line_id, eq007.asset_status, eq007.rated_max_temp_c) == ("LINE-E", "maintenance", 84.0)


def test_reference_data_load_is_idempotent(db_session):
    load_reference_data(db_session, DATA_DIR)
    load_reference_data(db_session, DATA_DIR)

    assert db_session.scalar(select(func.count()).select_from(Machine)) == 8


def _raw_record(session) -> RawRecord:
    batch = IngestionBatch(
        vendor=Vendor.PULSEFORGE, idempotency_key=str(uuid.uuid4()), payload_sha256="0" * 64, raw_payload={}
    )
    session.add(batch)
    session.flush()
    record = RawRecord(batch_id=batch.id, record_index=0, raw={})
    session.add(record)
    session.flush()
    return record


def _event(raw_record_id: int, source_record_id: str) -> Event:
    return Event(
        vendor=Vendor.PULSEFORGE,
        source_record_id=source_record_id,
        content_hash="a" * 64,
        raw_record_id=raw_record_id,
        machine_id="EQ-001",
        plant_id="PLANT_01",
        line_id="LINE-A",
        event_time=datetime(2026, 4, 18, 7, 59, 12, tzinfo=UTC),
        event_type="HIGH_VIBRATION",
    )


def test_event_vendor_and_source_id_is_unique(db_session, reference_data):
    # Database-level backstop for "same vendor + record id is a duplicate".
    record = _raw_record(db_session)
    db_session.add(_event(record.id, "PF-1001"))
    db_session.flush()

    db_session.add(_event(record.id, "PF-1001"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_only_one_queued_job_per_dedupe_key(db_session):
    db_session.add(Job(kind="recompute_machine", dedupe_key="machine:EQ-001"))
    db_session.flush()

    db_session.add(Job(kind="recompute_machine", dedupe_key="machine:EQ-001"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_finished_job_does_not_block_a_new_queued_one(db_session):
    db_session.add(Job(kind="recompute_machine", dedupe_key="machine:EQ-001", status=JobStatus.SUCCEEDED))
    db_session.add(Job(kind="recompute_machine", dedupe_key="machine:EQ-001"))
    db_session.flush()
