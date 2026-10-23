from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.deps import require_vendor_key
from app.config import Settings, get_settings
from app.db import get_session
from app.ingestion import BatchTooLarge, IdempotencyConflict, InvalidEnvelope, accept_batch
from app.models import Vendor
from app.schemas import IngestionAccepted
from app.vendors import VENDORS

router = APIRouter(prefix="/v1", tags=["ingestion"])


@router.post(
    "/ingest/{vendor}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IngestionAccepted,
    responses={
        401: {"description": "Missing or invalid X-API-Key"},
        409: {"description": "Idempotency-Key reused with a different payload"},
        413: {"description": "Too many records in one batch"},
        422: {"description": "Body is not a usable batch envelope"},
    },
)
def ingest(
    response: Response,
    vendor: Annotated[Vendor, Depends(require_vendor_key)],
    payload: Annotated[dict[str, Any], Body(description="The vendor's batch, in its own format.")],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    idempotency_key: Annotated[
        str | None,
        Header(
            max_length=128,
            description="Retries with the same key return the original ingestion. "
            "If omitted, the SHA-256 of the canonical JSON body is used.",
        ),
    ] = None,
) -> IngestionAccepted:
    """Accept a batch for background processing.

    Only the envelope is checked here. Records are validated by the worker, and the
    outcome per record is available from the status URL.
    """
    try:
        batch, replayed = accept_batch(
            session, VENDORS[vendor], payload, idempotency_key, settings.max_records_per_batch
        )
    except InvalidEnvelope as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except BatchTooLarge as exc:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)) from exc
    except IdempotencyConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    status_url = f"/v1/ingestions/{batch.id}"
    response.headers["Location"] = status_url
    return IngestionAccepted(
        ingestion_id=batch.id,
        vendor=batch.vendor,
        status=batch.status,
        record_count=batch.record_count,
        received_at=batch.received_at,
        idempotent_replay=replayed,
        status_url=status_url,
    )
