"""API response models; these make up the OpenAPI contract."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models import BatchStatus, Vendor


class IngestionAccepted(BaseModel):
    ingestion_id: uuid.UUID
    vendor: Vendor
    status: BatchStatus
    record_count: int
    received_at: datetime
    idempotent_replay: bool = Field(
        description="True if this exact request was already accepted earlier; nothing new was stored."
    )
    status_url: str
