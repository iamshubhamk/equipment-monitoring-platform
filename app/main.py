from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api import dead_letters, ingest, ingestions, machines, summary

DASHBOARD = Path(__file__).parent / "static" / "dashboard.html"


def create_app() -> FastAPI:
    app = FastAPI(
        title="Equipment Monitoring Service",
        version="0.1.0",
        description="Ingests machine data from multiple vendors, normalizes it, "
        "and serves a per-machine attention view plus plant/line summaries.",
    )

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        """Liveness check: the process is up and serving requests."""
        return {"status": "ok"}

    @app.get("/dashboard", include_in_schema=False)
    def dashboard() -> FileResponse:
        """Read-only page over the read APIs; not part of the API contract."""
        return FileResponse(DASHBOARD, media_type="text/html")

    app.include_router(ingest.router)
    app.include_router(ingestions.router)
    app.include_router(machines.router)
    app.include_router(summary.router)
    app.include_router(dead_letters.router)
    return app


app = create_app()
