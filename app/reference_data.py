"""Load line and machine reference data from the provided CSVs.

Idempotent (upsert by primary key), so it is safe to run on every startup:
    python -m app.reference_data
"""

import csv
from datetime import date
from pathlib import Path

from sqlalchemy import Table
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Line, Machine


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _upsert(session: Session, table: Table, rows: list[dict], key: str) -> None:
    if not rows:
        return
    stmt = insert(table).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[key],
        set_={col: stmt.excluded[col] for col in rows[0] if col != key},
    )
    session.execute(stmt)


def load_reference_data(session: Session, data_dir: Path) -> tuple[int, int]:
    """Upsert lines then machines (machines reference lines). Returns (lines, machines)."""
    lines = _read_csv(data_dir / "line_reference.csv")
    machines = [
        {
            **row,
            "installed_date": date.fromisoformat(row["installed_date"]),
            "rated_max_temp_c": float(row["rated_max_temp_c"]),
            "rated_max_vibration_mm_s": float(row["rated_max_vibration_mm_s"]),
            "baseline_power_kw": float(row["baseline_power_kw"]),
        }
        for row in _read_csv(data_dir / "asset_reference.csv")
    ]
    _upsert(session, Line.__table__, lines, "line_id")
    _upsert(session, Machine.__table__, machines, "machine_id")
    session.commit()
    return len(lines), len(machines)


if __name__ == "__main__":
    from app.db import SessionLocal

    with SessionLocal() as session:
        n_lines, n_machines = load_reference_data(session, get_settings().data_dir)
    print(f"reference data loaded: {n_lines} lines, {n_machines} machines")
