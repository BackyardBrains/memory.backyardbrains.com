"""add memory revision status

Revision ID: b7f93c2d4a10
Revises: 91d2b7c4e6f0
Create Date: 2026-06-10 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b7f93c2d4a10"
down_revision: Union[str, Sequence[str], None] = "91d2b7c4e6f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("capture", sa.Column("updated_at", sa.DateTime(), nullable=True))
    op.add_column("capture", sa.Column("memory_status", sa.String(), nullable=False, server_default="active"))
    op.add_column("capture", sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("capture", sa.Column("superseded_by_capture_id", sa.Integer(), nullable=True))
    op.add_column("capture", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    op.add_column("capture", sa.Column("revision_reason", sa.String(), nullable=True))
    op.add_column("capture", sa.Column("revision_actor", sa.String(), nullable=True))
    op.add_column("capture", sa.Column("revision_source_ids_json", sa.String(), nullable=True))
    op.create_foreign_key(
        "fk_capture_superseded_by_capture_id",
        "capture",
        "capture",
        ["superseded_by_capture_id"],
        ["id"],
    )
    op.create_index("ix_capture_updated_at", "capture", ["updated_at"])
    op.create_index("ix_capture_memory_status", "capture", ["memory_status"])
    op.create_index("ix_capture_revision", "capture", ["revision"])
    op.create_index("ix_capture_superseded_by_capture_id", "capture", ["superseded_by_capture_id"])
    op.create_index("ix_capture_deleted_at", "capture", ["deleted_at"])
    op.create_index("ix_capture_revision_actor", "capture", ["revision_actor"])
    op.alter_column("capture", "memory_status", server_default=None)
    op.alter_column("capture", "revision", server_default=None)

    op.add_column("memoryfactcard", sa.Column("memory_status", sa.String(), nullable=False, server_default="active"))
    op.add_column("memoryfactcard", sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("memoryfactcard", sa.Column("superseded_by_card_id", sa.Integer(), nullable=True))
    op.add_column("memoryfactcard", sa.Column("updated_at", sa.DateTime(), nullable=True))
    op.add_column("memoryfactcard", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    op.add_column("memoryfactcard", sa.Column("revision_reason", sa.String(), nullable=True))
    op.add_column("memoryfactcard", sa.Column("revision_actor", sa.String(), nullable=True))
    op.add_column("memoryfactcard", sa.Column("revision_source_ids_json", sa.String(), nullable=True))
    op.create_index("ix_memoryfactcard_memory_status", "memoryfactcard", ["memory_status"])
    op.create_index("ix_memoryfactcard_revision", "memoryfactcard", ["revision"])
    op.create_index("ix_memoryfactcard_superseded_by_card_id", "memoryfactcard", ["superseded_by_card_id"])
    op.create_index("ix_memoryfactcard_updated_at", "memoryfactcard", ["updated_at"])
    op.create_index("ix_memoryfactcard_deleted_at", "memoryfactcard", ["deleted_at"])
    op.create_index("ix_memoryfactcard_revision_actor", "memoryfactcard", ["revision_actor"])
    op.alter_column("memoryfactcard", "memory_status", server_default=None)
    op.alter_column("memoryfactcard", "revision", server_default=None)

    op.create_table(
        "memoryrevision",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("target_type", sa.String(), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("before_json", sa.String(), nullable=True),
        sa.Column("after_json", sa.String(), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("actor", sa.String(), nullable=True),
        sa.Column("source_ids_json", sa.String(), nullable=True),
        sa.Column("idempotency_key", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memoryrevision_target_type", "memoryrevision", ["target_type"])
    op.create_index("ix_memoryrevision_target_id", "memoryrevision", ["target_id"])
    op.create_index("ix_memoryrevision_user_id", "memoryrevision", ["user_id"])
    op.create_index("ix_memoryrevision_action", "memoryrevision", ["action"])
    op.create_index("ix_memoryrevision_actor", "memoryrevision", ["actor"])
    op.create_index("ix_memoryrevision_idempotency_key", "memoryrevision", ["idempotency_key"])
    op.create_index("ix_memoryrevision_created_at", "memoryrevision", ["created_at"])
    op.create_index(
        "ix_memoryrevision_idempotent_target",
        "memoryrevision",
        ["target_type", "target_id", "user_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_memoryrevision_idempotent_target", table_name="memoryrevision")
    op.drop_index("ix_memoryrevision_created_at", table_name="memoryrevision")
    op.drop_index("ix_memoryrevision_idempotency_key", table_name="memoryrevision")
    op.drop_index("ix_memoryrevision_actor", table_name="memoryrevision")
    op.drop_index("ix_memoryrevision_action", table_name="memoryrevision")
    op.drop_index("ix_memoryrevision_user_id", table_name="memoryrevision")
    op.drop_index("ix_memoryrevision_target_id", table_name="memoryrevision")
    op.drop_index("ix_memoryrevision_target_type", table_name="memoryrevision")
    op.drop_table("memoryrevision")

    op.drop_index("ix_memoryfactcard_revision_actor", table_name="memoryfactcard")
    op.drop_index("ix_memoryfactcard_deleted_at", table_name="memoryfactcard")
    op.drop_index("ix_memoryfactcard_updated_at", table_name="memoryfactcard")
    op.drop_index("ix_memoryfactcard_superseded_by_card_id", table_name="memoryfactcard")
    op.drop_index("ix_memoryfactcard_revision", table_name="memoryfactcard")
    op.drop_index("ix_memoryfactcard_memory_status", table_name="memoryfactcard")
    op.drop_column("memoryfactcard", "revision_source_ids_json")
    op.drop_column("memoryfactcard", "revision_actor")
    op.drop_column("memoryfactcard", "revision_reason")
    op.drop_column("memoryfactcard", "deleted_at")
    op.drop_column("memoryfactcard", "updated_at")
    op.drop_column("memoryfactcard", "superseded_by_card_id")
    op.drop_column("memoryfactcard", "revision")
    op.drop_column("memoryfactcard", "memory_status")

    op.drop_index("ix_capture_revision_actor", table_name="capture")
    op.drop_index("ix_capture_deleted_at", table_name="capture")
    op.drop_index("ix_capture_superseded_by_capture_id", table_name="capture")
    op.drop_index("ix_capture_revision", table_name="capture")
    op.drop_index("ix_capture_memory_status", table_name="capture")
    op.drop_index("ix_capture_updated_at", table_name="capture")
    op.drop_constraint("fk_capture_superseded_by_capture_id", "capture", type_="foreignkey")
    op.drop_column("capture", "revision_source_ids_json")
    op.drop_column("capture", "revision_actor")
    op.drop_column("capture", "revision_reason")
    op.drop_column("capture", "deleted_at")
    op.drop_column("capture", "superseded_by_capture_id")
    op.drop_column("capture", "revision")
    op.drop_column("capture", "memory_status")
    op.drop_column("capture", "updated_at")
