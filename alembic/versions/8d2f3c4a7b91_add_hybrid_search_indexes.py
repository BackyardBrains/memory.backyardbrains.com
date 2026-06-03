"""add hybrid search indexes

Revision ID: 8d2f3c4a7b91
Revises: 2cea16ccac13
Create Date: 2026-06-03 23:20:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "8d2f3c4a7b91"
down_revision: Union[str, Sequence[str], None] = "2cea16ccac13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chunk_user_content_trgm
        ON chunk
        USING gin (content gin_trgm_ops)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chunk_content_fts_simple
        ON chunk
        USING gin (to_tsvector('simple', coalesce(content, '')))
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_capture_user_raw_content_trgm
        ON capture
        USING gin (raw_content gin_trgm_ops)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_capture_raw_content_fts_simple
        ON capture
        USING gin (to_tsvector('simple', coalesce(raw_content, '')))
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_capture_raw_content_fts_simple")
    op.execute("DROP INDEX IF EXISTS idx_capture_user_raw_content_trgm")
    op.execute("DROP INDEX IF EXISTS idx_chunk_content_fts_simple")
    op.execute("DROP INDEX IF EXISTS idx_chunk_user_content_trgm")
