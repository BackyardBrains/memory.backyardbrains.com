"""add cortex task/project fields

Revision ID: a3e9d54c1f72
Revises: 91d2b7c4e6f0
Create Date: 2026-06-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "a3e9d54c1f72"
down_revision = "91d2b7c4e6f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Task: prepared draft body + Cortex triage state. Additive, nullable, no backfill.
    op.add_column("task", sa.Column("draft_text", sa.Text(), nullable=True))
    op.add_column("task", sa.Column("state", sa.String(), nullable=True))
    # Project: category, last meaningful activity, waiting-on marker.
    op.add_column("project", sa.Column("category", sa.String(), nullable=True))
    op.add_column("project", sa.Column("last_activity_at", sa.DateTime(), nullable=True))
    op.add_column("project", sa.Column("waiting_on", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("project", "waiting_on")
    op.drop_column("project", "last_activity_at")
    op.drop_column("project", "category")
    op.drop_column("task", "state")
    op.drop_column("task", "draft_text")
