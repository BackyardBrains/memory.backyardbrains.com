"""Durable intake identity, visibility and indexing state."""
from alembic import op
import sqlalchemy as sa

revision = "d61015a1"
down_revision = "c4d9f8a2b731"
branch_labels = depends_on = None

def upgrade():
    for name in ("ingestion_key", "source_event_id", "event_type", "project_slug", "policy_scope", "index_error"):
        op.add_column("capture", sa.Column(name, sa.String(), nullable=True))
    op.add_column("capture", sa.Column("memory_visibility", sa.String(), nullable=False, server_default="knowledge"))
    op.add_column("capture", sa.Column("index_state", sa.String(), nullable=False, server_default="queued"))
    op.add_column("capture", sa.Column("index_updated_at", sa.DateTime(), nullable=True))
    op.create_index("ix_capture_ingestion_key", "capture", ["ingestion_key"])
    op.create_index("uq_capture_user_ingestion", "capture", ["user_id", "ingestion_key"], unique=True, postgresql_where=sa.text("ingestion_key IS NOT NULL"))
    for name in ("memory_visibility", "project_slug", "index_state"):
        op.create_index("ix_capture_"+name, "capture", [name])
    op.create_index("ix_chunk_capture_id", "chunk", ["capture_id"])
    op.execute("UPDATE capture SET index_state='ready' WHERE id IN (SELECT capture_id FROM chunk)")

def downgrade():
    op.drop_index("ix_chunk_capture_id", table_name="chunk")
    op.drop_index("uq_capture_user_ingestion", table_name="capture")
    for name in ("ingestion_key", "memory_visibility", "project_slug", "index_state"):
        op.drop_index("ix_capture_"+name, table_name="capture")
    for name in ("ingestion_key", "source_event_id", "event_type", "project_slug", "policy_scope", "index_error", "memory_visibility", "index_state", "index_updated_at"):
        op.drop_column("capture", name)
