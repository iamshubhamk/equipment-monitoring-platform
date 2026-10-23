from fastapi import FastAPI


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

    return app


app = create_app()
