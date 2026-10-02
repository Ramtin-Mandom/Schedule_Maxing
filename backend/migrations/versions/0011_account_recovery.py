"""Password recovery, credential epochs and shared rate limiting (Milestone 6, docs/backend.md).

Expand only -- a server of the previous version keeps working against the
upgraded schema:

    users.credential_epoch            raised by every credential change (a password reset);
                                      NOT NULL, default 0
    browser_sessions.credential_epoch the epoch the session started under; NOT NULL, default 0
    password_recovery_tokens          issued recovery credentials: only a SHA-256 digest, the
                                      user, expiry and consumed/revoked state
    rate_limit_buckets                fixed-window counters shared by every worker and replica

Existing accounts and sessions keep working: every existing user and browser
session starts at epoch 0, and an access token issued before this revision
(it has no epoch claim) is read as epoch 0. The first password reset of an
account raises its epoch to 1 and so invalidates every earlier token and
session of that account -- intentionally.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('credential_epoch', sa.Integer(), nullable=False, server_default=sa.text('0')))
        batch.create_check_constraint('ck_users_credential_epoch', 'credential_epoch >= 0')
    with op.batch_alter_table('browser_sessions') as batch:
        batch.add_column(sa.Column('credential_epoch', sa.Integer(), nullable=False, server_default=sa.text('0')))
    op.create_table('password_recovery_tokens',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('expires_at > created_at', name='ck_password_recovery_tokens_expiry'),
    sa.CheckConstraint('consumed_at IS NULL OR revoked_at IS NULL', name='ck_password_recovery_tokens_state'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id']),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash'),
    )
    op.create_index('ix_password_recovery_tokens_user', 'password_recovery_tokens', ['user_id'])
    op.create_table('rate_limit_buckets',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('scope', sa.String(length=40), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('count > 0', name='ck_rate_limit_buckets_count'),
    sa.PrimaryKeyConstraint('key'),
    )
    op.create_index('ix_rate_limit_buckets_expiry', 'rate_limit_buckets', ['expires_at'])


def downgrade() -> None:
    # For tests and disposable databases only (docs/backend.md): recovery tokens and counters are dropped.
    op.drop_index('ix_rate_limit_buckets_expiry', table_name='rate_limit_buckets')
    op.drop_table('rate_limit_buckets')
    op.drop_index('ix_password_recovery_tokens_user', table_name='password_recovery_tokens')
    op.drop_table('password_recovery_tokens')
    with op.batch_alter_table('browser_sessions') as batch:
        batch.drop_column('credential_epoch')
    with op.batch_alter_table('users') as batch:
        batch.drop_constraint('ck_users_credential_epoch', type_='check')
        batch.drop_column('credential_epoch')
