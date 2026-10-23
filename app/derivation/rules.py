"""Derive a machine's attention state from all of its events.

`derive_state` is a pure function: same machine + same events -> same result, whatever
order the events arrived in. Events are ordered by event time only.

1. Each dimension (vibration, temperature, ...) looks only at its latest evidence.
2. A completed maintenance (MaintaFlow 'completed', or a work order closed with a
   passed check) resets everything older than itself, except still-open work orders.
3. Reasons carry a 0-4 severity; the highest decides the status, with two overrides:
   CRITICAL always wins, then UNDER_MAINTENANCE.
"""

import enum
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.models import Event, Machine
from app.normalization.canonical import EventType

# Thresholds against asset_reference.csv
ABOVE_RATED_SEVERITY = 3
POWER_DEVIATION_RATIO = 0.30  # more than 30% away from baseline_power_kw
POWER_DEVIATION_SEVERITY = 2
SENSOR_HEALTH_MIN = 0.6
SENSOR_HEALTH_LOW_SEVERITY = 2
SIGNAL_POOR_SEVERITY = 1
UNKNOWN_ALERT_SEVERITY = 1
OPEN_WORK_ORDER_SEVERITY = 1

INSPECTION_SEVERITY = {
    "major_defect_found": ("INSPECTION_MAJOR_DEFECT", 3),
    "failed": ("INSPECTION_FAILED", 3),
    "minor_defect_found": ("INSPECTION_MINOR_DEFECT", 1),
    "passed_with_observation": ("INSPECTION_OBSERVATION", 1),
    "calibration_pending": ("CALIBRATION_PENDING", 1),
}
MAINTENANCE_SEVERITY = {
    "overdue": ("MAINTENANCE_OVERDUE", 2),
    "due_soon": ("MAINTENANCE_DUE_SOON", 1),
}
OPEN_WORK_ORDER_STATES = {"open", "in_progress"}


class Status(enum.StrEnum):
    NORMAL = "NORMAL"
    WATCH = "WATCH"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    UNDER_MAINTENANCE = "UNDER_MAINTENANCE"
    UNKNOWN = "UNKNOWN"


class Freshness(enum.StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"


NEEDS_ATTENTION = {Status.WATCH, Status.WARNING, Status.CRITICAL}


@dataclass(frozen=True)
class Reason:
    code: str
    severity: int  # 0 = explains the status but adds no attention
    dimension: str
    message: str
    events: tuple[Event, ...]
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "dimension": self.dimension,
            "message": self.message,
            "confidence": self.confidence,
            "source_event_refs": [event_ref(e) for e in self.events],
        }


@dataclass(frozen=True)
class DerivedState:
    status: Status
    attention_level: int
    needs_attention: bool
    reasons: list[Reason] = field(default_factory=list)
    source_events: list[Event] = field(default_factory=list)
    latest_relevant_event_time: datetime | None = None
    latest_event_time: datetime | None = None
    event_count: int = 0


def event_ref(e: Event) -> dict[str, Any]:
    return {
        "event_id": e.id,
        "vendor": str(e.vendor),
        "source_record_id": e.source_record_id,
        "event_type": str(e.event_type),
        "event_time": e.event_time.isoformat(),
    }


def freshness(latest_event_time: datetime | None, now: datetime, stale_after: timedelta) -> Freshness:
    if latest_event_time is None or now - latest_event_time > stale_after:
        return Freshness.STALE
    return Freshness.FRESH


# --- helpers --------------------------------------------------------------------------


def _latest(events: Sequence[Event], matches: Callable[[Event], bool]) -> Event | None:
    """Events are sorted oldest first, so the last match is the latest."""
    return next((e for e in reversed(events) if matches(e)), None)


def _vendor_code(e: Event) -> str:
    return (e.attributes or {}).get("vendor_event_type") or str(e.event_type)


def _confidence(e: Event) -> float | None:
    return e.manual_confidence if e.manual_confidence is not None else e.vendor_confidence


Check = Callable[[Event, Machine], tuple[str, int, str] | None]


