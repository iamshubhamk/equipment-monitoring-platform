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

Or with a local Python 3.12 virtualenv:

```bash
python -m venv .venv
.venv/Scripts/activate    # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -e ".[dev]"
pytest
```

## Repository layout

| Path | Purpose |
|---|---|
| `app/` | Service code (API, normalization, jobs, derivation) |
| `tests/` | Unit and integration tests |
| `data/` | The provided assessment assets (reference CSVs, seed CSVs, vendor samples) |
| `docs/` | Architecture diagram and design note _(TBD)_ |

## Architecture

_TBD_

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

## Trade-offs

_TBD_

## Limitations

_TBD_

## What I would do next for production

_TBD_

## AI usage disclosure

_TBD_
