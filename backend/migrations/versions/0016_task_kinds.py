"""Task kinds, preferred thirds of the day, fixed-block points and completions.

Additive:

    tasks / task_revisions
        kind              'flexible' or 'todo' (a checklist item, never scheduled);
                          NOT NULL, server default 'flexible' (every existing task)
        preferred_time    'early' / 'mid' / 'late' -- the preferred third of the
                          day's window; NULL (every existing task): none
    fixed_blocks / fixed_block_revisions
        points            0..1000, NOT NULL, server default 0 (every existing block)

Fixed blocks can now be completed. Every live fixed block that has no
execution yet gets a completed one worth 0 points: a block saved before this
counts as done and is worth nothing. The execution's id is derived from the
block's (app.planning.models.fixed_block_execution_id) -- the same id a device
derives -- so a block that already has its execution is left as it is and
running the backfill again adds nothing.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.planning.models import DEFAULT_TASK_PRIORITY, fixed_block_execution_id
from backend.database import UTCDateTime

revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None

TASK_TABLES = ('tasks', 'task_revisions')
BLOCK_TABLES = ('fixed_blocks', 'fixed_block_revisions')


def backfill_fixed_block_completions(bind) -> int:
    """See the module docstring; returns the executions added."""
    record = lambda: (sa.column('user_id', sa.Uuid()), sa.column('id', sa.Uuid()),  # noqa: E731
                      sa.column('created_at', UTCDateTime()), sa.column('updated_at', UTCDateTime()),
                      sa.column('version', sa.Integer()), sa.column('deleted_at', UTCDateTime()))
    planned = lambda prefix: (sa.column(f'{prefix}planned_date', sa.Date()),  # noqa: E731
                              sa.column(f'{prefix}timezone', sa.String()),
                              sa.column(f'{prefix}planned_start', UTCDateTime()),
                              sa.column(f'{prefix}planned_end', UTCDateTime()))
    blocks = sa.table('fixed_blocks', *record(), sa.column('label', sa.String()), sa.column('category', sa.String()),
                      *planned(''))
    executions = sa.table(
        'executions', *record(), sa.column('historical_reference', sa.Boolean()), sa.column('task_name', sa.String()),
        sa.column('category', sa.String()), sa.column('tag', sa.String()), sa.column('planned_duration', sa.Integer()),
        sa.column('priority', sa.Integer()), sa.column('points', sa.Integer()), sa.column('status', sa.String()),
        *planned('canonical_'), sa.column('actual_final_end_at', UTCDateTime()))
    added = 0
    for block in bind.execute(sa.select(blocks).where(blocks.c.deleted_at.is_(None))).mappings():
        execution_id = fixed_block_execution_id(block['id'])
        exists = bind.execute(sa.select(executions.c.id).where(
            executions.c.user_id == block['user_id'], executions.c.id == execution_id)).first()
        if exists is not None:
            continue
        minutes = round((block['planned_end'] - block['planned_start']).total_seconds() / 60)
        bind.execute(executions.insert().values(
            user_id=block['user_id'], id=execution_id, created_at=block['created_at'], updated_at=block['created_at'],
            version=1, historical_reference=False, task_name=block['label'], category=block['category'], tag='',
            planned_duration=minutes, priority=DEFAULT_TASK_PRIORITY, points=0, status='completed',
            canonical_planned_date=block['planned_date'], canonical_timezone=block['timezone'],
            canonical_planned_start=block['planned_start'], canonical_planned_end=block['planned_end'],
            actual_final_end_at=block['planned_end'],
        ))
        added += 1
    return added


def upgrade() -> None:
    for table in TASK_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column('kind', sa.String(length=20), nullable=False, server_default='flexible'))
            batch.add_column(sa.Column('preferred_time', sa.String(length=10), nullable=True))
            batch.create_check_constraint(f'ck_{table}_kind', "kind IN ('flexible', 'todo')")
            batch.create_check_constraint(
                f'ck_{table}_preferred_time', "preferred_time IS NULL OR preferred_time IN ('early', 'mid', 'late')")
    for table in BLOCK_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column('points', sa.Integer(), nullable=False, server_default='0'))
            batch.create_check_constraint(f'ck_{table}_points', 'points BETWEEN 0 AND 1000')
    backfill_fixed_block_completions(op.get_bind())


def downgrade() -> None:
    # For tests and disposable databases only (docs/backend.md): the values are dropped with the columns; the
    # backfilled completions stay (they are ordinary executions).
    for table in BLOCK_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f'ck_{table}_points', type_='check')
            batch.drop_column('points')
    for table in TASK_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f'ck_{table}_preferred_time', type_='check')
            batch.drop_constraint(f'ck_{table}_kind', type_='check')
            batch.drop_column('preferred_time')
            batch.drop_column('kind')
