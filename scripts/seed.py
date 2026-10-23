"""Load the provided data into a running service through its public API.

    python scripts/seed.py                       # seed CSVs + vendor samples
    python scripts/seed.py --seed-only           # only the seed CSVs
    python scripts/seed.py --base-url http://api:8000   # from inside compose

Uses only the standard library, so it runs with any Python 3.12 without installing the
project. Everything goes through POST /v1/ingest/{vendor}, the same path a vendor uses,
then the script waits for the worker and prints the machine attention view.

Each batch has a fixed Idempotency-Key (e.g. "seed:pulseforge_events.csv"), so running
the script again changes nothing: the API answers with the original ingestion.
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parents[1] / "data"

# Where each source's batch envelope keeps its records (mirrors app/vendors.py; a test
# checks they agree).
RECORDS_KEY = {
    "pulseforge": "events",
    "thermexwatch": "readings",
    "maintaflow": "records",
    "workorders": "work_orders",
}
SEED_FILES = {
    "pulseforge_events.csv": "pulseforge",
    "thermexwatch_readings.csv": "thermexwatch",
    "maintaflow_records.csv": "maintaflow",
    "maintenance_work_orders.csv": "workorders",
}
MAX_RECORDS_PER_BATCH = 1000

# (method, path, json body or None, headers) -> (status code, parsed JSON body)
Send = Callable[[str, str, Any, dict[str, str]], tuple[int, Any]]


@dataclass(frozen=True)
class Batch:
    vendor: str
    payload: dict[str, Any]
    idempotency_key: str
    label: str


def build_batches(data_dir: Path = DATA_DIR, include_samples: bool = True) -> list[Batch]:
    """Seed CSVs first (one batch per file, split if large), then every sample file.

    Multi-vendor sample files are keyed by vendor and become one batch per vendor.
    """
    batches = []
    for name, vendor in SEED_FILES.items():
        with (data_dir / "seed_data" / name).open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        for start in range(0, len(rows), MAX_RECORDS_PER_BATCH):
            chunk = rows[start : start + MAX_RECORDS_PER_BATCH]
            batches.append(
                Batch(vendor, {RECORDS_KEY[vendor]: chunk}, f"seed:{name}:{start}", f"seed_data/{name}")
            )
    if include_samples:
        for path in sorted((data_dir / "vendor_api_samples").glob("*.json")):
            sample = json.loads(path.read_text(encoding="utf-8"))
            if set(sample) <= set(RECORDS_KEY):
                parts = list(sample.items())
            else:
                vendor = next(v for v, key in RECORDS_KEY.items() if key in sample)
                parts = [(vendor, sample)]
            for vendor, payload in parts:
                batches.append(Batch(vendor, payload, f"sample:{path.name}:{vendor}", f"{path.name} [{vendor}]"))
    return batches


def http_sender(base_url: str) -> Send:
    def send(method: str, path: str, body: Any, headers: dict[str, str]) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            base_url.rstrip("/") + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", **headers},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"null")

    return send


def api_key(vendor: str) -> str:
    return os.environ.get(f"API_KEY_{vendor.upper()}", f"dev-{vendor}-key")


def ingest_all(send: Send, batches: list[Batch], log: Callable[[str], None] = print) -> list[str]:
    """POST every batch. Returns the ingestion ids."""
    ids = []
    for batch in batches:
        headers = {"X-API-Key": api_key(batch.vendor), "Idempotency-Key": batch.idempotency_key}
        status, body = send("POST", f"/v1/ingest/{batch.vendor}", batch.payload, headers)
        if status != 202:
            raise RuntimeError(f"{batch.label}: HTTP {status} {body}")
        ids.append(body["ingestion_id"])
        note = " (already ingested)" if body["idempotent_replay"] else ""
        log(f"  {batch.label:<45} {body['record_count']:>3} records -> {body['ingestion_id']}{note}")
    return ids


def wait_until_processed(send: Send, ids: list[str], timeout: float, poll: float = 1.0) -> dict[str, Any]:
    """Wait until every batch is completed/failed and no machine has a pending recompute."""
    deadline = time.monotonic() + timeout
    while True:
        statuses = [send("GET", f"/v1/ingestions/{i}", None, {})[1]["status"] for i in ids]
        summary = send("GET", "/v1/summary", None, {})[1]
        if "queued" not in statuses and not summary["processing"]["pending_machines"]:
            return summary
        if time.monotonic() > deadline:
            raise TimeoutError(f"still processing after {timeout:.0f}s; is the worker running?")
        time.sleep(poll)


def print_report(send: Send, summary: dict[str, Any], log: Callable[[str], None] = print) -> None:
    machines = send("GET", "/v1/machines", None, {})[1]["items"]
    log("")
    log(f"{'machine':<8} {'line':<7} {'status':<18} {'lvl':>3}  reasons")
    for m in machines:
        log(f"{m['machine_id']:<8} {m['line_id']:<7} {m['derived_status']:<18} {m['attention_level']:>3}  {', '.join(m['reason_codes'])}")
    processing = summary["processing"]
    log("")
    log(f"status counts: {summary['status_counts']}")
    log(f"ingestions:    {processing['ingestions_by_status']} ({processing['ingestions_with_rejections']} with rejected records)")
    log(f"dead letters:  {processing['dead_letter_jobs']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--seed-only", action="store_true", help="skip vendor_api_samples/")
    parser.add_argument("--timeout", type=float, default=60, help="seconds to wait for the worker")
    args = parser.parse_args(argv)

    send = http_sender(args.base_url)
    batches = build_batches(include_samples=not args.seed_only)
    print(f"Posting {len(batches)} batches to {args.base_url}")
    try:
        ids = ingest_all(send, batches)
        print("Waiting for the worker...")
        summary = wait_until_processed(send, ids, args.timeout)
    except (urllib.error.URLError, RuntimeError, TimeoutError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print_report(send, summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
