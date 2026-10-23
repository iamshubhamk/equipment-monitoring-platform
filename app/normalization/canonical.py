"""The canonical event every vendor record is normalized into."""

import enum
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.models import Vendor


class EventType(enum.StrEnum):
    """Vendor-neutral event types. The vendor's own code is kept in attributes."""

    VIBRATION_ALERT = "vibration_alert"
    TEMPERATURE_ALERT = "temperature_alert"
    POWER_ALERT = "power_alert"
    SENSOR_HEALTH_ALERT = "sensor_health_alert"
    RECOVERY = "recovery"  # readings returning towards baseline
    NOMINAL = "nominal"  # vendor says everything is normal
    INSPECTION = "inspection"
    MAINTENANCE_UPDATE = "maintenance_update"
    CALIBRATION = "calibration"
    OPERATOR_NOTE = "operator_note"
    WORK_ORDER = "work_order"
    UNKNOWN = "unknown"


class RejectReason(enum.StrEnum):
    """Why a record could not become an event. Anything else only raises a quality flag."""

    INVALID_RECORD_SHAPE = "INVALID_RECORD_SHAPE"
    MISSING_RECORD_ID = "MISSING_RECORD_ID"
    MISSING_MACHINE_ID = "MISSING_MACHINE_ID"
    UNKNOWN_MACHINE = "UNKNOWN_MACHINE"
    MISSING_EVENT_TIME = "MISSING_EVENT_TIME"
    INVALID_EVENT_TIME = "INVALID_EVENT_TIME"


@dataclass(frozen=True)
class MachineRef:
    plant_id: str
    line_id: str


@dataclass(frozen=True)
class NormalizationContext:
    """Everything a normalizer needs besides the record, so normalizers stay pure."""

    machines: dict[str, MachineRef]
    maintaflow_tz: ZoneInfo = ZoneInfo("UTC")


@dataclass
class NormalizedEvent:
    vendor: Vendor
    source_record_id: str
    machine_id: str
    plant_id: str
    line_id: str
    event_time: datetime
    event_type: EventType
    vendor_received_at: datetime | None = None
    severity: int | None = None  # 0 nominal .. 4 critical
    temperature_c: float | None = None
    vibration_mm_s: float | None = None
    vibration_g: float | None = None
    power_kw: float | None = None
    sensor_health: float | None = None
    signal_quality: str | None = None
    machine_state: str | None = None
    machine_active: bool | None = None
    vendor_confidence: float | None = None
    inspection_result: str | None = None
    maintenance_status: str | None = None
    work_order_status: str | None = None
    post_maintenance_check: str | None = None
    manual_confidence: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    quality_flags: list[str] = field(default_factory=list)

    # Identity and arrival details, not part of what was observed.
    _NOT_CONTENT = ("source_record_id", "vendor_received_at", "quality_flags")

    def content_hash(self) -> str:
        """Hash of what was observed, so a re-send under a new id is recognisable."""
        content = {k: v for k, v in asdict(self).items() if k not in self._NOT_CONTENT}
        encoded = json.dumps(content, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass
class NormalizationResult:
    event: NormalizedEvent | None = None
    reject_reasons: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.event is not None
