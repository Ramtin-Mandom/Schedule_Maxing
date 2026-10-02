"""Recurrence expansion: series anchors, occurrence identity, exceptions and lineage (docs/recurrence.md).

Expand only -- every column is nullable and nothing is back-filled, so a
server of the previous version keeps working against the upgraded schema (it
neither reads nor writes the new columns; the API's keep-when-omitted rule in
backend/resources.py is what stops such a client from erasing them):

    tasks, task_revisions
        recurrence_start_date, recurrence_timezone
                              a series' explicit anchor (local date) and IANA time zone; NULL on
                              every existing template, which therefore needs configuration --
                              no anchor is guessed from placements or clocks
        series_id, occurrence_slot
                              a materialized occurrence's immutable identity
        occurrence_state      modified / skipped / deleted / superseded
        series_version        provenance: the series version it was materialized from
        series_predecessor_id the series segment a "this and later" change continues
    tasks: fk_tasks_series ((user_id, series_id) -> tasks: an occurrence's series is the same user's),
        uq_tasks_user_series_slot (one record per user, series and slot -- tombstones included, so a
        suppressed slot stays reserved), ix_tasks_user_series_predecessor, and the recurrence CHECKs.

Existing tasks, placements, executions, change-log entries, revisions and
recorded sync outcomes are not touched: legacy template placements keep their
ids and task ids and are mapped to occurrences by identity when a range is
expanded (app/planning/occurrence.py, app/planning/series.py).

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None

TASK_TABLES = ('tasks', 'task_revisions')
STATES = "occurrence_state IN ('modified', 'skipped', 'deleted', 'superseded')"


def _columns() -> list[sa.Column]:
    return [
        sa.Column('recurrence_start_date', sa.Date(), nullable=True),
        sa.Column('recurrence_timezone', sa.String(length=64), nullable=True),
        sa.Column('series_id', sa.Uuid(), nullable=True),
        sa.Column('occurrence_slot', sa.Date(), nullable=True),
        sa.Column('occurrence_state', sa.String(length=20), nullable=True),
        sa.Column('series_version', sa.Integer(), nullable=True),
        sa.Column('series_predecessor_id', sa.Uuid(), nullable=True),
    ]


def _checks(table: str) -> list[tuple[str, str]]:
    return [
        (f'ck_{table}_recurrence_anchor',
         '(recurrence_start_date IS NULL) = (recurrence_timezone IS NULL)'
         ' AND (recurrence_start_date IS NULL OR recurrence_frequency IS NOT NULL)'),
        (f'ck_{table}_occurrence',
         '(series_id IS NULL) = (occurrence_slot IS NULL)'
         ' AND (series_id IS NULL OR recurrence_frequency IS NULL)'
         ' AND (series_id IS NOT NULL OR (occurrence_state IS NULL AND series_version IS NULL))'
         ' AND (series_version IS NULL OR series_version > 0)'),
        (f'ck_{table}_occurrence_state', f'occurrence_state IS NULL OR {STATES}'),
        (f'ck_{table}_series_lineage', 'series_predecessor_id IS NULL OR recurrence_start_date IS NOT NULL'),
    ]


def upgrade() -> None:
    with op.batch_alter_table('tasks') as batch:
        for column in _columns():
            batch.add_column(column)
        for name, condition in _checks('tasks'):
            batch.create_check_constraint(name, condition)
        batch.create_check_constraint(
            'ck_tasks_recurrence_identity',
            "(series_id IS NULL OR series_id <> id) AND (series_predecessor_id IS NULL OR series_predecessor_id <> id)"
            " AND (occurrence_state IS NULL OR occurrence_state = 'modified' OR deleted_at IS NOT NULL)",
        )
        batch.create_foreign_key('fk_tasks_series', 'tasks', ['user_id', 'series_id'], ['user_id', 'id'])
        batch.create_index('uq_tasks_user_series_slot', ['user_id', 'series_id', 'occurrence_slot'], unique=True,
                           sqlite_where=sa.text('series_id IS NOT NULL'),
                           postgresql_where=sa.text('series_id IS NOT NULL'))
        batch.create_index('ix_tasks_user_series_predecessor', ['user_id', 'series_predecessor_id'])
    with op.batch_alter_table('task_revisions') as batch:
        for column in _columns():
            batch.add_column(column)
        for name, condition in _checks('task_revisions'):
            batch.create_check_constraint(name, condition)


def downgrade() -> None:
    # For tests and disposable databases only (docs/backend.md): series anchors, occurrence identity and
    # exception states are dropped with the columns (materialized occurrences remain as ordinary tasks).
    with op.batch_alter_table('task_revisions') as batch:
        for name, _ in reversed(_checks('task_revisions')):
            batch.drop_constraint(name, type_='check')
        for column in reversed(_columns()):
            batch.drop_column(column.name)
    with op.batch_alter_table('tasks') as batch:
        batch.drop_index('ix_tasks_user_series_predecessor')
        batch.drop_index('uq_tasks_user_series_slot')
        batch.drop_constraint('fk_tasks_series', type_='foreignkey')
        batch.drop_constraint('ck_tasks_recurrence_identity', type_='check')
        for name, _ in reversed(_checks('tasks')):
            batch.drop_constraint(name, type_='check')
        for column in reversed(_columns()):
            batch.drop_column(column.name)
