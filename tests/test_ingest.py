import copy

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.jobs.queue import NORMALIZE_BATCH
from app.models import IngestionBatch, Job, RawRecord, RecordStatus, Vendor
from app.vendors import VENDORS
from tests.samples import ALL_SAMPLE_BATCHES, load_sample

PULSEFORGE_BATCH = load_sample("pulseforge_sample.json")


def post(client, vendor: Vendor, payload, key: str | None = None, api_key: str | None = None):
    headers = {"X-API-Key": api_key or get_settings().api_key_for(vendor)}
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post(f"/v1/ingest/{vendor.value}", json=payload, headers=headers)


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


@pytest.mark.parametrize(
    ("sample", "vendor", "batch"),
    ALL_SAMPLE_BATCHES,
    ids=[f"{name}:{vendor.value}" for name, vendor, _ in ALL_SAMPLE_BATCHES],
)
def test_every_sample_batch_is_accepted_and_stored(client, db_session, sample, vendor, batch):
    # Malformed and partially bad batches are accepted too: records are judged by the worker.
    response = post(client, vendor, batch)

    assert response.status_code == 202, response.text
    body = response.json()
    expected_records = len(batch[VENDORS[vendor].records_key])
    assert body["record_count"] == expected_records
    assert body["status"] == "queued"
    assert body["idempotent_replay"] is False
    assert response.headers["Location"] == f"/v1/ingestions/{body['ingestion_id']}"

    stored = db_session.get(IngestionBatch, body["ingestion_id"])
    assert stored.vendor == vendor
    assert stored.raw_payload == batch
    records = db_session.scalars(select(RawRecord).order_by(RawRecord.record_index)).all()
    assert [r.raw for r in records] == batch[VENDORS[vendor].records_key]
    assert {r.status for r in records} == {RecordStatus.PENDING}
    job = db_session.scalar(select(Job))
    assert (job.kind, job.payload) == (NORMALIZE_BATCH, {"ingestion_id": body["ingestion_id"]})


def test_source_record_ids_are_captured_for_traceability(client, db_session):
    post(client, Vendor.THERMEXWATCH, load_sample("malformed.json")["thermexwatch"])

    ids = db_session.scalars(select(RawRecord.source_record_id).order_by(RawRecord.record_index)).all()
    assert ids == ["TW-9911", "TW-9912"]


def test_work_orders_can_be_ingested(client, db_session):
    batch = {
        "work_orders": [
            {"work_order_id": "WO-1002", "machine_id": "EQ-004", "status": "closed"},
        ]
    }

    response = post(client, Vendor.WORKORDERS, batch)

    assert response.status_code == 202, response.text
    assert db_session.scalar(select(RawRecord.source_record_id)) == "WO-1002"


# --- authentication -----------------------------------------------------------------


def test_missing_api_key_is_rejected(client, db_session):
    response = client.post("/v1/ingest/pulseforge", json=PULSEFORGE_BATCH)

    assert response.status_code == 401
    assert count(db_session, IngestionBatch) == 0


def test_another_vendors_key_is_rejected(client, db_session):
    other_key = get_settings().api_key_for(Vendor.THERMEXWATCH)

    response = post(client, Vendor.PULSEFORGE, PULSEFORGE_BATCH, api_key=other_key)

    assert response.status_code == 401
    assert count(db_session, IngestionBatch) == 0


# --- idempotency --------------------------------------------------------------------


def test_retry_with_same_idempotency_key_returns_original_ingestion(client, db_session):
    first = post(client, Vendor.PULSEFORGE, PULSEFORGE_BATCH, key="batch-123")
    retry = post(client, Vendor.PULSEFORGE, PULSEFORGE_BATCH, key="batch-123")

    assert retry.status_code == 202
    assert retry.json()["ingestion_id"] == first.json()["ingestion_id"]
    assert retry.json()["idempotent_replay"] is True
    assert count(db_session, IngestionBatch) == 1
    assert count(db_session, RawRecord) == 2
    assert count(db_session, Job) == 1


def test_same_idempotency_key_with_different_payload_is_a_conflict(client, db_session):
    changed = copy.deepcopy(PULSEFORGE_BATCH)
    changed["events"][0]["severity"] = "critical"

    post(client, Vendor.PULSEFORGE, PULSEFORGE_BATCH, key="batch-123")
    response = post(client, Vendor.PULSEFORGE, changed, key="batch-123")

    assert response.status_code == 409
    assert count(db_session, IngestionBatch) == 1


def test_without_idempotency_key_identical_body_is_deduplicated(client, db_session):
    # Same JSON with keys in a different order: the canonical hash is the same.
    reordered = dict(reversed(list(PULSEFORGE_BATCH.items())))

    first = post(client, Vendor.PULSEFORGE, PULSEFORGE_BATCH)
    second = post(client, Vendor.PULSEFORGE, reordered)

    assert second.json()["ingestion_id"] == first.json()["ingestion_id"]
    assert second.json()["idempotent_replay"] is True
    assert count(db_session, IngestionBatch) == 1


def test_idempotency_keys_are_scoped_per_vendor(client, db_session):
    thermex = load_sample("thermexwatch_sample.json")

    a = post(client, Vendor.PULSEFORGE, PULSEFORGE_BATCH, key="shared-key")
    b = post(client, Vendor.THERMEXWATCH, thermex, key="shared-key")

    assert b.status_code == 202
    assert a.json()["ingestion_id"] != b.json()["ingestion_id"]


# --- envelope validation ------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"vendor": "PulseForge"},  # no records list
        {"vendor": "PulseForge", "events": {"event_id": "PF-1"}},  # not a list
        {"vendor": "PulseForge", "events": []},  # empty
        {"vendor": "ThermexWatch", "events": [{"event_id": "PF-1"}]},  # wrong sender
    ],
    ids=["missing-records", "records-not-a-list", "empty-records", "sender-mismatch"],
)
def test_unusable_envelope_is_rejected(client, db_session, payload):
    response = post(client, Vendor.PULSEFORGE, payload)

    assert response.status_code == 422, response.text
    assert count(db_session, IngestionBatch) == 0


def test_body_must_be_a_json_object(client):
    response = post(client, Vendor.PULSEFORGE, [{"event_id": "PF-1"}])

    assert response.status_code == 422


def test_unknown_vendor_is_rejected(client):
    response = client.post("/v1/ingest/acme", json=PULSEFORGE_BATCH, headers={"X-API-Key": "x"})

    assert response.status_code == 422


def test_oversized_batch_is_rejected(client, db_session):
    limit = get_settings().max_records_per_batch
    batch = {"events": [{"event_id": f"PF-{i}"} for i in range(limit + 1)]}

    response = post(client, Vendor.PULSEFORGE, batch)

    assert response.status_code == 413
    assert count(db_session, IngestionBatch) == 0
