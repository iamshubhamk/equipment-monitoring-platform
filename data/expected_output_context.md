# Expected Output Context

The downstream system needs a machine operational attention view that can be consumed by product and backend teams.

## Minimum Output Expectations

A machine-level output should make it possible to understand:
- the latest derived operational status,
- whether the machine currently needs attention,
- the attention level or severity,
- key contributing reason codes,
- the latest relevant processed event time,
- processing freshness or status context,
- and traceability back to the source event or record identifiers.

## Minimum Conceptual Fields

A practical response will usually include equivalents of:
- `machine_id`
- `plant_id`
- `line_id`
- `derived_status`
- `attention_level`
- `reason_codes`
- `latest_relevant_event_time`
- `processing_status`
- `source_event_refs`
- `last_processed_at`

## Plant / Line Summary Expectations

A summary response should make it possible to answer:
- how many machines are currently in each derived status,
- which lines currently have the most machines requiring attention,
- which machines are currently critical,
- and whether any machines have stale, failed, or partially processed state.

## Implementation Note

You do not need to implement an ML model. Deterministic, explainable logic is preferred for this assessment.