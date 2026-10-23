"""Helpers for the provided vendor sample files.

Single-vendor files (pulseforge_sample.json, ...) are one batch. Multi-vendor files
(happy_path.json, ...) are keyed by vendor and are split into one batch per vendor,
because ingestion is per vendor.
"""

import csv
import json
from typing import Any

from app.models import Vendor
from app.vendors import VENDORS
from tests.conftest import DATA_DIR

SAMPLES_DIR = DATA_DIR / "vendor_api_samples"
SAMPLE_FILES = sorted(p.name for p in SAMPLES_DIR.glob("*.json"))


def load_sample(name: str) -> dict[str, Any]:
    return json.loads((SAMPLES_DIR / name).read_text(encoding="utf-8"))


def vendor_batches(name: str) -> list[tuple[Vendor, dict[str, Any]]]:
    sample = load_sample(name)
    if set(sample) <= {v.value for v in Vendor}:
        return [(Vendor(key), batch) for key, batch in sample.items()]
    vendor = next(v for v, spec in VENDORS.items() if spec.records_key in sample)
    return [(vendor, sample)]


ALL_SAMPLE_BATCHES = [
    (name, vendor, batch) for name in SAMPLE_FILES for vendor, batch in vendor_batches(name)
]

SEED_DIR = DATA_DIR / "seed_data"
SEED_FILES = {
    "pulseforge_events.csv": Vendor.PULSEFORGE,
    "thermexwatch_readings.csv": Vendor.THERMEXWATCH,
    "maintaflow_records.csv": Vendor.MAINTAFLOW,
    "maintenance_work_orders.csv": Vendor.WORKORDERS,
}


def load_seed_csv(name: str) -> list[dict[str, str]]:
    with (SEED_DIR / name).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# Everything provided: every seed CSV as one batch, then every sample batch.
ALL_BATCHES = [
    *[(vendor, {VENDORS[vendor].records_key: load_seed_csv(name)}) for name, vendor in SEED_FILES.items()],
    *[(vendor, batch) for _, vendor, batch in ALL_SAMPLE_BATCHES],
]
