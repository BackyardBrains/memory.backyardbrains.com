"""Separate context/feedback telemetry from durable memory."""
from alembic import op
import sqlalchemy as sa

revision='e61015b1'
down_revision='d61015a1'
branch_labels=depends_on=None


def upgrade():
    op.create_table('memoryuseevent',
        sa.Column('id',sa.String(),primary_key=True),
        sa.Column('user_id',sa.String(),nullable=False),
        sa.Column('kind',sa.String(),nullable=False),
        sa.Column('parent_id',sa.String(),sa.ForeignKey('memoryuseevent.id'),nullable=True),
        sa.Column('client',sa.String(),nullable=False),
        sa.Column('project_slug',sa.String(),nullable=True),
        sa.Column('query_hash',sa.String(),nullable=True),
        sa.Column('capture_ids_json',sa.String(),nullable=False,server_default='[]'),
        sa.Column('rule_ids_json',sa.String(),nullable=False,server_default='[]'),
        sa.Column('created_at',sa.DateTime(),nullable=False))
    for name in ('user_id','kind','parent_id','created_at'):
        op.create_index('ix_memoryuseevent_'+name,'memoryuseevent',[name])
    op.create_index('uq_memoryuseevent_feedback','memoryuseevent',['user_id','parent_id','kind'],unique=True,
                    postgresql_where=sa.text('parent_id IS NOT NULL'))


def downgrade():
    op.drop_table('memoryuseevent')
