"""add project lifecycle state

Revision ID: c4d9f8a2b731
Revises: f0a4c1d2e3b5
Create Date: 2026-06-17 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c4d9f8a2b731"
down_revision: Union[str, Sequence[str], None] = "f0a4c1d2e3b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "project",
        sa.Column("lifecycle_state", sa.String(), nullable=False, server_default="active"),
    )
    op.create_index("ix_project_lifecycle_state", "project", ["lifecycle_state"])
    op.execute(
        """
        UPDATE project
        SET lifecycle_state = 'closed'
        WHERE LOWER(COALESCE(priority, '')) = 'closed'
           OR LOWER(COALESCE(status, '')) LIKE '%closed%'
           OR LOWER(COALESCE(status, '')) LIKE '%duplicate%'
           OR LOWER(COALESCE(status, '')) LIKE '%retired%'
           OR LOWER(COALESCE(status, '')) LIKE '%merged%'
           OR LOWER(COALESCE(status, '')) LIKE '%superseded%'
           OR LOWER(COALESCE(status, '')) LIKE '%obsolete%'
           OR LOWER(COALESCE(status, '')) LIKE '%cancelled%'
           OR LOWER(COALESCE(status, '')) LIKE '%canceled%'
        """
    )
    op.alter_column("project", "lifecycle_state", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_project_lifecycle_state", table_name="project")
    op.drop_column("project", "lifecycle_state")
