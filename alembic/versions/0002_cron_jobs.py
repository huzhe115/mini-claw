"""cron_jobs table

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-14

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: str | Sequence[str] | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('cron_jobs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.String(length=12), nullable=False),
        sa.Column('prompt', sa.Text(), nullable=False),
        sa.Column('schedule', sa.String(length=100), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False,
                  server_default=sa.text('true')),
        sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_cron_jobs_session_id', 'cron_jobs', ['session_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_cron_jobs_session_id', table_name='cron_jobs')
    op.drop_table('cron_jobs')
