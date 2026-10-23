"""API response models; these make up the OpenAPI contract."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models import BatchStatus, JobStatus, RecordStatus, Vendor

DerivedStatus = Literal["NORMAL", "WATCH", "WARNING", "CRITICAL", "UNDER_MAINTENANCE", "UNKNOWN"]
ProcessingStatus = Literal["UP_TO_DATE", "PENDING", "FAILED", "NO_DATA"]


# --- ingestion ------------------------------------------------------------------------


class IngestionAccepted(BaseModel):
    ingestion_id: uuid.UUID
    vendor: Vendor
    status: BatchStatus
    record_count: int
    received_at: datetime
    idempotent_replay: bool = Field(
        description="True if this exact request was already accepted earlier; nothing new was stored."
    )
    status_url: str


class JobOut(BaseModel):
    job_id: int
    kind: str
    status: JobStatus
    attempts: int
    max_attempts: int
    next_attempt_at: datetime | None = Field(description="When a queued job will next be tried.")
    last_error: str | None
    payload: dict[str, Any]
    created_at: datetime
    finished_at: datetime | None


class IngestionStatus(BaseModel):
    ingestion_id: uuid.UUID
    vendor: Vendor
    status: BatchStatus
    received_at: datetime
    processed_at: datetime | None
    record_count: int
    accepted_count: int
    rejected_count: int
    duplicate_count: int
    pending_count: int = Field(description="Records not processed yet.")
    error: str | None = Field(description="Last processing error, if an attempt failed.")
    job: JobOut | None
    records_url: str


class RawRecordOut(BaseModel):
    record_index: int
    source_record_id: str | None
    status: RecordStatus
    reason_codes: list[str] = Field(description="Why it was rejected, or what it duplicates.")
    event_id: int | None = Field(description="Event created from it, or the event it duplicates.")
    raw: Any = Field(description="The record exactly as the vendor sent it.")


class RawRecordList(BaseModel):
    items: list[RawRecordOut]
    total: int


# --- machines -------------------------------------------------------------------------


class EventRef(BaseModel):
    event_id: int
    vendor: str
    source_record_id: str
    event_type: str
    event_time: datetime


class ReasonOut(BaseModel):
    code: str
    severity: int = Field(description="0-4; 0 explains the status without adding attention.")
    dimension: str
    message: str
    confidence: float | None
    source_event_refs: list[EventRef]


class MachineView(BaseModel):
    machine_id: str
    plant_id: str
    line_id: str
    machine_type: str
    criticality: str
    asset_status: str
    derived_status: DerivedStatus
    needs_attention: bool
    attention_level: int = Field(description="Highest reason severity, 0 (none) to 4 (critical).")
    reason_codes: list[str]
    reasons: list[ReasonOut]
    latest_relevant_event_time: datetime | None = Field(
        description="Latest event behind a contributing reason (or the latest event if none)."
    )
    latest_event_time: datetime | None
    freshness: Literal["FRESH", "STALE"]
    processing_status: ProcessingStatus = Field(
        description="UP_TO_DATE, PENDING (a recompute is queued/running), FAILED (last recompute "
        "was dead-lettered) or NO_DATA (no events yet)."
    )
    last_processed_at: datetime | None = Field(description="When this state was last recomputed.")
    event_count: int
    source_event_refs: list[EventRef]


class MachineList(BaseModel):
    as_of: datetime
    count: int
    items: list[MachineView]


class EventOut(BaseModel):
    event_id: int
    vendor: Vendor
    source_record_id: str
    ingestion_id: uuid.UUID
    raw_record_id: int
    event_time: datetime
    vendor_received_at: datetime | None
    ingested_at: datetime
    event_type: str
    severity: int | None
    temperature_c: float | None
    vibration_mm_s: float | None
    vibration_g: float | None
    power_kw: float | None
    sensor_health: float | None
    signal_quality: str | None
    machine_state: str | None
    machine_active: bool | None
    vendor_confidence: float | None
    inspection_result: str | None
    maintenance_status: str | None
    work_order_status: str | None
    post_maintenance_check: str | None
    manual_confidence: float | None
    attributes: dict[str, Any]
    quality_flags: list[str]


class EventList(BaseModel):
    items: list[EventOut]
    total: int


# --- summary --------------------------------------------------------------------------


class GroupSummary(BaseModel):
    machine_count: int
    needs_attention_count: int
    max_attention_level: int
    status_counts: dict[str, int]
    machines_needing_attention: list[str]


class PlantSummary(GroupSummary):
    plant_id: str


class LineSummary(GroupSummary):
    plant_id: str
    line_id: str
    line_name: str


class CriticalMachine(BaseModel):
    machine_id: str
    plant_id: str
    line_id: str
    reason_codes: list[str]
    latest_relevant_event_time: datetime | None


class ProcessingSummary(BaseModel):
    stale_machines: list[str]
    pending_machines: list[str]
    failed_machines: list[str]
    no_data_machines: list[str]
    ingestions_by_status: dict[str, int] = Field(description="Across all plants.")
    ingestions_with_rejections: int = Field(description="Completed batches with at least one rejected record.")
    dead_letter_jobs: int


class Summary(BaseModel):
    as_of: datetime
    plant_id: str | None
    machine_count: int
    needs_attention_count: int
    status_counts: dict[str, int]
    plants: list[PlantSummary]
    lines: list[LineSummary] = Field(description="Most machines needing attention first.")
    critical_machines: list[CriticalMachine]
    processing: ProcessingSummary
