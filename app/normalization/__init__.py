"""One pure normalizer per source: (raw record, batch envelope, context) -> result."""

from collections.abc import Callable
from typing import Any

from app.models import Vendor
from app.normalization import maintaflow, pulseforge, thermexwatch, workorders
from app.normalization.canonical import NormalizationContext, NormalizationResult

Normalizer = Callable[[Any, dict[str, Any], NormalizationContext], NormalizationResult]

NORMALIZERS: dict[Vendor, Normalizer] = {
    Vendor.PULSEFORGE: pulseforge.normalize,
    Vendor.THERMEXWATCH: thermexwatch.normalize,
    Vendor.MAINTAFLOW: maintaflow.normalize,
    Vendor.WORKORDERS: workorders.normalize,
}
