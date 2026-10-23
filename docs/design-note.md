# Design note

## The problem in one line

Turn messy, duplicated, out-of-order data from several vendors into a per-machine answer
to "does this machine need attention, how badly, and why?", and be able to show the
evidence for every answer.

## Key decisions

**1. Store first, judge later.** The ingest endpoint checks only the batch envelope, stores
the raw batch and its records, and queues a job, all in one transaction. Every record is
validated later by the worker.
- One bad record never sinks a batch.
- Nothing a vendor sent is ever lost, and every rejection is recorded with a reason code.
- Accepted batches can be re-processed if a normalizer is fixed.

**2. Postgres is the queue.** A `jobs` table with `SELECT … FOR UPDATE SKIP LOCKED`.
- **Why not Kafka, Redis or Celery?** At this scale they would add infrastructure without
  adding capability.
- **What it buys:** a job is committed atomically with the data it refers to, so nothing
  can be stored without its job (or the reverse).
- **Retries and dead letters:** retry with exponential backoff, dead-letter after 5
  attempts or at once on a `PermanentError`, re-queue after a crash, and replay through the
  API.
- **One clock:** all queue timing uses the database clock, so workers on different hosts
  agree.

**3. Two levels of duplicate protection.**
- **Batch level:** an `Idempotency-Key`, or the hash of the canonical JSON body, makes
  client retries safe.
- **Record level:** same vendor + record id, then same vendor + content hash, catches
  re-sends like `TW-8801-R`.
- **Never across vendors:** two vendors reporting the same moment is corroboration, not a
  duplicate.

**4. Normalize to a vendor-neutral event, but keep the vendor's words.**
- Times become UTC, temperatures Celsius, severities a shared 0-4 scale, and alert codes
  vendor-neutral types (`TEMP_CRIT` → `temperature_alert`, severity 4). The vendor's
  original code stays in `attributes`.
- Only a missing or invalid id, machine or time rejects a record. A bad optional value
  becomes null with a quality flag; an unknown code is kept and flagged.
- ThermexWatch vibration in g is not converted to mm/s: the conversion needs a frequency
  the vendor doesn't send, and a made-up number is worse than a missing one.

**5. Derived state is a pure function of all events, ordered by event time.**
- `derive_state(machine, events)` never looks at arrival order, so out-of-order data and
  late arrivals are handled by construction rather than by special cases.
- Each dimension (vibration, temperature, power, sensor health, inspection, maintenance,
  work orders) uses only its latest evidence.
- A completed maintenance resets older evidence.
- The status is the highest severity, except that CRITICAL always wins and an asset in
  maintenance shows as UNDER_MAINTENANCE.
- Every reason carries the ids of the events behind it.

Recomputing from all events costs more than updating the state step by step, but it is
simple to reason about, idempotent, and easy to test. A machine has hundreds of events,
not millions; at that scale it is the right trade.

## How the golden cases fall out of the rules

| Case | What decides it |
|---|---|
| EQ-002 CRITICAL | TW-8803 `TEMP_CRIT` (level 5 → severity 4), 97 °C against a 90 °C rating |
| EQ-008 still needs attention | PF-1401's recovery (08:39) is newer than TW-9921's `TEMP_CRIT` (08:29), so temperature clears; but MF-801's major defect (08:41) and the open WO-1003 remain |
| EQ-001 WATCH | PF-1302's recovery is newer than PF-1301's alert, although it arrived first; MF-501's minor defect remains |
| EQ-004 NORMAL | WO-1002 closed and passed, then MF-701 `completed`, reset the older power alerts |
| EQ-007 UNDER_MAINTENANCE | asset status `maintenance`, plus an open work order on an inactive machine |

## Trade-offs I would revisit

- **Recompute from all events** → incremental state, or a time window, once a machine
  has a very long history.
- **Thresholds as code constants** → per machine type, stored as data, versioned
  alongside the results they produced.
- **Static per-vendor API keys** → keys from a secrets manager with rotation; read APIs
  behind real auth (they are open here, assumed internal).
- **Polling worker** → `LISTEN/NOTIFY` to wake workers immediately; a partitioned or
  archived `jobs` table once it grows.
- **First version wins for a re-sent id** → an explicit vendor correction protocol, if
  vendors ever send corrections.
