"""Parsing helpers shared by the vendor normalizers.

`RecordReader` wraps one raw record and collects two kinds of problems while fields are
read:
- reject reasons: the record can't become an event (missing/invalid id, machine or time)
- quality flags: an optional value was bad and stored as null, or a code was unknown
"""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.normalization.canonical import MachineRef, NormalizationContext, RejectReason

# Epoch-ms values outside this window are treated as invalid rather than trusted.
_MIN_EPOCH_MS = int(datetime(2000, 1, 1, tzinfo=UTC).timestamp() * 1000)
_MAX_EPOCH_MS = int(datetime(2100, 1, 1, tzinfo=UTC).timestamp() * 1000)


def fahrenheit_to_celsius(value: float) -> float:
    return round((value - 32) * 5 / 9, 2)


def normalize_line_id(raw: str) -> str:
    """'A', 'line-a' and 'LINE-A' all mean LINE-A."""
    line = raw.strip().upper()
    return line if line.startswith("LINE-") else f"LINE-{line}"


def parse_iso_utc(value: Any) -> datetime:
    """ISO-8601 with an explicit offset ('2026-04-18T07:59:12Z'); converted to UTC."""
    if not isinstance(value, str) or "T" not in value:
        raise ValueError(f"not an ISO-8601 timestamp: {value!r}")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp has no timezone: {value!r}")
    return parsed.astimezone(UTC)


def parse_epoch_ms(value: Any) -> datetime:
    """Milliseconds since the Unix epoch, as a number or numeric string."""
    if isinstance(value, bool):
        raise ValueError("boolean is not a timestamp")
    millis = int(value) if isinstance(value, int) else int(str(value).strip())
    if not _MIN_EPOCH_MS <= millis < _MAX_EPOCH_MS:
        raise ValueError(f"epoch ms out of range: {millis}")
    return datetime.fromtimestamp(millis / 1000, tz=UTC)


def local_time_parser(fmt: str, tz: ZoneInfo) -> Callable[[Any], datetime]:
    """Parser for zone-less timestamps in a known format, interpreted in `tz`."""

    def parse(value: Any) -> datetime:
        if not isinstance(value, str):
            raise ValueError(f"not a timestamp string: {value!r}")
        return datetime.strptime(value.strip(), fmt).replace(tzinfo=tz).astimezone(UTC)

    return parse


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


class RecordReader:
    def __init__(self, record: dict[str, Any]):
        self.record = record
        self.reject_reasons: list[str] = []
        self.flags: list[str] = []
        self.attributes: dict[str, Any] = {}

    def get(self, *names: str) -> Any:
        """First non-blank value among field aliases (JSON vs CSV spellings), else None."""
        for name in names:
            value = self.record.get(name)
            if not _is_blank(value):
                return value.strip() if isinstance(value, str) else value
        return None

    def flag(self, code: str) -> None:
        if code not in self.flags:
            self.flags.append(code)

    # --- required fields: problems reject the record ---------------------------------

    def record_id(self, *names: str) -> str | None:
        value = self.get(*names)
        if value is None:
            self.reject_reasons.append(RejectReason.MISSING_RECORD_ID)
            return None
        return str(value)

    def machine(self, ctx: NormalizationContext, *names: str) -> tuple[str, MachineRef] | None:
        value = self.get(*names)
        if value is None:
            self.reject_reasons.append(RejectReason.MISSING_MACHINE_ID)
            return None
        machine_id = str(value).upper()
        ref = ctx.machines.get(machine_id)
        if ref is None:
            self.reject_reasons.append(RejectReason.UNKNOWN_MACHINE)
            return None
        return machine_id, ref

    def event_time(self, parse: Callable[[Any], datetime], *names: str) -> datetime | None:
        value = self.get(*names)
        if value is None:
            self.reject_reasons.append(RejectReason.MISSING_EVENT_TIME)
            return None
        try:
            return parse(value)
        except (ValueError, TypeError, OverflowError):
            self.reject_reasons.append(RejectReason.INVALID_EVENT_TIME)
            return None

    # --- location: reference data wins, disagreements are flagged ----------------------

    def location(self, ref: MachineRef, reported_plant: Any, reported_line: Any) -> tuple[str, str]:
        if reported_plant is not None and str(reported_plant).strip().upper() != ref.plant_id:
            self.flag("PLANT_MISMATCH")
            self.attributes["reported_plant_id"] = reported_plant
        if reported_line is None:
            self.flag("MISSING_LINE")
        elif normalize_line_id(str(reported_line)) != ref.line_id:
            self.flag("LINE_MISMATCH")
            self.attributes["reported_line_id"] = reported_line
        return ref.plant_id, ref.line_id

    # --- optional fields: problems become null + flag ----------------------------------

    def number(self, label: str, *names: str, lo: float, hi: float) -> float | None:
        value = self.get(*names)
        if value is None:
            return None
        try:
            if isinstance(value, bool):
                raise ValueError
            number = float(value)
        except (ValueError, TypeError):
            self.flag(f"INVALID_{label}")
            return None
        if not lo <= number <= hi:
            self.flag(f"{label}_OUT_OF_RANGE")
            return None
        return number

    def optional_time(self, label: str, parse: Callable[[Any], datetime], *names: str) -> datetime | None:
        value = self.get(*names)
        if value is None:
            return None
        try:
            return parse(value)
        except (ValueError, TypeError, OverflowError):
            self.flag(f"INVALID_{label}")
            return None

    def boolean(self, label: str, *names: str) -> bool | None:
        value = self.get(*names)
        if value is None or isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "1", "yes"):
            return True
        if text in ("false", "0", "no"):
            return False
        self.flag(f"INVALID_{label}")
        return None

    def code(self, label: str, value: Any, known: Mapping[str, Any]) -> Any:
        """Map a vendor code; unknown codes give None, keep the raw value and raise a flag."""
        if value is None:
            return None
        key = str(value).strip().lower()
        if key in known:
            return known[key]
        self.flag(f"UNKNOWN_{label}")
        self.attributes[f"vendor_{label.lower()}"] = value
        return None
