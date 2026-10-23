"""Read APIs: ingestion status, machine views, summary and dead letters."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.api.deps import get_clock
from app.config import get_settings
from app.jobs.errors import PermanentError
from app.jobs.handlers import HANDLERS, Handler, normalize_batch_failed
from app.jobs.queue import NORMALIZE_BATCH, RECOMPUTE_MACHINE, enqueue
from app.jobs.worker import run_once
from app.models import Job, JobStatus, Vendor
from tests.samples import ALL_BATCHES, load_sample
from tests.test_worker import drain, ingest, make_due

# Shortly after the sample data: EQ-007's latest event (07:46:40) is over an hour old.
NOW = datetime(2026, 4, 18, 9, 0, tzinfo=UTC)
OPS = {"X-API-Key": get_settings().api_key_ops}


@pytest.fixture
def api(client):
    client.app.dependency_overrides[get_clock] = lambda: lambda: NOW
    return client


@pytest.fixture
def full_dataset(api, make_session, reference_data):
    for vendor, batch in ALL_BATCHES:
        ingest(api, vendor, batch)
    drain(make_session)


def get(api, path, **params):
    response = api.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


# --- ingestion status -----------------------------------------------------------------


def test_ingestion_status_before_processing(api, reference_data):
    ingestion_id = ingest(api, Vendor.PULSEFORGE, load_sample("pulseforge_sample.json"))

    body = get(api, f"/v1/ingestions/{ingestion_id}")

    assert body["status"] == "queued"
    assert (body["record_count"], body["pending_count"], body["accepted_count"]) == (2, 2, 0)
    assert (body["job"]["kind"], body["job"]["status"], body["job"]["attempts"]) == (NORMALIZE_BATCH, "queued", 0)
    assert body["records_url"] == f"/v1/ingestions/{ingestion_id}/records"


def test_ingestion_status_after_processing(api, make_session, reference_data):
    ingestion_id = ingest(api, Vendor.THERMEXWATCH, load_sample("malformed.json")["thermexwatch"])
    drain(make_session)

    body = get(api, f"/v1/ingestions/{ingestion_id}")

    assert body["status"] == "completed"
    assert (body["accepted_count"], body["rejected_count"], body["duplicate_count"], body["pending_count"]) == (1, 1, 0, 0)
    assert body["processed_at"] is not None
    assert (body["job"]["status"], body["job"]["attempts"], body["job"]["next_attempt_at"]) == ("succeeded", 1, None)


def test_ingestion_status_shows_retry_in_progress(api, make_session, reference_data):
    ingestion_id = ingest(api, Vendor.PULSEFORGE, load_sample("pulseforge_sample.json"))

    def broken(session, payload, now):
        raise RuntimeError("database unavailable")

    run_once(make_session, "w", handlers={NORMALIZE_BATCH: Handler(run=broken, on_failure=normalize_batch_failed)})
    body = get(api, f"/v1/ingestions/{ingestion_id}")

    assert body["status"] == "queued"
    assert body["error"] == "RuntimeError: database unavailable"
    assert body["job"]["attempts"] == 1
    assert body["job"]["next_attempt_at"] is not None


def test_rejected_records_are_listed_with_reasons(api, make_session, reference_data):
    ingestion_id = ingest(api, Vendor.PULSEFORGE, load_sample("malformed.json")["pulseforge"])
    drain(make_session)

    body = get(api, f"/v1/ingestions/{ingestion_id}/records", status="rejected")

    assert body["total"] == 2
    assert [(r["source_record_id"], r["reason_codes"]) for r in body["items"]] == [
        ("PF-1201", ["MISSING_MACHINE_ID"]),
        ("PF-1202", ["INVALID_EVENT_TIME"]),
    ]
    assert body["items"][1]["raw"]["event_time"] == "18-04-2026 08:28:20"  # exactly as sent


def test_duplicate_records_link_to_the_original_event(api, make_session, reference_data):
    ingestion_id = ingest(api, Vendor.THERMEXWATCH, load_sample("duplicates.json")["thermexwatch"])
    drain(make_session)

    records = get(api, f"/v1/ingestions/{ingestion_id}/records")["items"]

    original, resend = records
    assert resend["status"] == "duplicate"
    assert resend["reason_codes"] == ["DUPLICATE_CONTENT"]
    assert resend["event_id"] == original["event_id"]


def test_records_are_paginated(api, make_session, reference_data):
    ingestion_id = ingest(api, Vendor.PULSEFORGE, load_sample("pulseforge_sample.json"))

    body = get(api, f"/v1/ingestions/{ingestion_id}/records", limit=1, offset=1)

    assert body["total"] == 2
    assert [r["source_record_id"] for r in body["items"]] == ["PF-1002"]


def test_unknown_ingestion_is_404(api):
    assert api.get("/v1/ingestions/00000000-0000-0000-0000-000000000000").status_code == 404


# --- machine views --------------------------------------------------------------------


def test_machine_list_is_most_urgent_first(api, full_dataset):
    body = get(api, "/v1/machines")

    assert body["count"] == 8
    assert body["as_of"] == "2026-04-18T09:00:00Z"
    assert [m["machine_id"] for m in body["items"]][:3] == ["EQ-002", "EQ-005", "EQ-008"]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"needs_attention": "true"}, ["EQ-002", "EQ-005", "EQ-008", "EQ-001"]),
        ({"line_id": "LINE-E"}, ["EQ-008", "EQ-007"]),
        ({"plant_id": "PLANT_01", "status": "NORMAL"}, ["EQ-003", "EQ-004"]),
        ({"status": "UNDER_MAINTENANCE"}, ["EQ-007"]),
    ],
)
def test_machine_list_filters(api, full_dataset, params, expected):
    assert [m["machine_id"] for m in get(api, "/v1/machines", **params)["items"]] == expected


def test_machine_view_has_the_expected_output_fields(api, full_dataset):
    eq002 = get(api, "/v1/machines/EQ-002")

    assert (eq002["plant_id"], eq002["line_id"]) == ("PLANT_01", "LINE-A")
    assert (eq002["derived_status"], eq002["needs_attention"], eq002["attention_level"]) == ("CRITICAL", True, 4)
    assert eq002["reason_codes"] == ["TEMPERATURE_ALERT", "TEMPERATURE_ABOVE_RATED", "MAINTENANCE_OVERDUE", "OPEN_WORK_ORDER"]
    assert eq002["latest_relevant_event_time"] == "2026-04-18T08:49:00Z"  # MF-901 overdue
    assert (eq002["freshness"], eq002["processing_status"]) == ("FRESH", "UP_TO_DATE")
    assert eq002["last_processed_at"] is not None
    refs = {r["source_record_id"] for r in eq002["source_event_refs"]}
    assert {"TW-8803", "MF-901", "WO-1001:open"} <= refs
    temperature = eq002["reasons"][0]
    assert temperature["source_event_refs"][0]["source_record_id"] == "TW-8803"
    assert temperature["message"] == "thermexwatch reported TEMP_CRIT (severity 4)"


def test_freshness_depends_on_the_clock(api, full_dataset):
    assert get(api, "/v1/machines/EQ-007")["freshness"] == "STALE"  # latest event 07:46:40
    assert get(api, "/v1/machines/EQ-001")["freshness"] == "FRESH"  # latest event 08:32:05

    api.app.dependency_overrides[get_clock] = lambda: lambda: datetime(2026, 4, 18, 12, 0, tzinfo=UTC)

    assert get(api, "/v1/machines/EQ-001")["freshness"] == "STALE"


def test_machine_without_events_is_unknown(api, reference_data):
    eq003 = get(api, "/v1/machines/EQ-003")

    assert (eq003["derived_status"], eq003["processing_status"], eq003["freshness"]) == ("UNKNOWN", "NO_DATA", "STALE")
    assert (eq003["needs_attention"], eq003["reasons"], eq003["event_count"]) == (False, [], 0)


def test_machine_with_queued_recompute_is_pending(api, make_session, reference_data):
    ingest(api, Vendor.PULSEFORGE, load_sample("pulseforge_sample.json"))
    run_once(make_session, "w")  # normalized; recompute still queued

    assert get(api, "/v1/machines/EQ-001")["processing_status"] == "PENDING"


def test_machine_whose_recompute_died_is_failed(api, make_session, reference_data):
    ingest(api, Vendor.PULSEFORGE, load_sample("pulseforge_sample.json"))

    def broken(session, payload, now):
        raise PermanentError("rules crashed")

    drain(make_session, handlers={**HANDLERS, RECOMPUTE_MACHINE: Handler(run=broken)})

    eq001 = get(api, "/v1/machines/EQ-001")
    assert (eq001["processing_status"], eq001["derived_status"]) == ("FAILED", "UNKNOWN")


def test_machine_events_newest_first_and_traceable(api, full_dataset):
    body = get(api, "/v1/machines/EQ-001/events", limit=3)

    assert body["total"] == 7  # PF-1001, PF-1008, TW-8801, MF-501, PF-1101, PF-1301, PF-1302
    assert [e["source_record_id"] for e in body["items"]] == ["PF-1302", "PF-1301", "PF-1101"]
    assert all(e["ingestion_id"] and e["raw_record_id"] for e in body["items"])
    assert body["items"][0]["event_type"] == "recovery"
    assert body["items"][0]["attributes"]["vendor_event_type"] == "RECOVERY_SIGNAL"


def test_event_quality_flags_are_exposed(api, full_dataset):
    events = get(api, "/v1/machines/EQ-008/events")["items"]

    tw9912 = next(e for e in events if e["source_record_id"] == "TW-9912")
    assert tw9912["quality_flags"] == ["LINE_MISMATCH", "UNKNOWN_EVENT_TYPE"]


def test_unknown_machine_is_404(api, reference_data):
    assert api.get("/v1/machines/EQ-999").status_code == 404
    assert api.get("/v1/machines/EQ-999/events").status_code == 404


# --- summary --------------------------------------------------------------------------


def test_summary_answers_the_plant_questions(api, full_dataset):
    body = get(api, "/v1/summary")

    # How many machines are in each status?
    assert body["status_counts"] == {
        "NORMAL": 3,
        "WATCH": 1,
        "WARNING": 2,
        "CRITICAL": 1,
        "UNDER_MAINTENANCE": 1,
        "UNKNOWN": 0,
    }
    assert (body["machine_count"], body["needs_attention_count"]) == (8, 4)
    # Which lines have the most machines needing attention?
    assert [(line["line_id"], line["needs_attention_count"]) for line in body["lines"]] == [
        ("LINE-A", 2),
        ("LINE-D", 1),
        ("LINE-E", 1),
        ("LINE-B", 0),
        ("LINE-C", 0),
    ]
    assert body["lines"][0]["machines_needing_attention"] == ["EQ-001", "EQ-002"]
    assert body["lines"][0]["line_name"] == "Assembly"
    # Which machines are critical?
    assert [m["machine_id"] for m in body["critical_machines"]] == ["EQ-002"]
    # Any stale, failed or partially processed state?
    processing = body["processing"]
    assert processing["stale_machines"] == ["EQ-007"]
    assert (processing["pending_machines"], processing["failed_machines"]) == ([], [])
    assert processing["ingestions_by_status"] == {"queued": 0, "completed": 21, "failed": 0}
    assert processing["ingestions_with_rejections"] == 4  # both vendor batches of malformed.json and retry_cases.json
    assert processing["dead_letter_jobs"] == 0


def test_summary_per_plant(api, full_dataset):
    body = get(api, "/v1/summary", plant_id="PLANT_02")

    assert body["machine_count"] == 4
    assert [p["plant_id"] for p in body["plants"]] == ["PLANT_02"]
    assert {line["line_id"] for line in body["lines"]} == {"LINE-D", "LINE-E"}
    assert body["critical_machines"] == []


# --- dead letters ---------------------------------------------------------------------


def kill_normalize_job(api, make_session) -> str:
    ingestion_id = ingest(api, Vendor.PULSEFORGE, load_sample("pulseforge_sample.json"))

    def broken(session, payload, now):
        raise PermanentError("vendor schema changed")

    run_once(make_session, "w", handlers={NORMALIZE_BATCH: Handler(run=broken, on_failure=normalize_batch_failed)})
    return ingestion_id


def test_dead_letters_are_listed(api, make_session, reference_data):
    ingestion_id = kill_normalize_job(api, make_session)

    body = get(api, "/v1/dead-letters")

    assert len(body) == 1
    assert body[0]["kind"] == NORMALIZE_BATCH
    assert body[0]["payload"] == {"ingestion_id": ingestion_id}
    assert body[0]["last_error"] == "PermanentError: vendor schema changed"
    assert get(api, f"/v1/ingestions/{ingestion_id}")["status"] == "failed"


def test_replay_reprocesses_a_dead_batch(api, make_session, reference_data):
    ingestion_id = kill_normalize_job(api, make_session)
    job_id = get(api, "/v1/dead-letters")[0]["job_id"]

    response = api.post(f"/v1/dead-letters/{job_id}/replay", headers=OPS)

    assert response.status_code == 202, response.text
    assert (response.json()["status"], response.json()["attempts"]) == ("queued", 0)
    assert get(api, f"/v1/ingestions/{ingestion_id}")["status"] == "queued"

    make_due(make_session)
    drain(make_session)  # the real handler this time

    assert get(api, f"/v1/ingestions/{ingestion_id}")["status"] == "completed"
    assert get(api, "/v1/dead-letters") == []
    assert get(api, "/v1/machines/EQ-002")["derived_status"] == "CRITICAL"


def test_replay_requires_the_ops_key(api, make_session, reference_data):
    kill_normalize_job(api, make_session)
    job_id = get(api, "/v1/dead-letters")[0]["job_id"]
    vendor_key = {"X-API-Key": get_settings().api_key_for(Vendor.PULSEFORGE)}

    assert api.post(f"/v1/dead-letters/{job_id}/replay").status_code == 401
    assert api.post(f"/v1/dead-letters/{job_id}/replay", headers=vendor_key).status_code == 401


def test_replay_of_a_job_that_is_not_dead_is_a_conflict(api, make_session, reference_data):
    ingest(api, Vendor.PULSEFORGE, load_sample("pulseforge_sample.json"))
    with make_session() as session:
        job_id = session.scalar(select(Job.id).where(Job.kind == NORMALIZE_BATCH))

    assert api.post(f"/v1/dead-letters/{job_id}/replay", headers=OPS).status_code == 409


def test_replay_is_refused_when_an_equivalent_job_is_queued(api, make_session, reference_data):
    with make_session() as session:
        dead = enqueue(session, RECOMPUTE_MACHINE, {"machine_id": "EQ-001"})
        dead.dedupe_key, dead.status = "recompute:EQ-001", JobStatus.DEAD
        queued = enqueue(session, RECOMPUTE_MACHINE, {"machine_id": "EQ-001"})
        queued.dedupe_key = "recompute:EQ-001"
        session.commit()

    response = api.post(f"/v1/dead-letters/{dead.id}/replay", headers=OPS)

    assert response.status_code == 409
    assert "already queued" in response.json()["detail"]


def test_replay_of_unknown_job_is_404(api):
    assert api.post("/v1/dead-letters/999999/replay", headers=OPS).status_code == 404


# --- contract -------------------------------------------------------------------------


def test_openapi_documents_every_endpoint(api):
    paths = api.get("/openapi.json").json()["paths"]

    assert set(paths) >= {
        "/v1/ingest/{vendor}",
        "/v1/ingestions/{ingestion_id}",
        "/v1/ingestions/{ingestion_id}/records",
        "/v1/machines",
        "/v1/machines/{machine_id}",
        "/v1/machines/{machine_id}/events",
        "/v1/summary",
        "/v1/dead-letters",
        "/v1/dead-letters/{job_id}/replay",
    }
