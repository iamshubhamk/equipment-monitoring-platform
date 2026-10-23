"""The seed script, driven through the real API via the test client instead of HTTP."""

import pytest
from sqlalchemy import func, select

from app.models import IngestionBatch
from app.vendors import VENDORS
from scripts import seed
from tests.samples import ALL_BATCHES
from tests.test_golden import EXPECTED_STATUS
from tests.test_worker import drain


def client_sender(client) -> seed.Send:
    def send(method, path, body, headers):
        response = client.request(method, path, json=body, headers=headers)
        return response.status_code, response.json()

    return send


def test_records_keys_match_the_service():
    assert seed.RECORDS_KEY == {v.value: spec.records_key for v, spec in VENDORS.items()}


def test_batches_cover_every_seed_csv_and_sample():
    batches = seed.build_batches()

    assert [(b.vendor, b.payload) for b in batches] == [(v.value, p) for v, p in ALL_BATCHES]
    assert len({b.idempotency_key for b in batches}) == len(batches) == 21


def test_seed_only_skips_the_samples():
    assert [b.label for b in seed.build_batches(include_samples=False)] == [
        "seed_data/pulseforge_events.csv",
        "seed_data/thermexwatch_readings.csv",
        "seed_data/maintaflow_records.csv",
        "seed_data/maintenance_work_orders.csv",
    ]


def test_seeding_produces_the_golden_states(client, make_session, reference_data):
    send = client_sender(client)
    lines = []

    ids = seed.ingest_all(send, seed.build_batches(), log=lines.append)
    drain(make_session)
    summary = seed.wait_until_processed(send, ids, timeout=5)
    seed.print_report(send, summary, log=lines.append)

    statuses = {m["machine_id"]: m["derived_status"] for m in send("GET", "/v1/machines", None, {})[1]["items"]}
    assert statuses == EXPECTED_STATUS
    assert any(line.startswith("EQ-002") and "CRITICAL" in line for line in lines)


def test_running_the_seed_twice_changes_nothing(client, make_session, reference_data):
    send = client_sender(client)
    batches = seed.build_batches()
    first = seed.ingest_all(send, batches, log=lambda _: None)
    lines = []

    second = seed.ingest_all(send, batches, log=lines.append)

    assert second == first
    assert all("(already ingested)" in line for line in lines)
    with make_session() as session:
        assert session.scalar(select(func.count()).select_from(IngestionBatch)) == 21


def test_rejected_batch_stops_the_seed(client, reference_data, monkeypatch):
    monkeypatch.setenv("API_KEY_PULSEFORGE", "wrong-key")

    with pytest.raises(RuntimeError, match="HTTP 401"):
        seed.ingest_all(client_sender(client), seed.build_batches(), log=lambda _: None)


def test_waiting_times_out_if_the_worker_is_not_running(client, reference_data):
    send = client_sender(client)
    ids = seed.ingest_all(send, seed.build_batches(include_samples=False), log=lambda _: None)

    with pytest.raises(TimeoutError, match="is the worker running"):
        seed.wait_until_processed(send, ids, timeout=0, poll=0)