def _reading_dimension(
    dimension: str,
    alert_type: EventType,
    has_reading: Callable[[Event], bool],
    checks: Iterable[Check],
    events: Sequence[Event],
    machine: Machine,
) -> list[Reason]:
    """Latest event with a reading (or an alert) for this dimension decides it.

    The vendor's severity counts only if the event is an alert for this dimension, so a
    recovery signal or an alert about something else never raises it.
    """
    e = _latest(events, lambda e: e.event_type == alert_type or has_reading(e))
    if e is None:
        return []
    reasons = []
    if e.event_type == alert_type and e.severity:
        reasons.append(
            Reason(
                f"{dimension.upper()}_ALERT",
                e.severity,
                dimension,
                f"{e.vendor} reported {_vendor_code(e)} (severity {e.severity})",
                (e,),
                _confidence(e),
            )
        )
    for check in checks:
        if hit := check(e, machine):
            code, severity, message = hit
            reasons.append(Reason(code, severity, dimension, message, (e,), _confidence(e)))
    return reasons


def _temperature_above_rated(e: Event, m: Machine):
    if e.temperature_c is not None and e.temperature_c > m.rated_max_temp_c:
        return "TEMPERATURE_ABOVE_RATED", ABOVE_RATED_SEVERITY, (
            f"{e.temperature_c:g} °C is above the rated max of {m.rated_max_temp_c:g} °C"
        )
    return None


def _vibration_above_rated(e: Event, m: Machine):
    # Only mm/s readings can be compared; ThermexWatch's g readings have no rated limit.
    if e.vibration_mm_s is not None and e.vibration_mm_s > m.rated_max_vibration_mm_s:
        return "VIBRATION_ABOVE_RATED", ABOVE_RATED_SEVERITY, (
            f"{e.vibration_mm_s:g} mm/s is above the rated max of {m.rated_max_vibration_mm_s:g} mm/s"
        )
    return None


def _power_deviation(e: Event, m: Machine):
    if e.power_kw is None or not m.baseline_power_kw:
        return None
    deviation = (e.power_kw - m.baseline_power_kw) / m.baseline_power_kw
    if abs(deviation) > POWER_DEVIATION_RATIO:
        return "POWER_DEVIATION", POWER_DEVIATION_SEVERITY, (
            f"{e.power_kw:g} kW is {deviation:+.0%} from the {m.baseline_power_kw:g} kW baseline"
        )
    return None


def _sensor_health_low(e: Event, m: Machine):
    if e.sensor_health is not None and e.sensor_health < SENSOR_HEALTH_MIN:
        return "SENSOR_HEALTH_LOW", SENSOR_HEALTH_LOW_SEVERITY, (
            f"sensor health {e.sensor_health:g} is below {SENSOR_HEALTH_MIN:g}"
        )
    return None


def _signal_quality_poor(e: Event, m: Machine):
    if e.signal_quality == "poor":
        return "SIGNAL_QUALITY_POOR", SIGNAL_POOR_SEVERITY, "signal quality reported as poor"
    return None


def _unknown_alert(events: Sequence[Event]) -> list[Reason]:
    e = _latest(events, lambda e: e.event_type == EventType.UNKNOWN)
    if e is None:
        return []
    return [
        Reason(
            "UNMAPPED_VENDOR_ALERT",
            UNKNOWN_ALERT_SEVERITY,
            "unknown_alert",
            f"{e.vendor} sent unrecognised code {_vendor_code(e)}; its severity is not trusted",
            (e,),
            _confidence(e),
        )
    ]


def _coded_dimension(
    dimension: str, field_name: str, table: dict[str, tuple[str, int]], events: Sequence[Event]
) -> list[Reason]:
    e = _latest(events, lambda e: getattr(e, field_name) is not None)
    if e is None or getattr(e, field_name) not in table:
        return []
    code, severity = table[getattr(e, field_name)]
    note = (e.attributes or {}).get("note")
    message = f"{field_name.replace('_', ' ')}: {getattr(e, field_name)}" + (f" ({note})" if note else "")
    return [Reason(code, severity, dimension, message, (e,), _confidence(e))]


def _open_work_orders(events: Sequence[Event]) -> list[Event]:
    """Latest state of each work order; those still open."""
    latest_by_order: dict[str, Event] = {}
    for e in events:
        if e.event_type == EventType.WORK_ORDER:
            latest_by_order[(e.attributes or {}).get("work_order_id", e.source_record_id)] = e
    return [e for e in latest_by_order.values() if e.work_order_status in OPEN_WORK_ORDER_STATES]


