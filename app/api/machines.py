from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_clock, get_stale_after
from app.clock import Clock
from app.db import get_session
from app.models import Event, Machine, RawRecord
from app.schemas import DerivedStatus, EventList, EventOut, MachineList, MachineView
from app.views import machine_views

router = APIRouter(prefix="/v1", tags=["machines"])

NOT_FOUND = {404: {"description": "Unknown machine"}}


@router.get("/machines", response_model=MachineList)
def list_machines(
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
    stale_after: Annotated[timedelta, Depends(get_stale_after)],
    plant_id: str | None = None,
    line_id: str | None = None,
    derived_status: Annotated[DerivedStatus | None, Query(alias="status")] = None,
    needs_attention: bool | None = None,
) -> MachineList:
    """Attention view of every machine, most urgent first."""
    now = clock()
    views = machine_views(session, now, stale_after, plant_id=plant_id, line_id=line_id)
    if derived_status is not None:
        views = [v for v in views if v.derived_status == derived_status]
    if needs_attention is not None:
        views = [v for v in views if v.needs_attention == needs_attention]
    return MachineList(as_of=now, count=len(views), items=views)


@router.get("/machines/{machine_id}", response_model=MachineView, responses=NOT_FOUND)
def get_machine(
    machine_id: str,
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
    stale_after: Annotated[timedelta, Depends(get_stale_after)],
) -> MachineView:
    """One machine's derived status, the reasons behind it and the events they came from."""
    views = machine_views(session, clock(), stale_after, machine_id=machine_id)
    if not views:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"machine {machine_id} not found")
    return views[0]


@router.get("/machines/{machine_id}/events", response_model=EventList, responses=NOT_FOUND)
def list_machine_events(
    machine_id: str,
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EventList:
    """The machine's normalized events, newest event time first, each linked to its raw record."""
    if session.get(Machine, machine_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"machine {machine_id} not found")
    rows = session.execute(
        select(Event, RawRecord.batch_id)
        .join(RawRecord, RawRecord.id == Event.raw_record_id)
        .where(Event.machine_id == machine_id)
        .order_by(Event.event_time.desc(), Event.id.desc())
        .limit(limit)
        .offset(offset)
    ).tuples()
    items = [
        EventOut(
            event_id=e.id,
            ingestion_id=batch_id,
            **{
                field: getattr(e, field)
                for field in EventOut.model_fields
                if field not in ("event_id", "ingestion_id")
            },
        )
        for e, batch_id in rows
    ]
    total = session.scalar(select(func.count()).where(Event.machine_id == machine_id))
    return EventList(items=items, total=total)

