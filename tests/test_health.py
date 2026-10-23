from fastapi.testclient import TestClient

from app.main import create_app


def test_health_returns_ok():
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_committed_openapi_contract_is_up_to_date():
    from scripts.export_openapi import OUTPUT, render

    assert OUTPUT.read_text(encoding="utf-8") == render(), "run: python -m scripts.export_openapi"


def test_openapi_schema_is_served():
    client = TestClient(create_app())

    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Equipment Monitoring Service"
