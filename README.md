# Equipment Monitoring Service

A backend service that ingests machine data from three vendors (PulseForge,
ThermexWatch, MaintaFlow) plus internal maintenance work orders, normalizes it into one
canonical event shape, processes it in the background, and serves a per-machine
"attention" view and a plant/line summary.

> Work in progress. Sections marked _TBD_ are filled in as each step lands.

## Stack

Python 3.12, FastAPI, PostgreSQL 16, SQLAlchemy 2 + Alembic, pytest, Docker Compose.

## Running locally

Requires Docker.

```bash
docker compose up --build
```

- API: http://localhost:8000
- OpenAPI docs: http://localhost:8000/docs
- Health check: http://localhost:8000/health

## Running tests

Inside Docker (no local Python needed):

```bash
docker compose run --rm api pytest
```

Or with a local Python 3.12 virtualenv (Postgres still comes from compose):

```bash
docker compose up -d db
python -m venv .venv
.venv/Scripts/activate    # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -e ".[dev]"
pytest
```

Integration tests use a separate `equipment_test` database on the same Postgres server
(created automatically, rebuilt from the migrations once per run, truncated after each
test). Override it with `TEST_DATABASE_URL`.

On startup the `api` container runs `alembic upgrade head` and then loads the reference
CSVs (`python -m app.reference_data`). Both steps are idempotent.

## Repository layout

| Path | Purpose |
|---|---|
| `app/` | Service code (API, normalization, jobs, derivation) |
| `tests/` | Unit and integration tests |
| `data/` | The provided assessment assets (reference CSVs, seed CSVs, vendor samples) |
| `docs/` | Architecture diagram and design note _(TBD)_ |

## Architecture

_TBD_

### Data model

| Table | Holds |
|---|---|
| `lines`, `machines` | Reference data from `line_reference.csv` / `asset_reference.csv` (rated limits, asset status) |
| `ingestion_batches` | Every accepted POST, stored verbatim, with its idempotency key and processing counts |
| `raw_records` | One row per record in a batch: `pending` / `accepted` / `rejected` / `duplicate`, plus reason codes |
| `events` | Canonical normalized events (UTC, Celsius, 0-4 severity), linked back to their raw record |
| `machine_states` | The derived attention view per machine, rebuilt from `events` |
| `jobs` | Postgres-backed job queue; `status = 'dead'` is the dead-letter list |

Data flows one way: raw → events → machine_states. Raw data is never modified, so any
derived table can be rebuilt from the layer below it.

## API

_TBD_

## Assumptions

Each assumption is recorded when the code that depends on it is written.

### Repository
- The provided assets are kept unchanged under `data/`, with the doubled
  `Aurik_Tech_Lead_Assessment_Assets/Aurik_Tech_Lead_Assessment_Assets/` folder
  flattened. The candidate brief PDF is not committed.
- `data/asset_reference.csv` and `data/seed_data/asset_reference.csv` are byte-identical
  (same for `line_reference.csv`); the top-level copies are treated as the source of truth.

### Data observations
- Vendor sample files that combine several vendors (`happy_path.json`, `duplicates.json`,
  `malformed.json`, `out_of_order.json`, `conflicting_updates.json`, `retry_cases.json`)
  are keyed by vendor. Tests and the seed script split them and send each part to its own
  vendor endpoint, because ingestion is per vendor.
- `maintenance_work_orders.csv` belongs to no vendor. It is treated as a fourth source,
  `workorders` (an internal maintenance system).

### Schema
- `asset_reference.csv` and `line_reference.csv` are the source of truth for which
  machines and lines exist, their plant/line, and their rated limits.
- Enum-like columns (vendor, statuses) are `VARCHAR` + `CHECK` constraints rather than
  native Postgres enums, so adding a value is a one-line migration.
- Freshness (FRESH/STALE) is not stored, because it depends on the current time; it is
  computed when the machine view is read.
- A single `jobs` table doubles as the dead-letter store (`status = 'dead'`); a separate
  table would add a copy step without adding information.
- At most one *queued* job per `dedupe_key` (a partial unique index), so many new
  events for one machine collapse into a single pending recompute.

## Trade-offs

_TBD_

## Limitations

_TBD_

## What I would do next for production

_TBD_

## AI usage disclosure

_TBD_
