from fastapi.testclient import TestClient

from app.main import create_app


def test_health_returns_ok():
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_dashboard_is_served_and_kept_out_of_the_api_contract():
    client = TestClient(create_app())

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>Machine Attention</title>" in response.text
    assert "/v1/summary" in response.text and "/v1/machines" in response.text
    assert "innerHTML" not in response.text  # vendor text is only ever set as textContent
    assert "/dashboard" not in client.get("/openapi.json").json()["paths"]


def test_committed_openapi_contract_is_up_to_date():
    from scripts.export_openapi import OUTPUT, render

    assert OUTPUT.read_text(encoding="utf-8") == render(), "run: python -m scripts.export_openapi"


def test_openapi_schema_is_served():
    client = TestClient(create_app())

    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Equipment Monitoring Service"
