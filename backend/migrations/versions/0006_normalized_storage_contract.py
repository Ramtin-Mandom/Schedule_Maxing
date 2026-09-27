"""Normalized storage, step 3 of 3 (validate and contract): prove the backfill exact, then drop the JSON.

Validate (batched, read back from the stored rows -- never from memory):
    - every task's tags, preferred dates and recurrence, every preference
      layer's overrides, every change-log payload and every recorded sync
      result is rebuilt from its relational rows and compared with the
      original JSON (backend/migrations/normalized_storage.same); row counts
      of the child tables must equal the list lengths they came from;
    - every placement's optimization_metadata is within the extension bound;
    - every non-historical execution has its enforced task/placement links.
Any difference stops the upgrade (MigrationDataError: rows and fields named,
never values) and rolls everything back to 0003.

Contract: add the constraints of the new columns, make change_log.revision_id
required, drop tasks.tags/preferred_dates/recurrence, preferences.overrides,
change_log.payload and sync_operations.result, and drop ix_placements_user_task
(uq_placements_user_task_id covers the same (user_id, task_id) prefix).

Writers: after this revision the JSON columns no longer exist, so any
application process of the previous version must be stopped before the
upgrade starts and replaced by this version (see docs/backend.md).

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from collections import defaultdict

import sqlalchemy as sa
from alembic import op

from backend.migrations import normalized_storage as ns

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

RECURRENCE_DEFAULTS = {'interval': 1, 'weekdays': None, 'day_of_month': None, 'end_date': None, 'count': None}


def _children(connection, table: sa.Table, parent: str, keys: list, order: tuple[str, ...]) -> dict:
    grouped = defaultdict(list)
    if keys:
        for row in connection.execute(
            sa.select(table).where(sa.tuple_(table.c.user_id, table.c[parent]).in_(keys))
            .order_by(*(table.c[name] for name in order))
        ):
            grouped[(row.user_id, row._mapping[parent])].append(row._mapping)
    return grouped


def _count(connection, table: sa.Table, *where) -> int:
    return connection.execute(sa.select(sa.func.count()).select_from(table).where(*where)).scalar_one()


def _rebuilt_tasks(connection, rows: list) -> dict:
    keys = [(row['user_id'], row['id']) for row in rows]
    tags = _children(connection, ns.task_tags, 'task_id', keys, ('position',))
    dates = _children(connection, ns.task_preferred_dates, 'task_id', keys, ('position',))
    weekdays = _children(connection, ns.task_recurrence_weekdays, 'task_id', keys, ('weekday',))
    return {key: {
        'tags': [item['tag'] for item in tags[key]],
        'preferred_dates': [item['preferred_date'].isoformat() for item in dates[key]],
        'recurrence': ns.recurrence_json(row, [item['weekday'] for item in weekdays[key]]),
    } for key, row in zip(keys, rows)}


def _rebuilt_overrides(connection, rows: list) -> dict:
    keys = [(row['user_id'], row['id']) for row in rows]
    multipliers = _children(connection, ns.preference_category_multipliers, 'preference_id', keys, ('category',))
    windows = _children(connection, ns.preference_category_windows, 'preference_id', keys, ('category',))
    relations = _children(connection, ns.preference_tag_relations, 'preference_id', keys, ('tag',))
    related = _children(connection, ns.preference_related_tags, 'preference_id', keys, ('tag', 'position'))
    result = {}
    for key, row in zip(keys, rows):
        values = defaultdict(list)
        for item in related[key]:
            values[item['tag']].append(item['related_tag'])
        pairs = [(item['tag'], values[item['tag']]) for item in relations[key]]
        result[key] = ns.overrides_json(row, multipliers[key], windows[key], pairs, with_mode=False)
    return result


def _validate(connection) -> None:
    expected_tags = expected_dates = 0
    for rows in ns.batches(connection, ns.tasks, ('user_id', 'id')):
        rebuilt = _rebuilt_tasks(connection, rows)
        for row in rows:
            where = f"tasks (user_id={row['user_id']}, id={row['id']})"
            again = rebuilt[(row['user_id'], row['id'])]
            recurrence = row['recurrence'] and {**RECURRENCE_DEFAULTS, **row['recurrence']}
            for field, original in (('tags', row['tags']), ('preferred_dates', row['preferred_dates']),
                                    ('recurrence', recurrence)):
                if not ns.same(original, again[field]):
                    ns.fail(where, f"field {field!r} does not survive the conversion exactly")
            expected_tags += len(row['tags'])
            expected_dates += len(row['preferred_dates'])
    if (_count(connection, ns.task_tags), _count(connection, ns.task_preferred_dates)) != (expected_tags, expected_dates):
        ns.fail('task_tags/task_preferred_dates', 'the row counts differ from the stored lists')

    for rows in ns.batches(connection, ns.preferences, ('user_id', 'id')):
        rebuilt = _rebuilt_overrides(connection, rows)
        for row in rows:
            original = ns.normalized_overrides(row['overrides'], with_mode=False)
            if not ns.same(original, rebuilt[(row['user_id'], row['id'])]):
                ns.fail(f"preferences (user_id={row['user_id']}, id={row['id']})",
                        "the overrides do not survive the conversion exactly")

    for rows in ns.batches(connection, ns.placements, ('user_id', 'id')):
        for row in rows:
            ns.check_metadata(row['optimization_metadata'], f"placements (user_id={row['user_id']}, id={row['id']})")

    unlinked = connection.execute(sa.select(ns.executions.c.user_id, ns.executions.c.id).where(
        ns.executions.c.historical_reference.is_(False),
        sa.or_(sa.and_(ns.executions.c.task_id.is_not(None), ns.executions.c.linked_task_id.is_(None)),
               sa.and_(ns.executions.c.scheduled_task_id.is_not(None), ns.executions.c.linked_placement_id.is_(None))),
    ).limit(5)).all()
    if unlinked:
        listed = ', '.join(f'(user_id={user_id}, id={record_id})' for user_id, record_id in unlinked)
        ns.fail(f'executions {listed}', 'a non-historical execution names a task or placement of that task that '
                                        'does not exist for its user (mark it historical_reference to keep it)')

    if _count(connection, ns.change_log, ns.change_log.c.revision_id.is_(None)):
        ns.fail('change_log', 'some entries have no snapshot')
    for rows in ns.batches(connection, ns.change_log, ('user_id', 'seq')):
        records = ns.load_revisions(connection, [(row['user_id'], row['revision_id']) for row in rows])
        for row in rows:
            if not ns.same(row['payload'], records[(row['user_id'], row['revision_id'])]):
                ns.fail(f"change_log (user_id={row['user_id']}, seq={row['seq']})",
                        'the snapshot does not survive the conversion exactly')

    for rows in ns.batches(connection, ns.sync_operations, ('user_id', 'op_id')):
        results = ns.load_outcomes(connection, rows)
        for row in rows:
            if not ns.same(row['result'], results[(row['user_id'], row['op_id'])]):
                ns.fail(f"sync_operations (user_id={row['user_id']}, op_id={row['op_id']})",
                        'the recorded result does not survive the conversion exactly')


def _recurrence_checks(table: str) -> list[tuple[str, str]]:
    return [
        (f'ck_{table}_recurrence_frequency',
         "recurrence_frequency IS NULL OR recurrence_frequency IN ('daily', 'weekly', 'monthly')"),
        (f'ck_{table}_recurrence',
         '(recurrence_frequency IS NULL) = (recurrence_interval IS NULL)'
         ' AND (recurrence_interval IS NULL OR recurrence_interval > 0)'
         ' AND (recurrence_count IS NULL OR recurrence_count > 0)'
         ' AND (recurrence_end_date IS NULL OR recurrence_count IS NULL)'),
        (f'ck_{table}_recurrence_day',
         "recurrence_day_of_month IS NULL"
         " OR (recurrence_frequency = 'monthly' AND recurrence_day_of_month BETWEEN 1 AND 31)"),
        (f'ck_{table}_recurrence_bounds',
         'recurrence_frequency IS NOT NULL OR (recurrence_end_date IS NULL AND recurrence_count IS NULL)'),
    ]


DAY_WINDOW = ('ck_preferences_day_window',
              '(day_window_start_minute IS NULL) = (day_window_end_minute IS NULL)'
              ' AND (day_window_start_minute IS NULL) = (day_window_end_day_offset IS NULL)')
EXECUTION_CHECKS = (
    ('ck_executions_linked_task', 'linked_task_id IS NULL OR linked_task_id = task_id'),
    ('ck_executions_linked_placement',
     'linked_placement_id IS NULL OR (linked_placement_id = scheduled_task_id AND linked_task_id IS NOT NULL)'),
    ('ck_executions_canonical_links',
     'historical_reference OR ((task_id IS NULL OR linked_task_id IS NOT NULL)'
     ' AND (scheduled_task_id IS NULL OR linked_placement_id IS NOT NULL))'),
)
SYNC_CHECKS = (
    ('ck_sync_operations_outcome',
     "(status = 'applied') = (record_revision_id IS NOT NULL)"
     " AND (status = 'applied') = (error_code IS NULL)"
     " AND (error_code IS NULL) = (error_message IS NULL)"),
    ('ck_sync_operations_versions', 'error_supplied_version IS NULL OR error_current_version IS NOT NULL'),
)
METADATA = ('ck_placements_metadata',
            "jsonb_typeof(optimization_metadata) = 'object' AND octet_length(optimization_metadata::text) <= 8192")


def upgrade() -> None:
    connection = op.get_bind()
    _validate(connection)

    with op.batch_alter_table('tasks') as batch:
        for name, condition in _recurrence_checks('tasks'):
            batch.create_check_constraint(name, condition)
        for column in ('recurrence', 'preferred_dates', 'tags'):
            batch.drop_column(column)
    with op.batch_alter_table('preferences') as batch:
        batch.create_check_constraint(*DAY_WINDOW)
        batch.drop_column('overrides')
    op.drop_index('ix_placements_user_task', table_name='placements')
    if connection.dialect.name == 'postgresql':
        op.create_check_constraint(METADATA[0], 'placements', METADATA[1])
    with op.batch_alter_table('executions') as batch:
        for name, condition in EXECUTION_CHECKS:
            batch.create_check_constraint(name, condition)
    with op.batch_alter_table('change_log') as batch:
        batch.alter_column('revision_id', existing_type=sa.Uuid(), nullable=False)
        batch.drop_column('payload')
    with op.batch_alter_table('sync_operations') as batch:
        for name, condition in SYNC_CHECKS:
            batch.create_check_constraint(name, condition)
        batch.drop_column('result')


def downgrade() -> None:
    """Restore the JSON columns, rebuilt from the relational rows (lossless); only for disposable databases."""
    connection = op.get_bind()
    with op.batch_alter_table('sync_operations') as batch:
        for name, _ in SYNC_CHECKS:
            batch.drop_constraint(name, type_='check')
        batch.add_column(sa.Column('result', ns.JSON_DOCUMENT, nullable=True))
    with op.batch_alter_table('change_log') as batch:
        batch.add_column(sa.Column('payload', ns.JSON_DOCUMENT, nullable=True))
        batch.alter_column('revision_id', existing_type=sa.Uuid(), nullable=True)
    with op.batch_alter_table('executions') as batch:
        for name, _ in EXECUTION_CHECKS:
            batch.drop_constraint(name, type_='check')
    if connection.dialect.name == 'postgresql':
        op.drop_constraint(METADATA[0], 'placements', type_='check')
    op.create_index('ix_placements_user_task', 'placements', ['user_id', 'task_id'], unique=False)
    with op.batch_alter_table('preferences') as batch:
        batch.drop_constraint(DAY_WINDOW[0], type_='check')
        batch.add_column(sa.Column('overrides', ns.JSON_DOCUMENT, nullable=True))
    with op.batch_alter_table('tasks') as batch:
        for name, _ in _recurrence_checks('tasks'):
            batch.drop_constraint(name, type_='check')
        batch.add_column(sa.Column('tags', ns.JSON_DOCUMENT, nullable=True))
        batch.add_column(sa.Column('preferred_dates', ns.JSON_DOCUMENT, nullable=True))
        batch.add_column(sa.Column('recurrence', ns.JSON_DOCUMENT, nullable=True))

    for rows in ns.batches(connection, ns.tasks, ('user_id', 'id')):
        rebuilt = _rebuilt_tasks(connection, rows)
        ns.update_rows(connection, ns.tasks, ('user_id', 'id'), [
            {'user_id': row['user_id'], 'id': row['id'], **rebuilt[(row['user_id'], row['id'])]} for row in rows])
    for rows in ns.batches(connection, ns.preferences, ('user_id', 'id')):
        rebuilt = _rebuilt_overrides(connection, rows)
        ns.update_rows(connection, ns.preferences, ('user_id', 'id'), [
            {'user_id': row['user_id'], 'id': row['id'], 'overrides': rebuilt[(row['user_id'], row['id'])]}
            for row in rows])
    for rows in ns.batches(connection, ns.change_log, ('user_id', 'seq')):
        records = ns.load_revisions(connection, [(row['user_id'], row['revision_id']) for row in rows])
        ns.update_rows(connection, ns.change_log, ('user_id', 'seq'), [
            {'user_id': row['user_id'], 'seq': row['seq'], 'payload': records[(row['user_id'], row['revision_id'])]}
            for row in rows])
    for rows in ns.batches(connection, ns.sync_operations, ('user_id', 'op_id')):
        results = ns.load_outcomes(connection, rows)
        ns.update_rows(connection, ns.sync_operations, ('user_id', 'op_id'), [
            {'user_id': row['user_id'], 'op_id': row['op_id'], 'result': results[(row['user_id'], row['op_id'])]}
            for row in rows])

    for table, columns in (('tasks', ('tags', 'preferred_dates')), ('preferences', ('overrides',)),
                           ('change_log', ('payload',)), ('sync_operations', ('result',))):
        with op.batch_alter_table(table) as batch:
            for column in columns:
                batch.alter_column(column, existing_type=ns.JSON_DOCUMENT, nullable=False)
