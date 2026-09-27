"""Normalized storage, step 1 of 3 (expand): relational tables and columns next to the JSON ones.

Adds, without touching existing values:
    - task child rows (tags, preferred dates, recurrence weekdays) and the
      recurrence scalar columns;
    - preference scalar columns (day window, reward fields, the tag-relations
      presence flag) and category/tag-relation child rows;
    - the immutable record revisions (record_revisions + one typed table per
      entity type + ordered child rows) the change log and sync outcomes
      will reference, and the typed sync outcome columns/problem rows;
    - the enforced execution references (linked_task_id/linked_placement_id,
      composite foreign keys; the placement must belong to the task) and
      the indexes they and the planning queries need.
0005 fills them from the JSON columns; 0006 validates and removes the JSON.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
ENTITY_TYPES = "'project', 'task', 'fixed_block', 'placement', 'preference', 'schedule_generation', 'execution'"
REWARD_FLOAT = ('weight_importance', 'weight_time_bonus', 'weight_tag_relation', 'weight_fragmentation_penalty',
                'weight_category_bonus', 'short_gap_bonus_weight', 'short_gap_bonus_cap')
REWARD_INT = ('max_time_distance_minutes', 'same_tag_window_minutes', 'min_gap_between_tasks_minutes',
              'short_gap_bonus_max_minutes')


def _instant(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _child_fk(table: str, parent: str, columns: tuple, parent_columns: tuple) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(['user_id', *columns], [f'{parent}.user_id', *(f'{parent}.{c}' for c in parent_columns)],
                                   name=f'fk_{table}_parent', ondelete='CASCADE')


def _position(table: str) -> sa.CheckConstraint:
    return sa.CheckConstraint('position >= 0', name=f'ck_{table}_position')


def _revision_key(table: str) -> list:
    return [
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['user_id', 'id'], ['record_revisions.user_id', 'record_revisions.id'],
                                name=f'fk_{table}_revision', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', 'id'),
    ]


def _recurrence_columns() -> list:
    return [
        sa.Column('recurrence_frequency', sa.String(length=10), nullable=True),
        sa.Column('recurrence_interval', sa.Integer(), nullable=True),
        sa.Column('recurrence_day_of_month', sa.Integer(), nullable=True),
        sa.Column('recurrence_end_date', sa.Date(), nullable=True),
        sa.Column('recurrence_count', sa.Integer(), nullable=True),
    ]


def recurrence_checks(table: str) -> list:
    return [
        sa.CheckConstraint(
            "recurrence_frequency IS NULL OR recurrence_frequency IN ('daily', 'weekly', 'monthly')",
            name=f'ck_{table}_recurrence_frequency'),
        sa.CheckConstraint(
            '(recurrence_frequency IS NULL) = (recurrence_interval IS NULL)'
            ' AND (recurrence_interval IS NULL OR recurrence_interval > 0)'
            ' AND (recurrence_count IS NULL OR recurrence_count > 0)'
            ' AND (recurrence_end_date IS NULL OR recurrence_count IS NULL)',
            name=f'ck_{table}_recurrence'),
        sa.CheckConstraint(
            "recurrence_day_of_month IS NULL"
            " OR (recurrence_frequency = 'monthly' AND recurrence_day_of_month BETWEEN 1 AND 31)",
            name=f'ck_{table}_recurrence_day'),
        sa.CheckConstraint(
            'recurrence_frequency IS NOT NULL OR (recurrence_end_date IS NULL AND recurrence_count IS NULL)',
            name=f'ck_{table}_recurrence_bounds'),
    ]


def _preference_scalar_columns() -> list:
    return [
        sa.Column('day_window_start_minute', sa.Integer(), nullable=True),
        sa.Column('day_window_end_minute', sa.Integer(), nullable=True),
        sa.Column('day_window_end_day_offset', sa.Integer(), nullable=True),
        *(sa.Column(f'reward_{name}', sa.Float(), nullable=True) for name in REWARD_FLOAT[:5]),
        *(sa.Column(f'reward_{name}', sa.Integer(), nullable=True) for name in REWARD_INT[:3]),
        sa.Column('reward_short_gap_bonus_weight', sa.Float(), nullable=True),
        sa.Column('reward_short_gap_bonus_max_minutes', sa.Integer(), nullable=True),
        sa.Column('reward_short_gap_bonus_cap', sa.Float(), nullable=True),
        sa.Column('reward_tag_relations_present', sa.Boolean(), server_default=sa.false(), nullable=False),
    ]


def day_window_check(table: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        '(day_window_start_minute IS NULL) = (day_window_end_minute IS NULL)'
        ' AND (day_window_start_minute IS NULL) = (day_window_end_day_offset IS NULL)',
        name=f'ck_{table}_day_window')


def metadata_check(table: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        "jsonb_typeof(optimization_metadata) = 'object' AND octet_length(optimization_metadata::text) <= 8192",
        name=f'ck_{table}_metadata')


def _create_live_children() -> None:
    op.create_table('task_tags',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('task_id', sa.Uuid(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('tag', sa.Text(), nullable=False),
    _position('task_tags'),
    _child_fk('task_tags', 'tasks', ('task_id',), ('id',)),
    sa.PrimaryKeyConstraint('user_id', 'task_id', 'position')
    )
    op.create_table('task_preferred_dates',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('task_id', sa.Uuid(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('preferred_date', sa.Date(), nullable=False),
    _position('task_preferred_dates'),
    _child_fk('task_preferred_dates', 'tasks', ('task_id',), ('id',)),
    sa.PrimaryKeyConstraint('user_id', 'task_id', 'position')
    )
    op.create_table('task_recurrence_weekdays',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('task_id', sa.Uuid(), nullable=False),
    sa.Column('weekday', sa.Integer(), nullable=False),
    sa.CheckConstraint('weekday BETWEEN 0 AND 6', name='ck_task_recurrence_weekdays_weekday'),
    _child_fk('task_recurrence_weekdays', 'tasks', ('task_id',), ('id',)),
    sa.PrimaryKeyConstraint('user_id', 'task_id', 'weekday')
    )
    op.create_table('preference_category_multipliers',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('preference_id', sa.Uuid(), nullable=False),
    sa.Column('category', sa.Text(), nullable=False),
    sa.Column('multiplier', sa.Float(), nullable=True),
    _child_fk('preference_category_multipliers', 'preferences', ('preference_id',), ('id',)),
    sa.PrimaryKeyConstraint('user_id', 'preference_id', 'category')
    )
    op.create_table('preference_category_windows',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('preference_id', sa.Uuid(), nullable=False),
    sa.Column('category', sa.Text(), nullable=False),
    sa.Column('start_minute', sa.Integer(), nullable=True),
    sa.Column('end_minute', sa.Integer(), nullable=True),
    sa.CheckConstraint('(start_minute IS NULL) = (end_minute IS NULL)', name='ck_preference_category_windows_window'),
    _child_fk('preference_category_windows', 'preferences', ('preference_id',), ('id',)),
    sa.PrimaryKeyConstraint('user_id', 'preference_id', 'category')
    )
    op.create_table('preference_tag_relations',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('preference_id', sa.Uuid(), nullable=False),
    sa.Column('tag', sa.Text(), nullable=False),
    _child_fk('preference_tag_relations', 'preferences', ('preference_id',), ('id',)),
    sa.PrimaryKeyConstraint('user_id', 'preference_id', 'tag')
    )
    op.create_table('preference_related_tags',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('preference_id', sa.Uuid(), nullable=False),
    sa.Column('tag', sa.Text(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('related_tag', sa.Text(), nullable=False),
    _position('preference_related_tags'),
    _child_fk('preference_related_tags', 'preference_tag_relations', ('preference_id', 'tag'), ('preference_id', 'tag')),
    sa.PrimaryKeyConstraint('user_id', 'preference_id', 'tag', 'position')
    )


def _create_revisions(postgres: bool) -> None:
    op.create_table('record_revisions',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('entity_type', sa.String(length=40), nullable=False),
    sa.Column('entity_id', sa.Uuid(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    _instant('created_at'),
    _instant('updated_at'),
    _instant('deleted_at', nullable=True),
    sa.CheckConstraint(f'entity_type IN ({ENTITY_TYPES})', name='ck_record_revisions_entity_type'),
    sa.CheckConstraint('version > 0', name='ck_record_revisions_version'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('user_id', 'id')
    )
    op.create_table('project_revisions',
    *_revision_key('project_revisions'),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.CheckConstraint('length(name) > 0', name='ck_project_revisions_name')
    )
    op.create_table('task_revisions',
    *_revision_key('task_revisions'),
    sa.Column('project_id', sa.Uuid(), nullable=True),
    sa.Column('name', sa.String(length=500), nullable=False),
    sa.Column('category', sa.String(length=100), nullable=False),
    sa.Column('estimated_duration_minutes', sa.Integer(), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('required', sa.Boolean(), nullable=False),
    sa.Column('required_date', sa.Date(), nullable=True),
    sa.Column('preferred_window_start_minute', sa.Integer(), nullable=True),
    sa.Column('preferred_window_end_minute', sa.Integer(), nullable=True),
    sa.Column('deadline', sa.String(length=40), nullable=True),
    _instant('deadline_utc', nullable=True),
    *_recurrence_columns(),
    sa.CheckConstraint('length(name) > 0', name='ck_task_revisions_name'),
    sa.CheckConstraint('length(category) > 0', name='ck_task_revisions_category'),
    sa.CheckConstraint('estimated_duration_minutes > 0', name='ck_task_revisions_duration'),
    sa.CheckConstraint('priority BETWEEN 1 AND 10', name='ck_task_revisions_priority'),
    sa.CheckConstraint('(preferred_window_start_minute IS NULL) = (preferred_window_end_minute IS NULL)',
                       name='ck_task_revisions_window'),
    sa.CheckConstraint('(deadline IS NULL) = (deadline_utc IS NULL)', name='ck_task_revisions_deadline'),
    *recurrence_checks('task_revisions')
    )
    for table, column, kind in (
        ('task_revision_tags', 'tag', sa.Text()),
        ('task_revision_preferred_dates', 'preferred_date', sa.Date()),
        ('task_revision_dependencies', 'depends_on_id', sa.Uuid()),
    ):
        op.create_table(table,
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('revision_id', sa.Uuid(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column(column, kind, nullable=False),
        _position(table),
        _child_fk(table, 'task_revisions', ('revision_id',), ('id',)),
        sa.PrimaryKeyConstraint('user_id', 'revision_id', 'position')
        )
    op.create_table('task_revision_recurrence_weekdays',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('revision_id', sa.Uuid(), nullable=False),
    sa.Column('weekday', sa.Integer(), nullable=False),
    sa.CheckConstraint('weekday BETWEEN 0 AND 6', name='ck_task_revision_recurrence_weekdays_weekday'),
    _child_fk('task_revision_recurrence_weekdays', 'task_revisions', ('revision_id',), ('id',)),
    sa.PrimaryKeyConstraint('user_id', 'revision_id', 'weekday')
    )
    op.create_table('fixed_block_revisions',
    *_revision_key('fixed_block_revisions'),
    sa.Column('label', sa.String(length=500), nullable=False),
    sa.Column('category', sa.String(length=100), nullable=False),
    sa.Column('planned_date', sa.Date(), nullable=False),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    _instant('planned_start'),
    _instant('planned_end'),
    sa.CheckConstraint('length(label) > 0', name='ck_fixed_block_revisions_label'),
    sa.CheckConstraint('length(category) > 0', name='ck_fixed_block_revisions_category'),
    sa.CheckConstraint('planned_end > planned_start', name='ck_fixed_block_revisions_order')
    )
    op.create_table('placement_revisions',
    *_revision_key('placement_revisions'),
    sa.Column('task_id', sa.Uuid(), nullable=False),
    sa.Column('planned_date', sa.Date(), nullable=False),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    _instant('planned_start'),
    _instant('planned_end'),
    sa.Column('score', sa.Float(), nullable=False),
    sa.Column('optimization_metadata', JSON_DOCUMENT, nullable=False),
    sa.CheckConstraint('planned_end > planned_start', name='ck_placement_revisions_order'),
    *([metadata_check('placement_revisions')] if postgres else [])
    )
    op.create_table('preference_revisions',
    *_revision_key('preference_revisions'),
    sa.Column('scope', sa.String(length=10), nullable=False),
    sa.Column('scope_date', sa.Date(), nullable=True),
    sa.Column('scope_key', sa.String(length=16), nullable=False),
    sa.Column('optimizer_mode', sa.String(length=20), nullable=True),
    *_preference_scalar_columns(),
    sa.CheckConstraint("scope IN ('user', 'date')", name='ck_preference_revisions_scope'),
    sa.CheckConstraint("(scope = 'date') = (scope_date IS NOT NULL)", name='ck_preference_revisions_scope_date'),
    sa.CheckConstraint("optimizer_mode IS NULL OR optimizer_mode IN ('precise_greedy', 'adhd_friendly')",
                       name='ck_preference_revisions_mode'),
    day_window_check('preference_revisions')
    )
    op.create_table('preference_revision_category_multipliers',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('revision_id', sa.Uuid(), nullable=False),
    sa.Column('category', sa.Text(), nullable=False),
    sa.Column('multiplier', sa.Float(), nullable=True),
    _child_fk('preference_revision_category_multipliers', 'preference_revisions', ('revision_id',), ('id',)),
    sa.PrimaryKeyConstraint('user_id', 'revision_id', 'category')
    )
    op.create_table('preference_revision_category_windows',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('revision_id', sa.Uuid(), nullable=False),
    sa.Column('category', sa.Text(), nullable=False),
    sa.Column('start_minute', sa.Integer(), nullable=True),
    sa.Column('end_minute', sa.Integer(), nullable=True),
    sa.CheckConstraint('(start_minute IS NULL) = (end_minute IS NULL)',
                       name='ck_preference_revision_category_windows_window'),
    _child_fk('preference_revision_category_windows', 'preference_revisions', ('revision_id',), ('id',)),
    sa.PrimaryKeyConstraint('user_id', 'revision_id', 'category')
    )
    op.create_table('preference_revision_tag_relations',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('revision_id', sa.Uuid(), nullable=False),
    sa.Column('tag', sa.Text(), nullable=False),
    _child_fk('preference_revision_tag_relations', 'preference_revisions', ('revision_id',), ('id',)),
    sa.PrimaryKeyConstraint('user_id', 'revision_id', 'tag')
    )
    op.create_table('preference_revision_related_tags',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('revision_id', sa.Uuid(), nullable=False),
    sa.Column('tag', sa.Text(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('related_tag', sa.Text(), nullable=False),
    _position('preference_revision_related_tags'),
    _child_fk('preference_revision_related_tags', 'preference_revision_tag_relations', ('revision_id', 'tag'),
              ('revision_id', 'tag')),
    sa.PrimaryKeyConstraint('user_id', 'revision_id', 'tag', 'position')
    )
    op.create_table('schedule_generation_revisions',
    *_revision_key('schedule_generation_revisions'),
    sa.Column('planned_date', sa.Date(), nullable=False),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('engine_mode', sa.String(length=20), nullable=False),
    sa.Column('range_start', sa.Date(), nullable=False),
    sa.Column('range_end', sa.Date(), nullable=False),
    sa.Column('range_scope', sa.String(length=20), nullable=False),
    sa.Column('allocation_id', sa.Uuid(), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=False),
    sa.Column('fingerprint_version', sa.Integer(), nullable=False),
    sa.Column('placements_digest', sa.String(length=64), nullable=False),
    sa.Column('placement_count', sa.Integer(), nullable=False),
    sa.Column('unscheduled_count', sa.Integer(), nullable=False),
    sa.Column('total_score', sa.Float(), nullable=False),
    _instant('generated_at'),
    sa.CheckConstraint("engine_mode IN ('precise_greedy', 'adhd_friendly')", name='ck_schedule_generation_revisions_mode'),
    sa.CheckConstraint('range_start <= planned_date AND planned_date <= range_end',
                       name='ck_schedule_generation_revisions_range'),
    sa.CheckConstraint('placement_count >= 0 AND unscheduled_count >= 0', name='ck_schedule_generation_revisions_counts')
    )
    op.create_table('execution_revisions',
    *_revision_key('execution_revisions'),
    sa.Column('legacy_id', sa.String(length=200), nullable=True),
    sa.Column('task_id', sa.Uuid(), nullable=True),
    sa.Column('scheduled_task_id', sa.Uuid(), nullable=True),
    sa.Column('historical_reference', sa.Boolean(), nullable=False),
    sa.Column('task_name', sa.String(length=500), nullable=False),
    sa.Column('category', sa.String(length=100), nullable=False),
    sa.Column('tag', sa.String(length=100), nullable=False),
    sa.Column('planned_date', sa.Integer(), nullable=True),
    sa.Column('planned_start', sa.Integer(), nullable=True),
    sa.Column('planned_end', sa.Integer(), nullable=True),
    sa.Column('planned_duration', sa.Integer(), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('actual_active_duration_minutes', sa.Float(), nullable=True),
    sa.Column('duration_variance_minutes', sa.Float(), nullable=True),
    sa.Column('start_delay_minutes', sa.Float(), nullable=True),
    sa.Column('focus_rating', sa.Integer(), nullable=True),
    sa.Column('energy_rating', sa.Integer(), nullable=True),
    sa.Column('interruption_count', sa.Integer(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('canonical_planned_date', sa.Date(), nullable=True),
    sa.Column('canonical_timezone', sa.String(length=64), nullable=True),
    _instant('canonical_planned_start', nullable=True),
    _instant('canonical_planned_end', nullable=True),
    _instant('actual_first_start_at', nullable=True),
    _instant('actual_final_end_at', nullable=True),
    sa.CheckConstraint(
        "status IN ('scheduled', 'in_progress', 'paused', 'completed', 'skipped', 'cancelled')",
        name='ck_execution_revisions_status'),
    sa.CheckConstraint('priority BETWEEN 1 AND 10', name='ck_execution_revisions_priority'),
    sa.CheckConstraint('focus_rating IS NULL OR focus_rating BETWEEN 1 AND 5', name='ck_execution_revisions_focus'),
    sa.CheckConstraint('energy_rating IS NULL OR energy_rating BETWEEN 1 AND 5', name='ck_execution_revisions_energy'),
    sa.CheckConstraint('interruption_count IS NULL OR interruption_count >= 0',
                       name='ck_execution_revisions_interruptions'),
    sa.CheckConstraint('scheduled_task_id IS NULL OR task_id IS NOT NULL', name='ck_execution_revisions_link')
    )
    op.create_table('execution_revision_sessions',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('revision_id', sa.Uuid(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    _instant('started_at'),
    _instant('ended_at', nullable=True),
    _position('execution_revision_sessions'),
    sa.CheckConstraint('ended_at IS NULL OR ended_at >= started_at', name='ck_execution_revision_sessions_order'),
    _child_fk('execution_revision_sessions', 'execution_revisions', ('revision_id',), ('id',)),
    sa.PrimaryKeyConstraint('user_id', 'revision_id', 'position')
    )


def upgrade() -> None:
    postgres = op.get_bind().dialect.name == 'postgresql'

    with op.batch_alter_table('tasks') as batch:
        for column in _recurrence_columns():
            batch.add_column(column)
    with op.batch_alter_table('preferences') as batch:
        for column in _preference_scalar_columns():
            batch.add_column(column)
    _create_live_children()

    # The target of an execution's (user, task, placement) reference; also serves placements-by-task queries.
    op.create_index('uq_placements_user_task_id', 'placements', ['user_id', 'task_id', 'id'], unique=True)
    with op.batch_alter_table('executions') as batch:
        batch.add_column(sa.Column('linked_task_id', sa.Uuid(), nullable=True))
        batch.add_column(sa.Column('linked_placement_id', sa.Uuid(), nullable=True))
        batch.create_foreign_key('fk_executions_task', 'tasks', ['user_id', 'linked_task_id'], ['user_id', 'id'])
        batch.create_foreign_key('fk_executions_placement', 'placements',
                                 ['user_id', 'linked_task_id', 'linked_placement_id'], ['user_id', 'task_id', 'id'])
        # PlanningService's execution facts of tasks: WHERE user_id = ? AND task_id IN (...).
        batch.create_index('ix_executions_user_task', ['user_id', 'task_id'])

    _create_revisions(postgres)

    with op.batch_alter_table('change_log') as batch:
        batch.add_column(sa.Column('revision_id', sa.Uuid(), nullable=True))
        batch.create_foreign_key('fk_change_log_revision', 'record_revisions', ['user_id', 'revision_id'],
                                 ['user_id', 'id'])
    with op.batch_alter_table('sync_operations') as batch:
        batch.add_column(sa.Column('record_revision_id', sa.Uuid(), nullable=True))
        batch.add_column(sa.Column('error_code', sa.String(length=40), nullable=True))
        batch.add_column(sa.Column('error_message', sa.Text(), nullable=True))
        batch.add_column(sa.Column('error_supplied_version', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('error_current_version', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('error_current_revision_id', sa.Uuid(), nullable=True))
        batch.add_column(sa.Column('error_conflicting_revision_id', sa.Uuid(), nullable=True))
        batch.add_column(sa.Column('error_reason', sa.String(length=60), nullable=True))
        batch.add_column(sa.Column('error_failed_op_id', sa.Uuid(), nullable=True))
        batch.add_column(sa.Column('error_problems_present', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch.create_foreign_key('fk_sync_operations_record', 'record_revisions', ['user_id', 'record_revision_id'],
                                 ['user_id', 'id'])
        batch.create_foreign_key('fk_sync_operations_current', 'record_revisions',
                                 ['user_id', 'error_current_revision_id'], ['user_id', 'id'])
        batch.create_foreign_key('fk_sync_operations_conflicting', 'record_revisions',
                                 ['user_id', 'error_conflicting_revision_id'], ['user_id', 'id'])
    op.create_table('sync_operation_problems',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('op_id', sa.Uuid(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    _position('sync_operation_problems'),
    _child_fk('sync_operation_problems', 'sync_operations', ('op_id',), ('op_id',)),
    sa.PrimaryKeyConstraint('user_id', 'op_id', 'position')
    )
    op.create_table('sync_operation_problem_locations',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('op_id', sa.Uuid(), nullable=False),
    sa.Column('problem_position', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('part', sa.Text(), nullable=False),
    _position('sync_operation_problem_locations'),
    _child_fk('sync_operation_problem_locations', 'sync_operation_problems', ('op_id', 'problem_position'),
              ('op_id', 'position')),
    sa.PrimaryKeyConstraint('user_id', 'op_id', 'problem_position', 'position')
    )


def downgrade() -> None:
    op.drop_table('sync_operation_problem_locations')
    op.drop_table('sync_operation_problems')
    with op.batch_alter_table('sync_operations') as batch:
        for name in ('fk_sync_operations_conflicting', 'fk_sync_operations_current', 'fk_sync_operations_record'):
            batch.drop_constraint(name, type_='foreignkey')
        for name in ('error_problems_present', 'error_failed_op_id', 'error_reason', 'error_conflicting_revision_id',
                     'error_current_revision_id', 'error_current_version', 'error_supplied_version', 'error_message',
                     'error_code', 'record_revision_id'):
            batch.drop_column(name)
    with op.batch_alter_table('change_log') as batch:
        batch.drop_constraint('fk_change_log_revision', type_='foreignkey')
        batch.drop_column('revision_id')

    for table in ('execution_revision_sessions', 'execution_revisions', 'schedule_generation_revisions',
                  'preference_revision_related_tags', 'preference_revision_tag_relations',
                  'preference_revision_category_windows', 'preference_revision_category_multipliers',
                  'preference_revisions', 'placement_revisions', 'fixed_block_revisions',
                  'task_revision_recurrence_weekdays', 'task_revision_dependencies', 'task_revision_preferred_dates',
                  'task_revision_tags', 'task_revisions', 'project_revisions', 'record_revisions'):
        op.drop_table(table)

    with op.batch_alter_table('executions') as batch:
        batch.drop_index('ix_executions_user_task')
        batch.drop_constraint('fk_executions_placement', type_='foreignkey')
        batch.drop_constraint('fk_executions_task', type_='foreignkey')
        batch.drop_column('linked_placement_id')
        batch.drop_column('linked_task_id')
    op.drop_index('uq_placements_user_task_id', table_name='placements')

    for table in ('preference_related_tags', 'preference_tag_relations', 'preference_category_windows',
                  'preference_category_multipliers', 'task_recurrence_weekdays', 'task_preferred_dates', 'task_tags'):
        op.drop_table(table)
    with op.batch_alter_table('preferences') as batch:
        for column in reversed(_preference_scalar_columns()):
            batch.drop_column(column.name)
    with op.batch_alter_table('tasks') as batch:
        for column in reversed(_recurrence_columns()):
            batch.drop_column(column.name)
