"""Email verification, pending registrations and the transactional email outbox.

Additive only. Existing accounts keep their data, credentials and sessions;
`users.email_verified_at` stays NULL for them: they are *not* declared
verified. Such an account is verified when its owner confirms an emailed
link while signed in, or resets the password through an emailed link
(docs/email.md, "Existing accounts").
"""

from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("users", sa.Column("email_verified_at", _TS, nullable=True))
    op.create_table(
        "pending_registrations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("expires_at", _TS, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_pending_registrations_expiry", "pending_registrations", ["expires_at"])
    op.create_table(
        "email_verification_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("pending_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("expires_at", _TS, nullable=False),
        sa.Column("consumed_at", _TS, nullable=True),
        sa.Column("revoked_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
        sa.ForeignKeyConstraint(["pending_id"], ["pending_registrations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.CheckConstraint("(pending_id IS NULL) <> (user_id IS NULL)", name="ck_email_verification_tokens_subject"),
    )
    op.create_index("ix_email_verification_tokens_pending", "email_verification_tokens", ["pending_id"])
    op.create_index("ix_email_verification_tokens_user", "email_verification_tokens", ["user_id"])
    op.create_table(
        "email_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("to_email", sa.String(320), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("dedupe_key", sa.String(160), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", _TS, nullable=False),
        sa.Column("locked_until", _TS, nullable=True),
        sa.Column("provider_message_id", sa.String(200), nullable=True),
        sa.Column("accepted_at", _TS, nullable=True),
        sa.Column("delivered_at", _TS, nullable=True),
        sa.Column("bounced_at", _TS, nullable=True),
        sa.Column("complained_at", _TS, nullable=True),
        sa.Column("last_error", sa.String(80), nullable=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
        sa.CheckConstraint("status IN ('pending', 'sending', 'sent', 'dead')", name="ck_email_outbox_status"),
    )
    op.create_index("ix_email_outbox_due", "email_outbox", ["status", "next_attempt_at"])
    op.create_index("ix_email_outbox_provider_message", "email_outbox", ["provider_message_id"])
    op.create_table(
        "email_webhook_events",
        sa.Column("id", sa.String(200), nullable=False),
        sa.Column("received_at", _TS, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("email_webhook_events")
    op.drop_table("email_outbox")
    op.drop_table("email_verification_tokens")
    op.drop_table("pending_registrations")
    op.drop_column("users", "email_verified_at")
