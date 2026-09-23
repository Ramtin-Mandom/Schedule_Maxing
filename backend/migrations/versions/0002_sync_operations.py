"""Idempotency records for batch synchronization (POST /sync/push).

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    op.create_table('sync_operations',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('op_id', sa.Uuid(), nullable=False),
    sa.Column('request_hash', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('result', JSON_DOCUMENT, nullable=False),
    sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status IN ('applied', 'conflict', 'rejected')", name='ck_sync_operations_status'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('user_id', 'op_id')
    )


def downgrade() -> None:
    op.drop_table('sync_operations')
