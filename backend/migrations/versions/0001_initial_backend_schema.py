"""Initial backend schema: accounts, the user-scoped synchronizable records, and the change log.

Revision ID: 0001
Revises: 
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None

#: JSON documents: JSONB on PostgreSQL, JSON text elsewhere (backend.database.JSONDocument).
JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
#: Partial unique indexes over live (not deleted) rows only.
LIVE_ONLY = {'sqlite_where': sa.text('deleted_at IS NULL'), 'postgresql_where': sa.text('deleted_at IS NULL')}


def upgrade() -> None:
    op.create_table('users',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=True),
    sa.Column('display_name', sa.String(length=200), nullable=True),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('change_seq', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.CheckConstraint('change_seq >= 0', name='ck_users_change_seq'),
    sa.CheckConstraint('length(email) > 0', name='ck_users_email_nonempty'),
    sa.CheckConstraint('version > 0', name='ck_users_version'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email'),
    sa.UniqueConstraint('username')
    )
    op.create_table('change_log',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('seq', sa.BigInteger(), nullable=False),
    sa.Column('entity_type', sa.String(length=40), nullable=False),
    sa.Column('entity_id', sa.Uuid(), nullable=False),
    sa.Column('operation', sa.String(length=10), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('payload', JSON_DOCUMENT, nullable=False),
    sa.CheckConstraint("operation IN ('upsert', 'delete')", name='ck_change_log_operation'),
    sa.CheckConstraint('seq > 0', name='ck_change_log_seq'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('user_id', 'seq')
    )
    op.create_index('ix_change_log_entity', 'change_log', ['user_id', 'entity_type', 'entity_id'], unique=False)

    op.create_table('executions',
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
    sa.Column('canonical_planned_start', sa.DateTime(timezone=True), nullable=True),
    sa.Column('canonical_planned_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('actual_first_start_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('actual_final_end_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint(
        "status IN ('scheduled', 'in_progress', 'paused', 'completed', 'skipped', 'cancelled')",
        name='ck_executions_status',
    ),
    sa.CheckConstraint('energy_rating IS NULL OR energy_rating BETWEEN 1 AND 5', name='ck_executions_energy'),
    sa.CheckConstraint('focus_rating IS NULL OR focus_rating BETWEEN 1 AND 5', name='ck_executions_focus'),
    sa.CheckConstraint('interruption_count IS NULL OR interruption_count >= 0', name='ck_executions_interruptions'),
    sa.CheckConstraint('priority BETWEEN 1 AND 10', name='ck_executions_priority'),
    sa.CheckConstraint('scheduled_task_id IS NULL OR task_id IS NOT NULL', name='ck_executions_link'),
    sa.CheckConstraint('version > 0', name='ck_executions_version'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('user_id', 'id')
    )
    op.create_index('uq_executions_user_legacy_id', 'executions', ['user_id', 'legacy_id'], unique=True)
    op.create_index('uq_executions_user_placement', 'executions', ['user_id', 'scheduled_task_id'], unique=True)

    op.create_table('fixed_blocks',
    sa.Column('label', sa.String(length=500), nullable=False),
    sa.Column('category', sa.String(length=100), nullable=False),
    sa.Column('planned_date', sa.Date(), nullable=False),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('planned_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('planned_end', sa.DateTime(timezone=True), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('length(category) > 0', name='ck_fixed_blocks_category'),
    sa.CheckConstraint('length(label) > 0', name='ck_fixed_blocks_label'),
    sa.CheckConstraint('planned_end > planned_start', name='ck_fixed_blocks_order'),
    sa.CheckConstraint('version > 0', name='ck_fixed_blocks_version'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('user_id', 'id')
    )
    op.create_index('ix_fixed_blocks_user_date', 'fixed_blocks', ['user_id', 'planned_date'], unique=False)

    op.create_table('preferences',
    sa.Column('scope', sa.String(length=10), nullable=False),
    sa.Column('scope_date', sa.Date(), nullable=True),
    sa.Column('scope_key', sa.String(length=16), nullable=False),
    sa.Column('optimizer_mode', sa.String(length=20), nullable=True),
    sa.Column('overrides', JSON_DOCUMENT, nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("(scope = 'date') = (scope_date IS NOT NULL)", name='ck_preferences_scope_date'),
    sa.CheckConstraint(
        "optimizer_mode IS NULL OR optimizer_mode IN ('precise_greedy', 'adhd_friendly')", name='ck_preferences_mode'
    ),
    sa.CheckConstraint("scope IN ('user', 'date')", name='ck_preferences_scope'),
    sa.CheckConstraint('version > 0', name='ck_preferences_version'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('user_id', 'id')
    )
    op.create_index('uq_preferences_live_scope', 'preferences', ['user_id', 'scope_key'], unique=True, **LIVE_ONLY)

    op.create_table('projects',
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('length(name) > 0', name='ck_projects_name'),
    sa.CheckConstraint('version > 0', name='ck_projects_version'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('user_id', 'id')
    )
    op.create_table('schedule_generations',
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
    sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("engine_mode IN ('precise_greedy', 'adhd_friendly')", name='ck_schedule_generations_mode'),
    sa.CheckConstraint('placement_count >= 0 AND unscheduled_count >= 0', name='ck_schedule_generations_counts'),
    sa.CheckConstraint('range_start <= planned_date AND planned_date <= range_end', name='ck_schedule_generations_range'),
    sa.CheckConstraint('version > 0', name='ck_schedule_generations_version'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('user_id', 'id')
    )
    op.create_index(
        'uq_schedule_generations_live_date', 'schedule_generations', ['user_id', 'planned_date'], unique=True, **LIVE_ONLY
    )

    op.create_table('tasks',
    sa.Column('project_id', sa.Uuid(), nullable=True),
    sa.Column('name', sa.String(length=500), nullable=False),
    sa.Column('category', sa.String(length=100), nullable=False),
    sa.Column('tags', JSON_DOCUMENT, nullable=False),
    sa.Column('estimated_duration_minutes', sa.Integer(), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('required', sa.Boolean(), nullable=False),
    sa.Column('required_date', sa.Date(), nullable=True),
    sa.Column('preferred_dates', JSON_DOCUMENT, nullable=False),
    sa.Column('preferred_window_start_minute', sa.Integer(), nullable=True),
    sa.Column('preferred_window_end_minute', sa.Integer(), nullable=True),
    sa.Column('deadline', sa.String(length=40), nullable=True),
    sa.Column('deadline_utc', sa.DateTime(timezone=True), nullable=True),
    sa.Column('recurrence', JSON_DOCUMENT, nullable=True),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('(deadline IS NULL) = (deadline_utc IS NULL)', name='ck_tasks_deadline'),
    sa.CheckConstraint(
        '(preferred_window_start_minute IS NULL) = (preferred_window_end_minute IS NULL)', name='ck_tasks_window'
    ),
    sa.CheckConstraint('estimated_duration_minutes > 0', name='ck_tasks_duration'),
    sa.CheckConstraint('length(category) > 0', name='ck_tasks_category'),
    sa.CheckConstraint('length(name) > 0', name='ck_tasks_name'),
    sa.CheckConstraint('priority BETWEEN 1 AND 10', name='ck_tasks_priority'),
    sa.CheckConstraint('version > 0', name='ck_tasks_version'),
    sa.ForeignKeyConstraint(['user_id', 'project_id'], ['projects.user_id', 'projects.id'], name='fk_tasks_project'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('user_id', 'id')
    )
    op.create_index('ix_tasks_user_project', 'tasks', ['user_id', 'project_id'], unique=False)

    op.create_table('work_sessions',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('execution_id', sa.Uuid(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('ended_at IS NULL OR ended_at >= started_at', name='ck_work_sessions_order'),
    sa.CheckConstraint('position >= 0', name='ck_work_sessions_position'),
    sa.ForeignKeyConstraint(
        ['user_id', 'execution_id'], ['executions.user_id', 'executions.id'], name='fk_work_sessions_execution'
    ),
    sa.PrimaryKeyConstraint('user_id', 'execution_id', 'position')
    )
    op.create_table('placements',
    sa.Column('task_id', sa.Uuid(), nullable=False),
    sa.Column('planned_date', sa.Date(), nullable=False),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('planned_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('planned_end', sa.DateTime(timezone=True), nullable=False),
    sa.Column('score', sa.Float(), nullable=False),
    sa.Column('optimization_metadata', JSON_DOCUMENT, nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('planned_end > planned_start', name='ck_placements_order'),
    sa.CheckConstraint('version > 0', name='ck_placements_version'),
    sa.ForeignKeyConstraint(['user_id', 'task_id'], ['tasks.user_id', 'tasks.id'], name='fk_placements_task'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('user_id', 'id')
    )
    op.create_index('ix_placements_user_date', 'placements', ['user_id', 'planned_date'], unique=False)
    op.create_index('ix_placements_user_task', 'placements', ['user_id', 'task_id'], unique=False)

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



def downgrade() -> None:
    op.drop_index('ix_task_dependencies_depends_on', table_name='task_dependencies')

    op.drop_table('task_dependencies')
    op.drop_index('ix_placements_user_task', table_name='placements')
    op.drop_index('ix_placements_user_date', table_name='placements')

    op.drop_table('placements')
    op.drop_table('work_sessions')
    op.drop_index('ix_tasks_user_project', table_name='tasks')

    op.drop_table('tasks')
    op.drop_index('uq_schedule_generations_live_date', table_name='schedule_generations', **LIVE_ONLY)

    op.drop_table('schedule_generations')
    op.drop_table('projects')
    op.drop_index('uq_preferences_live_scope', table_name='preferences', **LIVE_ONLY)

    op.drop_table('preferences')
    op.drop_index('ix_fixed_blocks_user_date', table_name='fixed_blocks')

    op.drop_table('fixed_blocks')
    op.drop_index('uq_executions_user_placement', table_name='executions')
    op.drop_index('uq_executions_user_legacy_id', table_name='executions')

    op.drop_table('executions')
    op.drop_index('ix_change_log_entity', table_name='change_log')

    op.drop_table('change_log')
    op.drop_table('users')
