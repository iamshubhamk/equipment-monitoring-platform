"""What the ingestion layer needs to know about each source's batch envelope.

Only the envelope shape lives here (where the record list is, how records are
identified). Field-level mapping of each record belongs to the normalizers.
"""

from dataclasses import dataclass

from app.models import Vendor


@dataclass(frozen=True)
class VendorSpec:
    vendor: Vendor
    display_name: str
    # Key of the record list inside the batch envelope.
    records_key: str
    # Envelope field naming the sender, checked against the URL if present.
    name_field: str | None
    # Record id field; several spellings because the seed CSVs differ from the JSON API.
    id_fields: tuple[str, ...]


VENDORS: dict[Vendor, VendorSpec] = {
    spec.vendor: spec
    for spec in (
        VendorSpec(Vendor.PULSEFORGE, "PulseForge", "events", "vendor", ("event_id",)),
        VendorSpec(Vendor.THERMEXWATCH, "ThermexWatch", "readings", "source", ("readingId", "reading_id")),
        VendorSpec(Vendor.MAINTAFLOW, "MaintaFlow", "records", "provider_name", ("record_id",)),
        VendorSpec(Vendor.WORKORDERS, "WorkOrders", "work_orders", None, ("work_order_id",)),
    )
}


def source_record_id(spec: VendorSpec, record: object) -> str | None:
    """Best-effort id for traceability before validation; None if absent or blank."""
    if not isinstance(record, dict):
        return None
    for field in spec.id_fields:
        value = record.get(field)
        if value not in (None, ""):
            return str(value)[:128]
    return None
