"""Read-side queries shared by the machine and summary endpoints."""

from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.derivation.rules import Status, freshness
from app.jobs.queue import RECOMPUTE_MACHINE
from app.models import (
    BatchStatus,
    IngestionBatch,
    Job,
    JobStatus,
    Line,
    Machine,
    MachineState,
)
from app.schemas import (
    CriticalMachine,
    LineSummary,
    MachineView,
    PlantSummary,
    ProcessingSummary,
    Summary,
)

STATUS_ORDER = [s.value for s in Status]


def _latest_recompute_status(session: Session) -> dict[str, JobStatus]:
    """Status of the most recent recompute job per machine."""
    machine_id = Job.payload["machine_id"].astext
    rows = session.execute(
        select(machine_id, Job.status)
        .where(Job.kind == RECOMPUTE_MACHINE)
        .distinct(machine_id)
        .order_by(machine_id, Job.id.desc())
    )
    return dict(rows.tuples().all())


def _processing_status(state: MachineState | None, job_status: JobStatus | None) -> str:
    if job_status in (JobStatus.QUEUED, JobStatus.RUNNING):
        return "PENDING"
    if job_status == JobStatus.DEAD:
        return "FAILED"
    return "UP_TO_DATE" if state is not None else "NO_DATA"


def _view(machine: Machine, state: MachineState | None, job_status: JobStatus | None, now: datetime, stale_after: timedelta) -> MachineView:
    reasons = state.reasons if state else []
    return MachineView(
        machine_id=machine.machine_id,
        plant_id=machine.plant_id,
        line_id=machine.line_id,
        machine_type=machine.machine_type,
        criticality=machine.criticality,
        asset_status=machine.asset_status,
        derived_status=state.derived_status if state else Status.UNKNOWN.value,
        needs_attention=state.needs_attention if state else False,
        attention_level=state.attention_level if state else 0,
        reason_codes=[r["code"] for r in reasons],
        reasons=reasons,
        latest_relevant_event_time=state.latest_relevant_event_time if state else None,
        latest_event_time=state.latest_event_time if state else None,
        freshness=freshness(state.latest_event_time if state else None, now, stale_after).value,
        processing_status=_processing_status(state, job_status),
        last_processed_at=state.computed_at if state else None,
        event_count=state.event_count if state else 0,
        source_event_refs=state.source_event_refs if state else [],
    )


def machine_views(
    session: Session,
    now: datetime,
    stale_after: timedelta,
    *,
    machine_id: str | None = None,
    plant_id: str | None = None,
    line_id: str | None = None,
) -> list[MachineView]:
    """Every machine in the reference data, with its derived state (UNKNOWN if none yet)."""
    query = select(Machine, MachineState).outerjoin(MachineState, MachineState.machine_id == Machine.machine_id)
    if machine_id:
        query = query.where(Machine.machine_id == machine_id)
    if plant_id:
        query = query.where(Machine.plant_id == plant_id)
    if line_id:
        query = query.where(Machine.line_id == line_id)
    jobs = _latest_recompute_status(session)
    views = [
        _view(machine, state, jobs.get(machine.machine_id), now, stale_after)
        for machine, state in session.execute(query).tuples()
    ]
    # Most urgent first.
    return sorted(views, key=lambda v: (-v.attention_level, not v.needs_attention, v.machine_id))


def _group(views: list[MachineView]) -> dict:
    counts = Counter(v.derived_status for v in views)
    attention = [v.machine_id for v in views if v.needs_attention]
    return {
        "machine_count": len(views),
        "needs_attention_count": len(attention),
        "max_attention_level": max((v.attention_level for v in views if v.needs_attention), default=0),
        "status_counts": {s: counts.get(s, 0) for s in STATUS_ORDER},
        "machines_needing_attention": sorted(attention),
    }


def _by(views: Iterable[MachineView], key) -> dict[tuple, list[MachineView]]:
    groups: dict[tuple, list[MachineView]] = {}
    for v in views:
        groups.setdefault(key(v), []).append(v)
    return groups


def summary(session: Session, now: datetime, stale_after: timedelta, plant_id: str | None = None) -> Summary:
    views = machine_views(session, now, stale_after, plant_id=plant_id)
    line_names = {line.line_id: line.line_name for line in session.scalars(select(Line))}

    lines = [
        LineSummary(plant_id=plant, line_id=line, line_name=line_names.get(line, line), **_group(group))
        for (plant, line), group in _by(views, lambda v: (v.plant_id, v.line_id)).items()
    ]
    lines.sort(key=lambda s: (-s.needs_attention_count, -s.max_attention_level, s.line_id))
    plants = [
        PlantSummary(plant_id=plant, **_group(group))
        for (plant,), group in sorted(_by(views, lambda v: (v.plant_id,)).items())
    ]

    batch_counts = dict(
        session.execute(select(IngestionBatch.status, func.count()).group_by(IngestionBatch.status)).tuples().all()
    )
    overall = _group(views)
    return Summary(
        as_of=now,
        plant_id=plant_id,
        machine_count=overall["machine_count"],
        needs_attention_count=overall["needs_attention_count"],
        status_counts=overall["status_counts"],
        plants=plants,
        lines=lines,
        critical_machines=[
            CriticalMachine(
                machine_id=v.machine_id,
                plant_id=v.plant_id,
                line_id=v.line_id,
                reason_codes=v.reason_codes,
                latest_relevant_event_time=v.latest_relevant_event_time,
            )
            for v in views
            if v.derived_status == Status.CRITICAL
        ],
        processing=ProcessingSummary(
            stale_machines=sorted(v.machine_id for v in views if v.freshness == "STALE" and v.event_count),
            pending_machines=sorted(v.machine_id for v in views if v.processing_status == "PENDING"),
            failed_machines=sorted(v.machine_id for v in views if v.processing_status == "FAILED"),
            no_data_machines=sorted(v.machine_id for v in views if v.event_count == 0),
            ingestions_by_status={s.value: batch_counts.get(s, 0) for s in BatchStatus},
            ingestions_with_rejections=session.scalar(
                select(func.count()).where(
                    IngestionBatch.status == BatchStatus.COMPLETED, IngestionBatch.rejected_count > 0
                )
            ),
            dead_letter_jobs=session.scalar(select(func.count()).where(Job.status == JobStatus.DEAD)),
        ),
    )
