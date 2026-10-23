"""Load a machine's events, run the pure rules and store the result."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.derivation.rules import derive_state, event_ref
from app.jobs.errors import PermanentError
from app.models import Event, Machine, MachineState


def recompute_machine(session: Session, machine_id: str, now: datetime) -> None:
    # Locking the machine row serialises recomputes of one machine: a later recompute
    # waits, then reads the newest events, so an older result can never overwrite a newer.
    machine = session.get(Machine, machine_id, with_for_update=True)
    if machine is None:
        raise PermanentError(f"machine {machine_id} not found")

    events = session.scalars(select(Event).where(Event.machine_id == machine_id)).all()
    state = derive_state(machine, events)

    values = {
        "derived_status": state.status.value,
        "attention_level": state.attention_level,
        "needs_attention": state.needs_attention,
        "reasons": [r.to_dict() for r in state.reasons],
        "source_event_refs": [event_ref(e) for e in state.source_events],
        "latest_relevant_event_time": state.latest_relevant_event_time,
        "latest_event_time": state.latest_event_time,
        "event_count": state.event_count,
        "computed_at": now,
    }
    stmt = insert(MachineState).values(machine_id=machine_id, **values)
    session.execute(stmt.on_conflict_do_update(index_elements=["machine_id"], set_=values))
