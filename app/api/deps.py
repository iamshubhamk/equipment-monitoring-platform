import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from app.config import Settings, get_settings
from app.models import Vendor


def require_vendor_key(
    vendor: Vendor,
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> Vendor:
    """Each source has its own key, so one vendor's key can't post as another vendor."""
    if x_api_key is None or not secrets.compare_digest(x_api_key, settings.api_key_for(vendor)):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid X-API-Key for this vendor",
        )
    return vendor
