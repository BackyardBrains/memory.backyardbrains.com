"""add capture provenance fields

Revision ID: 2c9e47c6f0b8
Revises: 8d2f3c4a7b91
Create Date: 2026-06-04 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "2c9e47c6f0b8"
down_revision = "8d2f3c4a7b91"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("capture", sa.Column("source_system", sa.String(), nullable=True))
    op.add_column("capture", sa.Column("source_path", sa.String(), nullable=True))
    op.add_column("capture", sa.Column("source_type", sa.String(), nullable=True))
    op.add_column("capture", sa.Column("observed_at", sa.DateTime(), nullable=True))
    op.add_column("capture", sa.Column("imported_at", sa.DateTime(), nullable=True))
    op.add_column("capture", sa.Column("content_hash", sa.String(), nullable=True))
    op.add_column("capture", sa.Column("import_batch_id", sa.String(), nullable=True))
    op.add_column(
        "capture",
        sa.Column("historical_until_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_capture_source_system", "capture", ["source_system"])
    op.create_index("ix_capture_source_path", "capture", ["source_path"])
    op.create_index("ix_capture_observed_at", "capture", ["observed_at"])
    op.create_index("ix_capture_imported_at", "capture", ["imported_at"])
    op.create_index("ix_capture_content_hash", "capture", ["content_hash"])
    op.create_index("ix_capture_import_batch_id", "capture", ["import_batch_id"])
    op.create_index("ix_capture_historical_until_verified", "capture", ["historical_until_verified"])
    op.alter_column("capture", "historical_until_verified", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_capture_historical_until_verified", table_name="capture")
    op.drop_index("ix_capture_import_batch_id", table_name="capture")
    op.drop_index("ix_capture_content_hash", table_name="capture")
    op.drop_index("ix_capture_imported_at", table_name="capture")
    op.drop_index("ix_capture_observed_at", table_name="capture")
    op.drop_index("ix_capture_source_path", table_name="capture")
    op.drop_index("ix_capture_source_system", table_name="capture")
    op.drop_column("capture", "historical_until_verified")
    op.drop_column("capture", "import_batch_id")
    op.drop_column("capture", "content_hash")
    op.drop_column("capture", "imported_at")
    op.drop_column("capture", "observed_at")
    op.drop_column("capture", "source_type")
    op.drop_column("capture", "source_path")
    op.drop_column("capture", "source_system")
