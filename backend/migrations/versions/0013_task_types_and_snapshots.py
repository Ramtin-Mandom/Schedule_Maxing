"""Reusable task types and placement planning snapshots (docs/productivity-redesign-plan.md, contract A).

Expand only -- additive, nullable or defaulted, so a server of the previous
version keeps working against the upgraded schema:

    task_types, task_type_revisions       one owner-scoped record per reusable type (a label; the
                                          id is the identity), with its revision table like every
                                          synchronizable record; 'task_type' joins the revision
                                          entity types
    tasks, task_revisions
        task_type_id                      the task's type; on tasks a composite foreign key, so a
                                          task can only ever name a type of the same user
    placements, placement_revisions
        task_name, task_points,           the task's name, points, estimate and type when the
        task_estimate_minutes,            placement was saved (task_category, since 0007, is the
        task_type_id, task_type_label     same kind of snapshot); NULL = not recorded
        task_tags_recorded                whether the tag snapshot was recorded (no tags and
                                          unknown stay distinct)
    placement_task_tags,                  the tag snapshot, ordered child rows
    placement_revision_task_tags

Existing tasks get their deterministic type, exactly as the desktop database
does (app/execution/db.py: legacy_task_type_roots, derived_task_type_id):
uuid5 of the oldest provable root of a recurring series' lineage, or of the
task itself -- never a grouping by name -- with one type record per root
labelled with the root's name. This is a derived identity, not a user
mutation: no task version changes, no revision or change-log entry is
fabricated (a historical task revision keeps a NULL type), and every device
derives the same ids. Existing placements keep NULL snapshots: unknown,
never back-filled from the current task.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None

OLD_ENTITY_TYPES = ('project', 'task', 'fixed_block', 'placement', 'preference', 'schedule_generation', 'execution')
NEW_ENTITY_TYPES = (*OLD_ENTITY_TYPES, 'task_type')
#: app.planning.models.TASK_TYPE_NAMESPACE (frozen here: a migration never follows later code changes).
TASK_TYPE_NAMESPACE = uuid.UUID('3b9d6c1e-52a7-4f0b-8e64-1c7a9d2f5e08')
SNAPSHOT = ('(task_name IS NULL OR length(task_name) > 0) AND (task_type_label IS NULL OR length(task_type_label) > 0)'
            ' AND (task_points IS NULL OR task_points >= 0)'
            ' AND (task_estimate_minutes IS NULL OR task_estimate_minutes > 0)')


def _entity_types(values: tuple[str, ...]) -> str:
    return f"entity_type IN ({', '.join(repr(value) for value in values)})"


def _snapshot_columns() -> list[sa.Column]:
    return [
        sa.Column('task_name', sa.String(length=500), nullable=True),
        sa.Column('task_tags_recorded', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('task_points', sa.Integer(), nullable=True),
        sa.Column('task_estimate_minutes', sa.Integer(), nullable=True),
        sa.Column('task_type_id', sa.Uuid(), nullable=True),
        sa.Column('task_type_label', sa.String(length=500), nullable=True),
    ]


def _tag_table(table: str, parent: str, key: str) -> None:
    op.create_table(
        table,
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column(key, sa.Uuid(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('tag', sa.Text(), nullable=False),
        sa.CheckConstraint('position >= 0', name=f'ck_{table}_position'),
        sa.ForeignKeyConstraint(['user_id', key], [f'{parent}.user_id', f'{parent}.id'], name=f'fk_{table}_parent',
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', key, 'position'),
    )


def _roots(tasks: dict) -> dict:
    """{task id: the task its type is derived from} within one user (see legacy_task_type_roots)."""
    roots = {}
    for task_id, (series_id, _) in tasks.items():
        current = series_id if series_id is not None and series_id in tasks else task_id
        seen = {current}
        while True:
            previous = tasks[current][1]
            if previous is None or previous in seen or previous not in tasks:
                break
            seen.add(previous)
            current = previous
        roots[task_id] = current
    return roots


def _assign_types() -> None:
    connection = op.get_bind()
    tasks = sa.table('tasks', sa.column('user_id', sa.Uuid()), sa.column('id', sa.Uuid()), sa.column('name', sa.String()),
                     sa.column('series_id', sa.Uuid()), sa.column('series_predecessor_id', sa.Uuid()),
                     sa.column('task_type_id', sa.Uuid()))
    types = sa.table('task_types', sa.column('user_id', sa.Uuid()), sa.column('id', sa.Uuid()),
                     sa.column('label', sa.String()), sa.column('created_at', sa.DateTime(timezone=True)),
                     sa.column('updated_at', sa.DateTime(timezone=True)), sa.column('version', sa.Integer()),
                     sa.column('deleted_at', sa.DateTime(timezone=True)))
    by_user: dict = {}
    names: dict = {}
    for user_id, task_id, name, series_id, predecessor_id in connection.execute(
            sa.select(tasks.c.user_id, tasks.c.id, tasks.c.name, tasks.c.series_id, tasks.c.series_predecessor_id)):
        by_user.setdefault(user_id, {})[task_id] = (series_id, predecessor_id)
        names[(user_id, task_id)] = name
    now = datetime.now(timezone.utc)
    for user_id, user_tasks in by_user.items():
        roots = _roots(user_tasks)
        type_ids = {root: uuid.uuid5(TASK_TYPE_NAMESPACE, str(root)) for root in set(roots.values())}
        connection.execute(sa.insert(types), [
            {'user_id': user_id, 'id': type_id, 'label': names[(user_id, root)], 'created_at': now, 'updated_at': now,
             'version': 1, 'deleted_at': None}
            for root, type_id in sorted(type_ids.items(), key=lambda item: str(item[0]))
        ])
        for task_id, root in roots.items():
            connection.execute(
                sa.update(tasks).where(tasks.c.user_id == user_id, tasks.c.id == task_id)
                .values(task_type_id=type_ids[root]))


def upgrade() -> None:
    op.create_table(
        'task_types',
        sa.Column('label', sa.String(length=500), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint('version > 0', name='ck_task_types_version'),
        sa.CheckConstraint('length(label) > 0', name='ck_task_types_label'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('user_id', 'id'),
    )
    op.create_table(
        'task_type_revisions',
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('label', sa.String(length=500), nullable=False),
        sa.CheckConstraint('length(label) > 0', name='ck_task_type_revisions_label'),
        sa.ForeignKeyConstraint(['user_id', 'id'], ['record_revisions.user_id', 'record_revisions.id'],
                                name='fk_task_type_revisions_revision', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', 'id'),
    )
    with op.batch_alter_table('record_revisions') as batch:
        batch.drop_constraint('ck_record_revisions_entity_type', type_='check')
        batch.create_check_constraint('ck_record_revisions_entity_type', _entity_types(NEW_ENTITY_TYPES))

    with op.batch_alter_table('tasks') as batch:
        batch.add_column(sa.Column('task_type_id', sa.Uuid(), nullable=True))
        batch.create_foreign_key('fk_tasks_task_type', 'task_types', ['user_id', 'task_type_id'], ['user_id', 'id'])
        batch.create_index('ix_tasks_user_task_type', ['user_id', 'task_type_id'])
    with op.batch_alter_table('task_revisions') as batch:
        batch.add_column(sa.Column('task_type_id', sa.Uuid(), nullable=True))

    for table in ('placements', 'placement_revisions'):
        with op.batch_alter_table(table) as batch:
            for column in _snapshot_columns():
                batch.add_column(column)
            batch.create_check_constraint(f'ck_{table}_snapshot', SNAPSHOT)
    _tag_table('placement_task_tags', 'placements', 'placement_id')
    _tag_table('placement_revision_task_tags', 'placement_revisions', 'revision_id')

    _assign_types()


def downgrade() -> None:
    # For tests and disposable databases only (docs/backend.md): task types, type assignments and planning
    # snapshots are dropped with their tables and columns. It fails while a task-type revision exists (the
    # narrow entity-type constraint cannot hold), like 0012's.
    op.drop_table('placement_revision_task_tags')
    op.drop_table('placement_task_tags')
    for table in ('placement_revisions', 'placements'):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f'ck_{table}_snapshot', type_='check')
            for column in reversed(_snapshot_columns()):
                batch.drop_column(column.name)
    with op.batch_alter_table('task_revisions') as batch:
        batch.drop_column('task_type_id')
    with op.batch_alter_table('tasks') as batch:
        batch.drop_index('ix_tasks_user_task_type')
        batch.drop_constraint('fk_tasks_task_type', type_='foreignkey')
        batch.drop_column('task_type_id')
    with op.batch_alter_table('record_revisions') as batch:
        batch.drop_constraint('ck_record_revisions_entity_type', type_='check')
        batch.create_check_constraint('ck_record_revisions_entity_type', _entity_types(OLD_ENTITY_TYPES))
    op.drop_table('task_type_revisions')
    op.drop_table('task_types')
