import secrets
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from app.clock import Clock, utc_now
from app.config import Settings, get_settings
from app.models import Vendor


def _check_key(given: str | None, expected: str, what: str) -> None:
    # Constant-time comparison, so response timing doesn't leak the key.
    if given is None or not secrets.compare_digest(given, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=f"missing or invalid X-API-Key for {what}")


def require_vendor_key(
    vendor: Vendor,
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> Vendor:
    """Each source has its own key, so one vendor's key can't post as another vendor."""
    _check_key(x_api_key, settings.api_key_for(vendor), "this vendor")
    return vendor


def require_ops_key(
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    _check_key(x_api_key, settings.api_key_ops, "operations")


def get_clock() -> Clock:
    """Overridden in tests to pin "now" for freshness."""
    return utc_now


def get_stale_after(settings: Annotated[Settings, Depends(get_settings)]) -> timedelta:
    return timedelta(minutes=settings.stale_after_minutes)
