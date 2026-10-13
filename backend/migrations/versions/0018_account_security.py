"""Second factor, session labels and sign-in recognition.

Additive only. Existing accounts, sessions and credentials are unchanged:
nobody has a second factor until they enroll one, existing sessions simply
have no label, and password hashes are untouched (the longer minimum applies
to passwords chosen from now on; see docs/account-security.md).
"""

from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("native_sessions", sa.Column("client_label", sa.String(120), nullable=True))
    op.add_column("browser_sessions", sa.Column("client_label", sa.String(120), nullable=True))
    op.create_table(
        "mfa_totp",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("secret_encrypted", sa.String(255), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("confirmed_at", _TS, nullable=True),
        sa.Column("last_used_step", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("user_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_table(
        "mfa_recovery_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("used_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_hash"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index("ix_mfa_recovery_codes_user", "mfa_recovery_codes", ["user_id"])
    op.create_table(
        "sign_in_fingerprints",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("first_seen_at", _TS, nullable=False),
        sa.PrimaryKeyConstraint("user_id", "fingerprint"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )


def downgrade() -> None:
    op.drop_table("sign_in_fingerprints")
    op.drop_table("mfa_recovery_codes")
    op.drop_table("mfa_totp")
    op.drop_column("browser_sessions", "client_label")
    op.drop_column("native_sessions", "client_label")
