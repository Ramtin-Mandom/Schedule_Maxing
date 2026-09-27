"""Normalized storage, step 2 of 3 (backfill): fill the relational storage from the JSON columns.

In keyset-paginated batches (backend/migrations/normalized_storage.py,
BATCH_SIZE rows at a time, so memory stays bounded for any table size):
    - tasks: tags / preferred dates (ordered, repeated values kept), recurrence
      scalars and weekdays;
    - preferences: overrides -> scalar columns and category / tag-relation
      rows (absent, value and explicit-clear states kept);
    - executions: linked_task_id / linked_placement_id wherever the historical
      ids resolve to the user's own task and a placement of that task
      (tombstones included); non-historical executions must resolve;
    - change_log: every payload -> an immutable typed revision (tombstones and
      execution work-session histories included); the entry references it;
    - sync_operations: every recorded result -> typed outcome columns, the
      revisions of its record / current / conflicting snapshots and its
      validation problems (applied, conflict and rejected alike).
The JSON columns stay untouched; 0006 compares every one of them with what
is stored here before removing them. Data that cannot be converted exactly
stops the upgrade with a MigrationDataError that names the row and field.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from backend.migrations import normalized_storage as ns

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def _tasks(connection) -> None:
    for rows in ns.batches(connection, ns.tasks, ('user_id', 'id')):
        written, updates = [], []
        for row in rows:
            where = f"tasks (user_id={row['user_id']}, id={row['id']})"
            key = {'user_id': row['user_id'], 'task_id': row['id']}
            for position, tag in enumerate(ns.string_list(row['tags'], where, 'tags')):
                written.append((ns.task_tags, {**key, 'position': position, 'tag': tag}))
            if not isinstance(row['preferred_dates'], list):
                ns.fail(where, "field 'preferred_dates' is not a list")
            for position, day in enumerate(row['preferred_dates']):
                written.append((ns.task_preferred_dates, {
                    **key, 'position': position, 'preferred_date': ns.parse('date', day, where, 'preferred_dates')}))
            columns, weekdays = ns.recurrence_columns(row['recurrence'], where)
            written.extend((ns.task_recurrence_weekdays, {**key, 'weekday': day}) for day in weekdays)
            updates.append({'user_id': row['user_id'], 'id': row['id'], **columns})
        ns.insert_rows(connection, written, 'tasks')
        ns.update_rows(connection, ns.tasks, ('user_id', 'id'), updates)


def _preferences(connection) -> None:
    for rows in ns.batches(connection, ns.preferences, ('user_id', 'id')):
        written, updates = [], []
        for row in rows:
            where = f"preferences (user_id={row['user_id']}, id={row['id']})"
            columns, multipliers, windows, relations = ns.overrides_rows(
                row['overrides'], where, with_mode=False, mode=row['optimizer_mode'])
            key = {'user_id': row['user_id'], 'preference_id': row['id']}
            written.extend((ns.preference_category_multipliers, {**key, **item}) for item in multipliers)
            written.extend((ns.preference_category_windows, {**key, **item}) for item in windows)
            for tag, related in relations:
                written.append((ns.preference_tag_relations, {**key, 'tag': tag}))
                written.extend((ns.preference_related_tags, {**key, 'tag': tag, 'position': position,
                                                             'related_tag': value})
                               for position, value in enumerate(related))
            columns.pop('optimizer_mode')  # already its own column
            updates.append({'user_id': row['user_id'], 'id': row['id'], **columns})
        ns.insert_rows(connection, written, 'preferences')
        ns.update_rows(connection, ns.preferences, ('user_id', 'id'), updates)


def _execution_links(connection) -> None:
    """Set-based: one UPDATE per reference (each row is written at most once)."""
    executions = ns.executions
    tasks = sa.table('tasks', sa.column('user_id', sa.Uuid()), sa.column('id', sa.Uuid()))
    placements = sa.table('placements', sa.column('user_id', sa.Uuid()), sa.column('id', sa.Uuid()),
                          sa.column('task_id', sa.Uuid()))
    connection.execute(sa.update(executions).where(
        executions.c.task_id.is_not(None),
        sa.exists().where(tasks.c.user_id == executions.c.user_id, tasks.c.id == executions.c.task_id),
    ).values(linked_task_id=executions.c.task_id))
    connection.execute(sa.update(executions).where(
        executions.c.linked_task_id.is_not(None), executions.c.scheduled_task_id.is_not(None),
        sa.exists().where(placements.c.user_id == executions.c.user_id,
                          placements.c.id == executions.c.scheduled_task_id,
                          placements.c.task_id == executions.c.task_id),
    ).values(linked_placement_id=executions.c.scheduled_task_id))


def _change_log(connection) -> None:
    for rows in ns.batches(connection, ns.change_log, ('user_id', 'seq')):
        written, updates = [], []
        for row in rows:
            where = f"change_log (user_id={row['user_id']}, seq={row['seq']})"
            record = row['payload']
            entity_type = ns.entity_type_of(record, where)
            if entity_type != row['entity_type']:
                ns.fail(where, "the snapshot is not a record of the entry's entity type")
            rid = ns.revision_id('change_log', row['user_id'], row['seq'])
            revision_rows = ns.revision_rows(row['user_id'], rid, entity_type, record, where)
            header = revision_rows[0][1]
            if header['entity_id'] != row['entity_id'] or header['version'] != row['version']:
                ns.fail(where, "the snapshot's id or version differs from the entry's")
            written.extend(revision_rows)
            updates.append({'user_id': row['user_id'], 'seq': row['seq'], 'revision_id': rid})
        ns.insert_rows(connection, written, 'change_log')
        ns.update_rows(connection, ns.change_log, ('user_id', 'seq'), updates)


def _sync_operations(connection) -> None:
    for rows in ns.batches(connection, ns.sync_operations, ('user_id', 'op_id')):
        written, updates = [], []
        for row in rows:
            where = f"sync_operations (user_id={row['user_id']}, op_id={row['op_id']})"
            columns, outcome_rows = ns.outcome_rows(row['user_id'], row['op_id'], row['status'], row['result'], where)
            written.extend(outcome_rows)
            updates.append({'user_id': row['user_id'], 'op_id': row['op_id'], **columns})
        # The revisions (and problem rows) first: the outcome columns reference them.
        ns.insert_rows(connection, written, 'sync_operations')
        ns.update_rows(connection, ns.sync_operations, ('user_id', 'op_id'), updates)


def upgrade() -> None:
    connection = op.get_bind()
    _tasks(connection)
    _preferences(connection)
    _execution_links(connection)
    _change_log(connection)
    _sync_operations(connection)


def downgrade() -> None:
    """Remove what the backfill wrote (the JSON columns, restored by 0006's downgrade, hold every value)."""
    connection = op.get_bind()
    connection.execute(sa.update(ns.sync_operations).values(
        record_revision_id=None, error_code=None, error_message=None, error_supplied_version=None,
        error_current_version=None, error_current_revision_id=None, error_conflicting_revision_id=None,
        error_reason=None, error_failed_op_id=None, error_problems_present=False))
    connection.execute(sa.update(ns.change_log).values(revision_id=None))
    for table in reversed(ns.WRITTEN_TABLES):
        connection.execute(sa.delete(table))
    connection.execute(sa.update(ns.executions).values(linked_task_id=None, linked_placement_id=None))
    connection.execute(sa.update(ns.tasks).values({name: None for name, _ in ns.RECURRENCE}))
    cleared = {name: None for name, _ in ns.PREFERENCE_SCALARS if name != 'optimizer_mode'}
    connection.execute(sa.update(ns.preferences).values({**cleared, 'reward_tag_relations_present': False}))
