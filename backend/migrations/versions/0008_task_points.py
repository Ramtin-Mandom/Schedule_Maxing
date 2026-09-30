"""Task points (the user's own productivity value) and each execution's snapshot of them.

Expand only -- additive, so a server of the previous version keeps working
against the upgraded schema (it neither reads nor writes the new columns):

    tasks, task_revisions
        points            0..1000, NOT NULL, server default 1 (existing tasks and their
                          stored revisions get the default -- the value every client used)
    executions, execution_revisions
        points            the task's points when the execution was created; NULL for
                          existing executions (unknown, never back-filled from the task)

Points are analytics data only: they are not a scheduling input (the
inputs fingerprint excludes them) and unrelated to a placement's optimizer
score.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None

TASK_TABLES = ('tasks', 'task_revisions')
EXECUTION_TABLES = ('executions', 'execution_revisions')


def upgrade() -> None:
    for table in TASK_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column('points', sa.Integer(), nullable=False, server_default='1'))
            batch.create_check_constraint(f'ck_{table}_points', 'points BETWEEN 0 AND 1000')
    for table in EXECUTION_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column('points', sa.Integer(), nullable=True))
            batch.create_check_constraint(f'ck_{table}_points', 'points IS NULL OR points >= 0')


def downgrade() -> None:
    # For tests and disposable databases only (docs/backend.md): the points are dropped with the columns.
    for table in (*EXECUTION_TABLES, *TASK_TABLES):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f'ck_{table}_points', type_='check')
            batch.drop_column('points')
