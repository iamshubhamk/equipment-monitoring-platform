from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_clock, get_stale_after
from app.clock import Clock
from app.db import get_session
from app.schemas import Summary
from app.views import summary

router = APIRouter(prefix="/v1", tags=["summary"])


@router.get("/summary", response_model=Summary)
def get_summary(
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
    stale_after: Annotated[timedelta, Depends(get_stale_after)],
    plant_id: str | None = None,
) -> Summary:
    """Plant/line overview: status counts, lines ranked by machines needing attention,
    critical machines, and machines with stale, pending or failed processing."""
    return summary(session, clock(), stale_after, plant_id)
