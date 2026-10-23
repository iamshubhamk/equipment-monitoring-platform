# Architecture

Three processes and one database. The API accepts and serves; the worker does everything
slow or failure-prone; Postgres holds the data, the queue and the derived views.

```mermaid
flowchart LR
    subgraph Vendors
        PF[PulseForge]
        TW[ThermexWatch]
        MF[MaintaFlow]
        WO[Work orders]
    end

    subgraph API["api (FastAPI)"]
        ING["POST /v1/ingest/{vendor}<br/>auth, idempotency,<br/>envelope check"]
        READ["GET /v1/machines, /v1/summary,<br/>/v1/ingestions, /v1/dead-letters"]
    end

    subgraph DB["Postgres 16"]
        RAW[("ingestion_batches<br/>raw_records")]
        JOBS[("jobs<br/>(queue + dead letters)")]
        EV[("events")]
        MS[("machine_states")]
        REF[("machines, lines<br/>(reference data)")]
    end

    subgraph Worker["worker"]
        NB["normalize_batch<br/>normalize, reject/flag,<br/>dedupe"]
        RC["recompute_machine<br/>pure derive_state()"]
    end

    PF & TW & MF & WO -->|batch + X-API-Key| ING
    ING -->|one transaction| RAW
    ING -->|one transaction| JOBS
    JOBS -->|SKIP LOCKED claim| NB
    NB --> EV
    NB -->|one job per touched machine| JOBS
    JOBS -->|SKIP LOCKED claim| RC
    REF -.-> NB
    REF -.-> RC
    EV --> RC
    RC --> MS
    MS --> READ
    RAW --> READ
    JOBS --> READ
```

## Lifecycle of a batch

```mermaid
sequenceDiagram
    participant V as Vendor
    participant A as API
    participant DB as Postgres
    participant W as Worker

    V->>A: POST /v1/ingest/thermexwatch (X-API-Key, Idempotency-Key)
    A->>DB: same key seen? (vendor, idempotency_key)
    alt retry of an accepted batch
        A-->>V: 202, original ingestion_id, idempotent_replay=true
    else new batch
        A->>DB: INSERT batch + raw records + normalize_batch job (one transaction)
        A-->>V: 202 ingestion_id, status_url
    end

    W->>DB: claim job (FOR UPDATE SKIP LOCKED), commit
    W->>DB: per record: normalize -> reject / duplicate / insert event
    W->>DB: queue recompute_machine per machine with new events
    W->>DB: mark batch completed + job succeeded (one transaction)

    W->>DB: claim recompute_machine, lock machine row
    W->>DB: load ALL events for the machine, derive_state(), upsert machine_states

    V->>A: GET /v1/ingestions/{id}
    A-->>V: counts, rejected records, job state
```

If any step in the worker raises, its transaction rolls back completely and the job is
retried with backoff (2s, 4s, 8s, 16s). After 5 attempts, or on a `PermanentError`, the
job is dead-lettered and can be replayed with `POST /v1/dead-letters/{id}/replay`.

## Data layers

```mermaid
flowchart LR
    A["raw_records<br/><i>as sent, never changed</i>"] -->|normalizers| B["events<br/><i>UTC, °C, 0-4 severity,<br/>vendor-neutral types</i>"]
    B -->|derive_state| C["machine_states<br/><i>status, reasons,<br/>source event refs</i>"]
```

Each layer is computed only from the layer before it. That gives three properties:

- **Traceable:** every reason in a machine view points to events, every event to its raw
  record, every raw record to its ingestion batch.
- **Rebuildable:** a fixed normalizer can re-run over stored raw records; a changed rule can
  recompute every machine from its events.
- **Order-independent:** `derive_state` reads *all* of a machine's events sorted by event
  time, so arrival order never matters (tested by ingesting the full dataset forwards and
  backwards).

## Code map

| Path | Responsibility |
|---|---|
| `app/api/` | HTTP layer only: auth, request parsing, status codes |
| `app/ingestion.py` | Envelope check, idempotency, storing a batch and its job |
| `app/normalization/` | One pure normalizer per source + shared parsing helpers |
| `app/jobs/` | Queue (claim, retry, dead-letter), handlers, worker loop |
| `app/derivation/rules.py` | Pure attention rules: events + machine → state |
| `app/derivation/service.py` | Loads events, runs the rules, stores the result |
| `app/views.py` | Read-side queries for machine views and the summary |
| `app/models.py` | Database schema (migrations in `alembic/`) |

The two pure modules, `normalization/` and `derivation/rules.py`, hold the business logic
and are unit-tested without a database. Everything around them is plumbing.
