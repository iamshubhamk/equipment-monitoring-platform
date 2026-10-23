"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-10-24
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('ingestion_batches',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('vendor', sa.Enum('pulseforge', 'thermexwatch', 'maintaflow', 'workorders', name='vendor', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('idempotency_key', sa.String(length=128), nullable=False),
    sa.Column('payload_sha256', sa.String(length=64), nullable=False),
    sa.Column('raw_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.Enum('queued', 'processing', 'completed', 'failed', name='batch_status', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('record_count', sa.Integer(), nullable=False),
    sa.Column('accepted_count', sa.Integer(), nullable=False),
    sa.Column('rejected_count', sa.Integer(), nullable=False),
    sa.Column('duplicate_count', sa.Integer(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ingestion_batches')),
    sa.UniqueConstraint('vendor', 'idempotency_key', name=op.f('uq_ingestion_batches_vendor_idempotency_key'))
    )
    op.create_table('jobs',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.String(length=64), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('dedupe_key', sa.String(length=128), nullable=True),
    sa.Column('status', sa.Enum('queued', 'running', 'succeeded', 'dead', name='job_status', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('run_after', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('locked_by', sa.String(length=64), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_jobs'))
    )
    op.create_index('ix_jobs_queued_due', 'jobs', ['run_after'], unique=False, postgresql_where=sa.text("status = 'queued'"))
    op.create_index(op.f('ix_jobs_status'), 'jobs', ['status'], unique=False)
    op.create_index('uq_jobs_queued_dedupe_key', 'jobs', ['dedupe_key'], unique=True, postgresql_where=sa.text("status = 'queued' AND dedupe_key IS NOT NULL"))
    op.create_table('lines',
    sa.Column('line_id', sa.String(length=32), nullable=False),
    sa.Column('plant_id', sa.String(length=32), nullable=False),
    sa.Column('line_name', sa.String(length=64), nullable=False),
    sa.Column('operating_window', sa.String(length=32), nullable=False),
    sa.PrimaryKeyConstraint('line_id', name=op.f('pk_lines'))
    )
    op.create_index(op.f('ix_lines_plant_id'), 'lines', ['plant_id'], unique=False)
    op.create_table('machines',
    sa.Column('machine_id', sa.String(length=32), nullable=False),
    sa.Column('plant_id', sa.String(length=32), nullable=False),
    sa.Column('line_id', sa.String(length=32), nullable=False),
    sa.Column('machine_type', sa.String(length=64), nullable=False),
    sa.Column('criticality', sa.String(length=16), nullable=False),
    sa.Column('installed_date', sa.Date(), nullable=False),
    sa.Column('rated_max_temp_c', sa.Float(), nullable=False),
    sa.Column('rated_max_vibration_mm_s', sa.Float(), nullable=False),
    sa.Column('baseline_power_kw', sa.Float(), nullable=False),
    sa.Column('asset_status', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['line_id'], ['lines.line_id'], name=op.f('fk_machines_line_id_lines')),
    sa.PrimaryKeyConstraint('machine_id', name=op.f('pk_machines'))
    )
    op.create_index(op.f('ix_machines_line_id'), 'machines', ['line_id'], unique=False)
    op.create_index(op.f('ix_machines_plant_id'), 'machines', ['plant_id'], unique=False)
    op.create_table('raw_records',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('batch_id', sa.UUID(), nullable=False),
    sa.Column('record_index', sa.Integer(), nullable=False),
    sa.Column('source_record_id', sa.String(length=128), nullable=True),
    sa.Column('raw', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.Enum('pending', 'accepted', 'rejected', 'duplicate', name='record_status', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('reason_codes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('event_id', sa.BigInteger(), nullable=True),
    sa.ForeignKeyConstraint(['batch_id'], ['ingestion_batches.id'], name=op.f('fk_raw_records_batch_id_ingestion_batches'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_raw_records')),
    sa.UniqueConstraint('batch_id', 'record_index', name=op.f('uq_raw_records_batch_id_record_index'))
    )
    op.create_index(op.f('ix_raw_records_batch_id'), 'raw_records', ['batch_id'], unique=False)
    op.create_table('events',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('vendor', sa.Enum('pulseforge', 'thermexwatch', 'maintaflow', 'workorders', name='vendor', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('source_record_id', sa.String(length=128), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('raw_record_id', sa.BigInteger(), nullable=False),
    sa.Column('machine_id', sa.String(length=32), nullable=False),
    sa.Column('plant_id', sa.String(length=32), nullable=False),
    sa.Column('line_id', sa.String(length=32), nullable=False),
    sa.Column('event_time', sa.DateTime(timezone=True), nullable=False),
    sa.Column('vendor_received_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('event_type', sa.String(length=64), nullable=False),
    sa.Column('severity', sa.SmallInteger(), nullable=True),
    sa.Column('temperature_c', sa.Float(), nullable=True),
    sa.Column('vibration_mm_s', sa.Float(), nullable=True),
    sa.Column('vibration_g', sa.Float(), nullable=True),
    sa.Column('power_kw', sa.Float(), nullable=True),
    sa.Column('sensor_health', sa.Float(), nullable=True),
    sa.Column('signal_quality', sa.String(length=16), nullable=True),
    sa.Column('machine_state', sa.String(length=32), nullable=True),
    sa.Column('machine_active', sa.Boolean(), nullable=True),
    sa.Column('vendor_confidence', sa.Float(), nullable=True),
    sa.Column('inspection_result', sa.String(length=64), nullable=True),
    sa.Column('maintenance_status', sa.String(length=32), nullable=True),
    sa.Column('work_order_status', sa.String(length=32), nullable=True),
    sa.Column('post_maintenance_check', sa.String(length=32), nullable=True),
    sa.Column('manual_confidence', sa.Float(), nullable=True),
    sa.Column('attributes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('quality_flags', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.ForeignKeyConstraint(['machine_id'], ['machines.machine_id'], name=op.f('fk_events_machine_id_machines')),
    sa.ForeignKeyConstraint(['raw_record_id'], ['raw_records.id'], name=op.f('fk_events_raw_record_id_raw_records')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_events')),
    sa.UniqueConstraint('vendor', 'source_record_id', name=op.f('uq_events_vendor_source_record_id'))
    )
    op.create_index('ix_events_machine_time', 'events', ['machine_id', 'event_time'], unique=False)
    op.create_index('ix_events_vendor_content_hash', 'events', ['vendor', 'content_hash'], unique=False)
    # raw_records <-> events reference each other, so this FK is added once both exist.
    op.create_foreign_key(op.f('fk_raw_records_event_id_events'), 'raw_records', 'events', ['event_id'], ['id'])
    op.create_table('machine_states',
    sa.Column('machine_id', sa.String(length=32), nullable=False),
    sa.Column('derived_status', sa.String(length=32), nullable=False),
    sa.Column('attention_level', sa.SmallInteger(), nullable=False),
    sa.Column('needs_attention', sa.Boolean(), nullable=False),
    sa.Column('reasons', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('source_event_refs', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('latest_relevant_event_time', sa.DateTime(timezone=True), nullable=True),
    sa.Column('latest_event_time', sa.DateTime(timezone=True), nullable=True),
    sa.Column('event_count', sa.Integer(), nullable=False),
    sa.Column('computed_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['machine_id'], ['machines.machine_id'], name=op.f('fk_machine_states_machine_id_machines')),
    sa.PrimaryKeyConstraint('machine_id', name=op.f('pk_machine_states'))
    )


def downgrade() -> None:
    op.drop_table('machine_states')
    op.drop_constraint(op.f('fk_raw_records_event_id_events'), 'raw_records', type_='foreignkey')
    op.drop_index('ix_events_vendor_content_hash', table_name='events')
    op.drop_index('ix_events_machine_time', table_name='events')
    op.drop_table('events')
    op.drop_index(op.f('ix_raw_records_batch_id'), table_name='raw_records')
    op.drop_table('raw_records')
    op.drop_index(op.f('ix_machines_plant_id'), table_name='machines')
    op.drop_index(op.f('ix_machines_line_id'), table_name='machines')
    op.drop_table('machines')
    op.drop_index(op.f('ix_lines_plant_id'), table_name='lines')
    op.drop_table('lines')
    op.drop_index('uq_jobs_queued_dedupe_key', table_name='jobs', postgresql_where=sa.text("status = 'queued' AND dedupe_key IS NOT NULL"))
    op.drop_index(op.f('ix_jobs_status'), table_name='jobs')
    op.drop_index('ix_jobs_queued_due', table_name='jobs', postgresql_where=sa.text("status = 'queued'"))
    op.drop_table('jobs')
    op.drop_table('ingestion_batches')
