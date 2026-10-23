from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration, read from environment variables (or a local .env)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/equipment"
    # Folder holding asset_reference.csv and line_reference.csv.
    data_dir: Path = Path("data")


@lru_cache
def get_settings() -> Settings:
    return Settings()
