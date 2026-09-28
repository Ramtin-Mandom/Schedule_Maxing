"""Placement provenance and multi-record sync outcomes (Milestone 5, docs/execution-rescheduling.md).

Expand only -- every change is additive and nullable, so a server of the
previous version keeps working against the upgraded schema (it neither
reads nor writes the new columns):

    placements, placement_revisions
        task_category     the task's category when the placement was saved (a snapshot)
        removal_reason    why a tombstone left the plan (rescheduled / regenerated / deleted /
                          task_deleted / reset); NULL while live
        superseded_by_id  the placement that replaced it (history: not a foreign key)
    placements: ck_placements_removal_tombstone (removal provenance only on tombstones) and
        ix_placements_user_superseded_by (walking a chain of moves backwards)
    sync_operation_related_records
        the further record snapshots of an applied operation that changed several records
        (a reschedule), so a retried op_id answers all of them identically

Existing rows get NULL: a placement saved before this revision has no known
category snapshot, and an existing tombstone's reason is unknown. Nothing
is back-filled from current tasks or guessed.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None

REASONS = "removal_reason IS NULL OR removal_reason IN ('rescheduled', 'regenerated', 'deleted', 'task_deleted', 'reset')"


def _columns() -> list[sa.Column]:
    return [
        sa.Column('task_category', sa.String(length=100), nullable=True),
        sa.Column('removal_reason', sa.String(length=20), nullable=True),
        sa.Column('superseded_by_id', sa.Uuid(), nullable=True),
    ]


def upgrade() -> None:
    with op.batch_alter_table('placements') as batch:
        for column in _columns():
            batch.add_column(column)
        batch.create_check_constraint('ck_placements_removal_reason', REASONS)
        batch.create_check_constraint('ck_placements_superseded_by', 'superseded_by_id IS NULL OR superseded_by_id <> id')
        batch.create_check_constraint(
            'ck_placements_removal_tombstone',
            'deleted_at IS NOT NULL OR (removal_reason IS NULL AND superseded_by_id IS NULL)',
        )
        batch.create_index('ix_placements_user_superseded_by', ['user_id', 'superseded_by_id'])
    with op.batch_alter_table('placement_revisions') as batch:
        for column in _columns():
            batch.add_column(column)
        batch.create_check_constraint('ck_placement_revisions_removal_reason', REASONS)
        batch.create_check_constraint(
            'ck_placement_revisions_superseded_by', 'superseded_by_id IS NULL OR superseded_by_id <> id'
        )
    op.create_table('sync_operation_related_records',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('op_id', sa.Uuid(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('revision_id', sa.Uuid(), nullable=False),
    sa.CheckConstraint('position >= 0', name='ck_sync_operation_related_records_position'),
    sa.ForeignKeyConstraint(['user_id', 'op_id'], ['sync_operations.user_id', 'sync_operations.op_id'],
                            name='fk_sync_operation_related_records_parent', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id', 'revision_id'], ['record_revisions.user_id', 'record_revisions.id'],
                            name='fk_sync_operation_related_records_revision'),
    sa.PrimaryKeyConstraint('user_id', 'op_id', 'position')
    )


def downgrade() -> None:
    # For tests and disposable databases only (docs/backend.md): the provenance is dropped with the columns.
    op.drop_table('sync_operation_related_records')
    with op.batch_alter_table('placement_revisions') as batch:
        batch.drop_constraint('ck_placement_revisions_superseded_by', type_='check')
        batch.drop_constraint('ck_placement_revisions_removal_reason', type_='check')
        for name in ('superseded_by_id', 'removal_reason', 'task_category'):
            batch.drop_column(name)
    with op.batch_alter_table('placements') as batch:
        batch.drop_index('ix_placements_user_superseded_by')
        batch.drop_constraint('ck_placements_removal_tombstone', type_='check')
        batch.drop_constraint('ck_placements_superseded_by', type_='check')
        batch.drop_constraint('ck_placements_removal_reason', type_='check')
        for name in ('superseded_by_id', 'removal_reason', 'task_category'):
            batch.drop_column(name)
