"""Budget records (app/budget): balances, income/expense entries and occurrence edits.

Expand only -- new tables, so a server of the previous version keeps working
against the upgraded schema (it never reads them):

    budget_balances, budget_balance_revisions     a user-entered balance and its as-of date
    budget_entries, budget_entry_revisions        named income/expense entries: an amount in integer
                                                  minor units, a category, a start date, a recurrence
                                                  (none/daily/weekly/biweekly/monthly), an optional end
                                                  date and an optional project (composite foreign key:
                                                  only a project of the same user)
    budget_overrides, budget_override_revisions   an edit of one occurrence of an entry (skipped, or
                                                  modified values), keyed by its scheduled date; at most
                                                  one live edit per occurrence

'budget_balance', 'budget_entry' and 'budget_override' join the revision
entity types. No existing row changes.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None

OLD_ENTITY_TYPES = ('project', 'task', 'fixed_block', 'placement', 'preference', 'schedule_generation', 'execution',
                    'task_type')
NEW_ENTITY_TYPES = (*OLD_ENTITY_TYPES, 'budget_balance', 'budget_entry', 'budget_override')
#: Frozen copies of backend/models.py's values (a migration never follows later code changes).
KINDS = ('income', 'expense')
RECURRENCES = ('none', 'daily', 'weekly', 'biweekly', 'monthly')
STATES = ('modified', 'skipped')
MAX_AMOUNT = 10 ** 13 - 1


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _meta() -> list[sa.Column]:
    return [
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    ]


def _revision_key(table: str) -> list:
    return [
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['user_id', 'id'], ['record_revisions.user_id', 'record_revisions.id'],
                                name=f'fk_{table}_revision', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', 'id'),
    ]


def _live(table: str) -> list:
    return [
        *_meta(),
        sa.CheckConstraint('version > 0', name=f'ck_{table}_version'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('user_id', 'id'),
    ]


def _balance(table: str) -> list:
    return [
        sa.Column('amount_minor', sa.BigInteger(), nullable=False),
        sa.Column('as_of', sa.Date(), nullable=False),
        sa.CheckConstraint(f'amount_minor BETWEEN -{MAX_AMOUNT} AND {MAX_AMOUNT}', name=f'ck_{table}_amount'),
    ]


def _entry(table: str) -> list:
    return [
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('amount_minor', sa.BigInteger(), nullable=False),
        sa.Column('category', sa.String(length=100), nullable=False),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('recurrence', sa.String(length=10), nullable=False),
        sa.Column('end_date', sa.Date(), nullable=True),
        sa.Column('project_id', sa.Uuid(), nullable=True),
        sa.CheckConstraint(_in('kind', KINDS), name=f'ck_{table}_kind'),
        sa.CheckConstraint(_in('recurrence', RECURRENCES), name=f'ck_{table}_recurrence'),
        sa.CheckConstraint('length(name) > 0 AND length(category) > 0', name=f'ck_{table}_text'),
        sa.CheckConstraint(f'amount_minor BETWEEN 1 AND {MAX_AMOUNT}', name=f'ck_{table}_amount'),
        sa.CheckConstraint("end_date IS NULL OR (recurrence <> 'none' AND end_date >= start_date)",
                           name=f'ck_{table}_end_date'),
    ]


def _override(table: str) -> list:
    return [
        sa.Column('entry_id', sa.Uuid(), nullable=False),
        sa.Column('occurrence_date', sa.Date(), nullable=False),
        sa.Column('state', sa.String(length=10), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=True),
        sa.Column('amount_minor', sa.BigInteger(), nullable=True),
        sa.Column('date', sa.Date(), nullable=True),
        sa.Column('category', sa.String(length=100), nullable=True),
        sa.Column('project_id', sa.Uuid(), nullable=True),
        sa.CheckConstraint(_in('state', STATES), name=f'ck_{table}_state'),
        sa.CheckConstraint(f'amount_minor IS NULL OR amount_minor BETWEEN 1 AND {MAX_AMOUNT}',
                           name=f'ck_{table}_amount'),
        sa.CheckConstraint("state = 'modified' OR (name IS NULL AND amount_minor IS NULL AND date IS NULL "
                           "AND category IS NULL AND project_id IS NULL)", name=f'ck_{table}_skipped'),
    ]


def upgrade() -> None:
    op.create_table('budget_balances', *_balance('budget_balances'), *_live('budget_balances'))
    op.create_table(
        'budget_entries', *_entry('budget_entries'), *_live('budget_entries'),
        sa.ForeignKeyConstraint(['user_id', 'project_id'], ['projects.user_id', 'projects.id'],
                                name='fk_budget_entries_project'),
    )
    op.create_table(
        'budget_overrides', *_override('budget_overrides'), *_live('budget_overrides'),
        sa.ForeignKeyConstraint(['user_id', 'entry_id'], ['budget_entries.user_id', 'budget_entries.id'],
                                name='fk_budget_overrides_entry'),
        sa.ForeignKeyConstraint(['user_id', 'project_id'], ['projects.user_id', 'projects.id'],
                                name='fk_budget_overrides_project'),
    )
    op.create_index('uq_budget_overrides_live_slot', 'budget_overrides', ['user_id', 'entry_id', 'occurrence_date'],
                    unique=True, sqlite_where=sa.text('deleted_at IS NULL'),
                    postgresql_where=sa.text('deleted_at IS NULL'))
    op.create_table('budget_balance_revisions', *_revision_key('budget_balance_revisions'),
                    *_balance('budget_balance_revisions'))
    op.create_table('budget_entry_revisions', *_revision_key('budget_entry_revisions'),
                    *_entry('budget_entry_revisions'))
    op.create_table('budget_override_revisions', *_revision_key('budget_override_revisions'),
                    *_override('budget_override_revisions'))
    with op.batch_alter_table('record_revisions') as batch:
        batch.drop_constraint('ck_record_revisions_entity_type', type_='check')
        batch.create_check_constraint('ck_record_revisions_entity_type', _in('entity_type', NEW_ENTITY_TYPES))


def downgrade() -> None:
    # For tests and disposable databases only (docs/backend.md): budget records are dropped with their tables.
    # It fails while a budget revision exists (the narrow entity-type constraint cannot hold), like 0013's.
    with op.batch_alter_table('record_revisions') as batch:
        batch.drop_constraint('ck_record_revisions_entity_type', type_='check')
        batch.create_check_constraint('ck_record_revisions_entity_type', _in('entity_type', OLD_ENTITY_TYPES))
    op.drop_table('budget_override_revisions')
    op.drop_table('budget_entry_revisions')
    op.drop_table('budget_balance_revisions')
    op.drop_index('uq_budget_overrides_live_slot', table_name='budget_overrides')
    op.drop_table('budget_overrides')
    op.drop_table('budget_entries')
    op.drop_table('budget_balances')
