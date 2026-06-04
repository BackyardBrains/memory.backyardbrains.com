"""add memory fact cards

Revision ID: 91d2b7c4e6f0
Revises: 2c9e47c6f0b8
Create Date: 2026-06-04 01:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector


revision: str = "91d2b7c4e6f0"
down_revision: Union[str, Sequence[str], None] = "2c9e47c6f0b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "memoryfactcard",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_capture_id", sa.Integer(), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("aliases_json", sa.String(), nullable=True),
        sa.Column("aliases_text", sa.String(), nullable=True),
        sa.Column("entities_json", sa.String(), nullable=True),
        sa.Column("project_slug", sa.String(), nullable=True),
        sa.Column("source_system", sa.String(), nullable=True),
        sa.Column("source_type", sa.String(), nullable=True),
        sa.Column("source_path", sa.String(), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=True),
        sa.Column("historical_status", sa.String(), nullable=True),
        sa.Column("memory_visibility", sa.String(), nullable=True),
        sa.Column("provenance_json", sa.String(), nullable=True),
        sa.Column("embedding", Vector(384), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["source_capture_id"], ["capture.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memoryfactcard_source_capture_id", "memoryfactcard", ["source_capture_id"])
    op.create_index("ix_memoryfactcard_aliases_text", "memoryfactcard", ["aliases_text"])
    op.create_index("ix_memoryfactcard_project_slug", "memoryfactcard", ["project_slug"])
    op.create_index("ix_memoryfactcard_source_system", "memoryfactcard", ["source_system"])
    op.create_index("ix_memoryfactcard_source_path", "memoryfactcard", ["source_path"])
    op.create_index("ix_memoryfactcard_observed_at", "memoryfactcard", ["observed_at"])
    op.create_index("ix_memoryfactcard_historical_status", "memoryfactcard", ["historical_status"])
    op.create_index("ix_memoryfactcard_memory_visibility", "memoryfactcard", ["memory_visibility"])
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memoryfactcard_content_trgm
        ON memoryfactcard
        USING gin (content gin_trgm_ops)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memoryfactcard_content_fts_simple
        ON memoryfactcard
        USING gin (to_tsvector('simple', coalesce(content, '') || ' ' || coalesce(aliases_text, '')))
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_memoryfactcard_content_fts_simple")
    op.execute("DROP INDEX IF EXISTS idx_memoryfactcard_content_trgm")
    op.drop_index("ix_memoryfactcard_memory_visibility", table_name="memoryfactcard")
    op.drop_index("ix_memoryfactcard_historical_status", table_name="memoryfactcard")
    op.drop_index("ix_memoryfactcard_observed_at", table_name="memoryfactcard")
    op.drop_index("ix_memoryfactcard_source_path", table_name="memoryfactcard")
    op.drop_index("ix_memoryfactcard_source_system", table_name="memoryfactcard")
    op.drop_index("ix_memoryfactcard_project_slug", table_name="memoryfactcard")
    op.drop_index("ix_memoryfactcard_aliases_text", table_name="memoryfactcard")
    op.drop_index("ix_memoryfactcard_source_capture_id", table_name="memoryfactcard")
    op.drop_table("memoryfactcard")
