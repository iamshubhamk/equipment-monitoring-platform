from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models import Vendor


class Settings(BaseSettings):
    """All runtime configuration, read from environment variables (or a local .env)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/equipment"
    # Folder holding asset_reference.csv and line_reference.csv.
    data_dir: Path = Path("data")

    # One API key per source, sent as X-API-Key. The defaults are for local use only.
    api_key_pulseforge: str = "dev-pulseforge-key"
    api_key_thermexwatch: str = "dev-thermexwatch-key"
    api_key_maintaflow: str = "dev-maintaflow-key"
    api_key_workorders: str = "dev-workorders-key"

    # Larger batches are refused with 413; vendors should split them.
    max_records_per_batch: int = 1000

    # MaintaFlow timestamps carry no timezone; this is the zone they are read in.
    maintaflow_timezone: str = "UTC"

    def api_key_for(self, vendor: Vendor) -> str:
        return getattr(self, f"api_key_{vendor.value}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