def _is_reset(e: Event) -> bool:
    return e.maintenance_status == "completed" or (
        e.work_order_status == "closed" and e.post_maintenance_check == "passed"
    )


def _is_active(events: Sequence[Event]) -> bool | None:
    """Latest activity signal: is_active, else machine_state == running. None if unknown."""
    e = _latest(events, lambda e: e.machine_active is not None or e.machine_state is not None)
    if e is None:
        return None
    return e.machine_active if e.machine_active is not None else e.machine_state == "running"


def _status_for_level(level: int) -> Status:
    if level >= 4:
        return Status.CRITICAL
    if level == 3:
        return Status.WARNING
    if level >= 1:
        return Status.WATCH
    return Status.NORMAL


# --- the rule -------------------------------------------------------------------------


def derive_state(machine: Machine, events: Iterable[Event]) -> DerivedState:
    # Event time decides order, never arrival. On a tie, the more severe event counts as
    # later (cautious), then the id keeps the order deterministic.
    ordered = sorted(events, key=lambda e: (e.event_time, e.severity or 0, e.id or 0))
    if not ordered:
        return DerivedState(status=Status.UNKNOWN, attention_level=0, needs_attention=False)

    reset = _latest(ordered, _is_reset)
    current = [e for e in ordered if reset is None or e.event_time >= reset.event_time]

    reasons = [
        *_reading_dimension(
            "vibration",
            EventType.VIBRATION_ALERT,
            lambda e: e.vibration_mm_s is not None or e.vibration_g is not None,
            [_vibration_above_rated],
            current,
            machine,
        ),
        *_reading_dimension(
            "temperature",
            EventType.TEMPERATURE_ALERT,
            lambda e: e.temperature_c is not None,
            [_temperature_above_rated],
            current,
            machine,
        ),
        *_reading_dimension(
            "power", EventType.POWER_ALERT, lambda e: e.power_kw is not None, [_power_deviation], current, machine
        ),
        *_reading_dimension(
            "sensor_health",
            EventType.SENSOR_HEALTH_ALERT,
            lambda e: e.sensor_health is not None or e.signal_quality is not None,
            [_sensor_health_low, _signal_quality_poor],
            current,
            machine,
        ),
        *_unknown_alert(current),
        *_coded_dimension("inspection", "inspection_result", INSPECTION_SEVERITY, current),
        *_coded_dimension("maintenance", "maintenance_status", MAINTENANCE_SEVERITY, current),
    ]
    open_orders = _open_work_orders(ordered)  # a reset never closes a work order
    reasons += [
        Reason(
            "OPEN_WORK_ORDER",
            OPEN_WORK_ORDER_SEVERITY,
            "work_orders",
            f"work order {(e.attributes or {}).get('work_order_id')} is {e.work_order_status}"
            + (f" ({e.attributes['maintenance_type']})" if (e.attributes or {}).get("maintenance_type") else ""),
            (e,),
        )
        for e in open_orders
    ]

    level = max((r.severity for r in reasons), default=0)
    inactive = _is_active(ordered) is False
    if level >= 4:
        status = Status.CRITICAL
    elif machine.asset_status == "maintenance" or (open_orders and inactive):
        status = Status.UNDER_MAINTENANCE
        if machine.asset_status == "maintenance":
            reasons.append(Reason("ASSET_IN_MAINTENANCE", 0, "asset", "asset status is 'maintenance'", ()))
        if open_orders and inactive:
            reasons.append(
                Reason("INACTIVE_WITH_OPEN_WORK_ORDER", 0, "work_orders", "machine is not running and has an open work order", ())
            )
    else:
        status = _status_for_level(level)

    reasons.sort(key=lambda r: -r.severity)
    contributing = [r for r in reasons if r.severity > 0]
    source_events = sorted(
        {e.id: e for r in reasons for e in r.events}.values() or [ordered[-1]],
        key=lambda e: (e.event_time, e.id or 0),
    )
    return DerivedState(
        status=status,
        attention_level=level,
        needs_attention=status in NEEDS_ATTENTION,
        reasons=reasons,
        source_events=source_events,
        latest_relevant_event_time=max(
            (e.event_time for r in contributing for e in r.events), default=ordered[-1].event_time
        ),
        latest_event_time=ordered[-1].event_time,
        event_count=len(ordered),
    )
