"""drop unused 'processing' batch status

A batch is normalized in a single transaction, so the intermediate 'processing'
status was never visible. Retry progress is reported from the batch's job instead.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-24
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME = op.f("ck_ingestion_batches_batch_status")


def upgrade() -> None:
    op.execute("UPDATE ingestion_batches SET status = 'queued' WHERE status = 'processing'")
    op.drop_constraint(NAME, "ingestion_batches", type_="check")
    op.create_check_constraint(NAME, "ingestion_batches", "status IN ('queued', 'completed', 'failed')")


def downgrade() -> None:
    op.drop_constraint(NAME, "ingestion_batches", type_="check")
    op.create_check_constraint(
        NAME, "ingestion_batches", "status IN ('queued', 'processing', 'completed', 'failed')"
    )
