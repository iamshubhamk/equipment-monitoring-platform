"""Database schema.

Five groups of tables:
- reference data:  lines, machines (loaded from the provided CSVs)
- ingestion:       ingestion_batches, raw_records (exactly what vendors sent us)
- canonical data:  events (one normalized row per accepted vendor record)
- derived data:    machine_states (recomputed from events, never edited by hand)
- background work: jobs (Postgres-backed queue; status 'dead' is the dead-letter list)
"""

import enum
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    # Deterministic constraint names, so migrations can reference them reliably.
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


def _enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    # Stored as VARCHAR + CHECK constraint rather than a native Postgres ENUM,
    # so adding a value later is a simple migration.
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
    )


class Vendor(enum.StrEnum):
    PULSEFORGE = "pulseforge"
    THERMEXWATCH = "thermexwatch"
    MAINTAFLOW = "maintaflow"
    WORKORDERS = "workorders"


class BatchStatus(enum.StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class RecordStatus(enum.StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    DEAD = "dead"


# --- reference data -------------------------------------------------------------------


class Line(Base):
    __tablename__ = "lines"

    line_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    plant_id: Mapped[str] = mapped_column(String(32), index=True)
    line_name: Mapped[str] = mapped_column(String(64))
    operating_window: Mapped[str] = mapped_column(String(32))


class Machine(Base):
    __tablename__ = "machines"

    machine_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    plant_id: Mapped[str] = mapped_column(String(32), index=True)
    line_id: Mapped[str] = mapped_column(ForeignKey("lines.line_id"), index=True)
    machine_type: Mapped[str] = mapped_column(String(64))
    criticality: Mapped[str] = mapped_column(String(16))
    installed_date: Mapped[date] = mapped_column(Date)
    rated_max_temp_c: Mapped[float] = mapped_column(Float)
    rated_max_vibration_mm_s: Mapped[float] = mapped_column(Float)
    baseline_power_kw: Mapped[float] = mapped_column(Float)
    asset_status: Mapped[str] = mapped_column(String(32))


# --- ingestion ------------------------------------------------------------------------


class IngestionBatch(Base):
    """One accepted POST to /v1/ingest/{vendor}, stored verbatim."""

    __tablename__ = "ingestion_batches"
    __table_args__ = (UniqueConstraint("vendor", "idempotency_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor: Mapped[Vendor] = mapped_column(_enum(Vendor, "vendor"))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[BatchStatus] = mapped_column(
        _enum(BatchStatus, "batch_status"), default=BatchStatus.QUEUED
    )
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    accepted_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RawRecord(Base):
    """One record inside a batch, with the outcome of validating it."""

    __tablename__ = "raw_records"
    __table_args__ = (UniqueConstraint("batch_id", "record_index"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ingestion_batches.id", ondelete="CASCADE"), index=True
    )
    record_index: Mapped[int] = mapped_column(Integer)
    source_record_id: Mapped[str | None] = mapped_column(String(128))
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[RecordStatus] = mapped_column(
        _enum(RecordStatus, "record_status"), default=RecordStatus.PENDING
    )
    # Why a record was rejected, e.g. ["MISSING_MACHINE_ID"]; empty when accepted.
    reason_codes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    # Accepted -> the event created from it; duplicate -> the event it duplicates.
    # use_alter: events also references raw_records, so this FK is added after both exist.
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", use_alter=True))


# --- canonical data -------------------------------------------------------------------


class Event(Base):
    """A normalized vendor record: UTC times, Celsius, 0-4 severity, resolved machine.

    Measurement columns are typed because the derivation rules read them; anything
    vendor-specific that the rules don't need goes into `attributes`.
    """

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("vendor", "source_record_id"),
        Index("ix_events_machine_time", "machine_id", "event_time"),
        Index("ix_events_vendor_content_hash", "vendor", "content_hash"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vendor: Mapped[Vendor] = mapped_column(_enum(Vendor, "vendor"))
    source_record_id: Mapped[str] = mapped_column(String(128))
    # Hash of the normalized content without ids or arrival times, to catch the same
    # reading re-sent under a new id (e.g. TW-8801 vs TW-8801-R).
    content_hash: Mapped[str] = mapped_column(String(64))
    raw_record_id: Mapped[int] = mapped_column(ForeignKey("raw_records.id"))

    machine_id: Mapped[str] = mapped_column(ForeignKey("machines.machine_id"))
    plant_id: Mapped[str] = mapped_column(String(32))
    line_id: Mapped[str] = mapped_column(String(32))
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    vendor_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # What happened: the vendor's code, e.g. HIGH_VIBRATION, TEMP_CRIT, inspection.
    event_type: Mapped[str] = mapped_column(String(64))
    severity: Mapped[int | None] = mapped_column(SmallInteger)  # 0 (nominal) .. 4 (critical)

    # Sensor readings
    temperature_c: Mapped[float | None] = mapped_column(Float)
    vibration_mm_s: Mapped[float | None] = mapped_column(Float)
    vibration_g: Mapped[float | None] = mapped_column(Float)  # ThermexWatch; not converted
    power_kw: Mapped[float | None] = mapped_column(Float)
    sensor_health: Mapped[float | None] = mapped_column(Float)  # 0..1
    signal_quality: Mapped[str | None] = mapped_column(String(16))
    machine_state: Mapped[str | None] = mapped_column(String(32))
    machine_active: Mapped[bool | None]
    vendor_confidence: Mapped[float | None] = mapped_column(Float)  # 0..1, vendor-supplied

    # Maintenance / inspection / work orders
    inspection_result: Mapped[str | None] = mapped_column(String(64))
    maintenance_status: Mapped[str | None] = mapped_column(String(32))
    work_order_status: Mapped[str | None] = mapped_column(String(32))
    post_maintenance_check: Mapped[str | None] = mapped_column(String(32))
    manual_confidence: Mapped[float | None] = mapped_column(Float)  # MaintaFlow high/medium/low

    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Data-quality notes on accepted records, e.g. ["TEMPERATURE_OUT_OF_RANGE"].
    quality_flags: Mapped[list[str]] = mapped_column(JSONB, default=list)


# --- derived data ---------------------------------------------------------------------


class MachineState(Base):
    """The latest derived attention view for one machine.

    Freshness (FRESH/STALE) is not stored: it depends on "now", so the read API
    computes it from latest_event_time and the clock.
    """

    __tablename__ = "machine_states"

    machine_id: Mapped[str] = mapped_column(ForeignKey("machines.machine_id"), primary_key=True)
    derived_status: Mapped[str] = mapped_column(String(32))
    attention_level: Mapped[int] = mapped_column(SmallInteger)
    needs_attention: Mapped[bool]
    # [{"code": ..., "severity": ..., "message": ..., "source_event_refs": [...]}]
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    source_event_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    latest_relevant_event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latest_event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# --- background work ------------------------------------------------------------------


class Job(Base):
    """A unit of background work, claimed with SELECT ... FOR UPDATE SKIP LOCKED."""

    __tablename__ = "jobs"
    __table_args__ = (
        # What the worker polls: queued jobs that are due.
        Index("ix_jobs_queued_due", "run_after", postgresql_where=text("status = 'queued'")),
        # At most one queued job per dedupe key, so 50 new events for one machine
        # coalesce into a single pending recompute.
        Index(
            "uq_jobs_queued_dedupe_key",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status = 'queued' AND dedupe_key IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[JobStatus] = mapped_column(
        _enum(JobStatus, "job_status"), default=JobStatus.QUEUED, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
