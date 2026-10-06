"""A project's planned dates, completion, task defaults and milestones.

Expand only -- additive and nullable, so a server of the previous version
keeps working against the upgraded schema (it neither reads nor writes the
new columns and tables):

    projects, project_revisions
        start_date, estimated_end_date    the planned span; NULL = not set
        completed_at                      when the project was marked complete; NULL = ongoing
        default_duration_minutes,         what the project fills in for a new task of its own;
        default_priority, default_points  NULL = not configured (a category's default applies)
    project_milestones,                   a project's milestones, ordered child rows: the milestone's
    project_revision_milestones           own id, its number (the user's ordering number), title,
                                          description and score (1..10)

Existing projects and their stored revisions have no dates, are ongoing and
have no milestones.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None

TABLES = ('projects', 'project_revisions')
COLUMNS = ('start_date', 'estimated_end_date', 'completed_at', 'default_duration_minutes', 'default_priority',
           'default_points')
TASK_DEFAULTS = ('(default_duration_minutes IS NULL OR default_duration_minutes BETWEEN 1 AND 1440)'
                 ' AND (default_priority IS NULL OR default_priority BETWEEN 1 AND 10)'
                 ' AND (default_points IS NULL OR default_points BETWEEN 0 AND 1000)')


def _milestone_table(table: str, parent: str, key: str) -> None:
    op.create_table(
        table,
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column(key, sa.Uuid(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('milestone_id', sa.Uuid(), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('description', sa.String(length=4000), nullable=False),
        sa.Column('score', sa.Integer(), nullable=False),
        sa.CheckConstraint('position >= 0', name=f'ck_{table}_position'),
        sa.CheckConstraint('length(title) > 0', name=f'ck_{table}_title'),
        sa.CheckConstraint('score BETWEEN 1 AND 10', name=f'ck_{table}_score'),
        sa.ForeignKeyConstraint(['user_id', key], [f'{parent}.user_id', f'{parent}.id'], name=f'fk_{table}_parent',
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', key, 'position'),
    )


def upgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column('start_date', sa.Date(), nullable=True))
            batch.add_column(sa.Column('estimated_end_date', sa.Date(), nullable=True))
            batch.add_column(sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True))
            batch.add_column(sa.Column('default_duration_minutes', sa.Integer(), nullable=True))
            batch.add_column(sa.Column('default_priority', sa.Integer(), nullable=True))
            batch.add_column(sa.Column('default_points', sa.Integer(), nullable=True))
            batch.create_check_constraint(f'ck_{table}_task_defaults', TASK_DEFAULTS)
    _milestone_table('project_milestones', 'projects', 'project_id')
    _milestone_table('project_revision_milestones', 'project_revisions', 'revision_id')


def downgrade() -> None:
    # For tests and disposable databases only (docs/backend.md): the details are dropped with their tables
    # and columns.
    op.drop_table('project_revision_milestones')
    op.drop_table('project_milestones')
    for table in reversed(TABLES):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f'ck_{table}_task_defaults', type_='check')
            for column in reversed(COLUMNS):
                batch.drop_column(column)
