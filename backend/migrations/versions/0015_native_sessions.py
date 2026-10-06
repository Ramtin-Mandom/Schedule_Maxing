"""Native-client refresh families and single-use credential digests.

Additive only. Existing accounts, records, browser sessions and sync history
are unchanged. Consumed digests are retained for replay detection until the
family expires. The cascading FK permits eventual administrator retention
cleanup without leaving orphan credentials.
"""

from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "native_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("credential_epoch", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.CheckConstraint("credential_epoch >= 0", name="ck_native_sessions_epoch"),
        sa.CheckConstraint("expires_at > created_at", name="ck_native_sessions_expiry"),
    )
    op.create_index("ix_native_sessions_user", "native_sessions", ["user_id"])
    op.create_index("ix_native_sessions_expiry", "native_sessions", ["expires_at"])
    op.create_table(
        "refresh_credentials",
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("token_hash"),
        sa.ForeignKeyConstraint(["session_id"], ["native_sessions.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_refresh_credentials_session", "refresh_credentials", ["session_id"])


def downgrade() -> None:
    op.drop_table("refresh_credentials")
    op.drop_table("native_sessions")
