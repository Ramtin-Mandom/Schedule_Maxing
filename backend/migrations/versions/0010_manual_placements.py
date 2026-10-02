"""Placement origin, manual intent and execution cancel reasons (Milestone 6,
docs/execution-rescheduling.md "Manual placements").

Expand only -- every change is additive, nullable or defaulted, so a server
of the previous version keeps working against the upgraded schema (it
neither reads nor writes the new columns):

    placements, placement_revisions
        origin       how the placement came to be: generated / manual; NULL = unknown
                     (saved before this revision)
        preserved    the user's manual intent: generation keeps the placement until it is
                     released (NOT NULL, default false); only a manual placement can be preserved
    executions, execution_revisions
        cancel_reason  why a cancelled execution was cancelled: user / rescheduled / superseded;
                       NULL for any other status, or a cancellation recorded before this revision

Existing rows keep origin NULL and preserved false: nothing is guessed from
coordinates. A placement whose recorded lineage proves it is the destination
of an explicit reschedule (it superseded a 'rescheduled' tombstone) is
treated as preserved when read (app/planning/application.py,
preserved_placement_ids) -- a derivation, not a rewrite, so no revision is
fabricated and synced clients agree.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None

ORIGINS = "origin IS NULL OR origin IN ('generated', 'manual')"
PRESERVED = "NOT preserved OR (origin IS NOT NULL AND origin = 'manual')"
CANCEL_REASONS = "cancel_reason IS NULL OR (status = 'cancelled' AND cancel_reason IN ('user', 'rescheduled', 'superseded'))"


def _placement_columns() -> list[sa.Column]:
    return [
        sa.Column('origin', sa.String(length=20), nullable=True),
        sa.Column('preserved', sa.Boolean(), nullable=False, server_default=sa.false()),
    ]


def upgrade() -> None:
    for table in ('placements', 'placement_revisions'):
        with op.batch_alter_table(table) as batch:
            for column in _placement_columns():
                batch.add_column(column)
            batch.create_check_constraint(f'ck_{table}_origin', ORIGINS)
            batch.create_check_constraint(f'ck_{table}_preserved', PRESERVED)
    for table in ('executions', 'execution_revisions'):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column('cancel_reason', sa.String(length=20), nullable=True))
            batch.create_check_constraint(f'ck_{table}_cancel_reason', CANCEL_REASONS)


def downgrade() -> None:
    # For tests and disposable databases only (docs/backend.md): origins, manual intent and reasons are dropped.
    for table in ('execution_revisions', 'executions'):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f'ck_{table}_cancel_reason', type_='check')
            batch.drop_column('cancel_reason')
    for table in ('placement_revisions', 'placements'):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f'ck_{table}_preserved', type_='check')
            batch.drop_constraint(f'ck_{table}_origin', type_='check')
            batch.drop_column('preserved')
            batch.drop_column('origin')
