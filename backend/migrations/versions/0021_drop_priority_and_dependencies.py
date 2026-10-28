"""Tasks have no priority and no dependencies (points replaced priority; dependencies were removed).

Contract:

    task_dependencies, task_revision_dependencies     dropped (with their rows)
    tasks / task_revisions                             priority and ck_<table>_priority dropped
    executions / execution_revisions                   priority and ck_<table>_priority dropped
    projects / project_revisions                       default_priority dropped; ck_<table>_task_defaults
                                                       recreated without it

The wire keeps `priority` (5) and `dependency_ids` ([]) as placeholders for 1.3.0 clients
(backend/resources.py LEGACY_TASK_WIRE_FIELDS, backend/executions.py LEGACY_EXECUTION_WIRE_FIELDS), so a
server of the previous version must not run against this schema: deploy the server with this revision.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None

TASK_TABLES = ('tasks', 'task_revisions', 'executions', 'execution_revisions')
PROJECT_TABLES = ('projects', 'project_revisions')
#: Frozen copies of backend/models.py's values before and after this revision.
OLD_TASK_DEFAULTS = ('(default_duration_minutes IS NULL OR default_duration_minutes BETWEEN 1 AND 1440)'
                     ' AND (default_priority IS NULL OR default_priority BETWEEN 1 AND 10)'
                     ' AND (default_points IS NULL OR default_points BETWEEN 0 AND 1000)')
NEW_TASK_DEFAULTS = ('(default_duration_minutes IS NULL OR default_duration_minutes BETWEEN 1 AND 1440)'
                     ' AND (default_points IS NULL OR default_points BETWEEN 0 AND 1000)')
LEGACY_PRIORITY = 5


def upgrade() -> None:
    op.drop_table('task_revision_dependencies')
    op.drop_index('ix_task_dependencies_depends_on', table_name='task_dependencies')
    op.drop_table('task_dependencies')
    for table in TASK_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f'ck_{table}_priority', type_='check')
            batch.drop_column('priority')
    for table in PROJECT_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f'ck_{table}_task_defaults', type_='check')
            batch.drop_column('default_priority')
            batch.create_check_constraint(f'ck_{table}_task_defaults', NEW_TASK_DEFAULTS)


def downgrade() -> None:
    # For tests and disposable databases only (docs/backend.md): every task and execution gets the legacy
    # priority, and no dependency comes back.
    for table in reversed(PROJECT_TABLES):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f'ck_{table}_task_defaults', type_='check')
            batch.add_column(sa.Column('default_priority', sa.Integer(), nullable=True))
            batch.create_check_constraint(f'ck_{table}_task_defaults', OLD_TASK_DEFAULTS)
    for table in reversed(TASK_TABLES):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column('priority', sa.Integer(), nullable=False,
                                       server_default=str(LEGACY_PRIORITY)))
        with op.batch_alter_table(table) as batch:
            batch.alter_column('priority', server_default=None)
            batch.create_check_constraint(f'ck_{table}_priority', 'priority BETWEEN 1 AND 10')
    op.create_table('task_dependencies',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('task_id', sa.Uuid(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('depends_on_id', sa.Uuid(), nullable=False),
    sa.CheckConstraint('position >= 0', name='ck_task_dependencies_position'),
    sa.CheckConstraint('task_id <> depends_on_id', name='ck_task_dependencies_not_self'),
    sa.ForeignKeyConstraint(
        ['user_id', 'depends_on_id'], ['tasks.user_id', 'tasks.id'], name='fk_task_dependencies_depends_on'
    ),
    sa.ForeignKeyConstraint(['user_id', 'task_id'], ['tasks.user_id', 'tasks.id'], name='fk_task_dependencies_task'),
    sa.PrimaryKeyConstraint('user_id', 'task_id', 'position')
    )
    op.create_index('ix_task_dependencies_depends_on', 'task_dependencies', ['user_id', 'depends_on_id'], unique=False)
    op.create_table('task_revision_dependencies',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('revision_id', sa.Uuid(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('depends_on_id', sa.Uuid(), nullable=False),
    sa.CheckConstraint('position >= 0', name='ck_task_revision_dependencies_position'),
    sa.ForeignKeyConstraint(['user_id', 'revision_id'], ['task_revisions.user_id', 'task_revisions.id'],
                            name='fk_task_revision_dependencies_parent', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'revision_id', 'position')
    )
