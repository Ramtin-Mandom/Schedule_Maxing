"""Budget Future items (app/budget): income ideas and wishlist items.

Expand only -- new tables, so a server of the previous version keeps working
against the upgraded schema:

    budget_ideas, budget_idea_revisions       a possible income: name, expected amount, the amount
                                              received or secured so far, description
    budget_wishes, budget_wish_revisions      a wished-for purchase without a date: name, amount,
                                              expense category, optional project (composite foreign
                                              key), description, and once turned into an expense the
                                              expense (composite foreign key) and its date

'budget_idea' and 'budget_wish' join the revision entity types. No existing row changes.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None

OLD_ENTITY_TYPES = ('project', 'task', 'fixed_block', 'placement', 'preference', 'schedule_generation', 'execution',
                    'task_type', 'budget_balance', 'budget_entry', 'budget_override')
NEW_ENTITY_TYPES = (*OLD_ENTITY_TYPES, 'budget_idea', 'budget_wish')
#: Frozen copy of backend/models.py's value.
MAX_AMOUNT = 10 ** 13 - 1


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _live(table: str) -> list:
    return [
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint('version > 0', name=f'ck_{table}_version'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('user_id', 'id'),
    ]


def _revision_key(table: str) -> list:
    return [
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['user_id', 'id'], ['record_revisions.user_id', 'record_revisions.id'],
                                name=f'fk_{table}_revision', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', 'id'),
    ]


def _idea(table: str) -> list:
    return [
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('expected_amount_minor', sa.BigInteger(), nullable=False),
        sa.Column('secured_amount_minor', sa.BigInteger(), nullable=False),
        sa.Column('description', sa.String(length=4000), nullable=True),
        sa.CheckConstraint('length(name) > 0', name=f'ck_{table}_name'),
        sa.CheckConstraint(f'expected_amount_minor BETWEEN 1 AND {MAX_AMOUNT}', name=f'ck_{table}_expected'),
        sa.CheckConstraint(f'secured_amount_minor BETWEEN 0 AND {MAX_AMOUNT}', name=f'ck_{table}_secured'),
    ]


def _wish(table: str) -> list:
    return [
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('amount_minor', sa.BigInteger(), nullable=False),
        sa.Column('category', sa.String(length=100), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=True),
        sa.Column('description', sa.String(length=4000), nullable=True),
        sa.Column('converted_entry_id', sa.Uuid(), nullable=True),
        sa.Column('converted_on', sa.Date(), nullable=True),
        sa.CheckConstraint('length(name) > 0 AND length(category) > 0', name=f'ck_{table}_text'),
        sa.CheckConstraint(f'amount_minor BETWEEN 1 AND {MAX_AMOUNT}', name=f'ck_{table}_amount'),
        sa.CheckConstraint('(converted_entry_id IS NULL) = (converted_on IS NULL)', name=f'ck_{table}_converted'),
    ]


def upgrade() -> None:
    op.create_table('budget_ideas', *_idea('budget_ideas'), *_live('budget_ideas'))
    op.create_table(
        'budget_wishes', *_wish('budget_wishes'), *_live('budget_wishes'),
        sa.ForeignKeyConstraint(['user_id', 'project_id'], ['projects.user_id', 'projects.id'],
                                name='fk_budget_wishes_project'),
        sa.ForeignKeyConstraint(['user_id', 'converted_entry_id'], ['budget_entries.user_id', 'budget_entries.id'],
                                name='fk_budget_wishes_entry'),
    )
    op.create_table('budget_idea_revisions', *_revision_key('budget_idea_revisions'), *_idea('budget_idea_revisions'))
    op.create_table('budget_wish_revisions', *_revision_key('budget_wish_revisions'), *_wish('budget_wish_revisions'))
    with op.batch_alter_table('record_revisions') as batch:
        batch.drop_constraint('ck_record_revisions_entity_type', type_='check')
        batch.create_check_constraint('ck_record_revisions_entity_type', _in('entity_type', NEW_ENTITY_TYPES))


def downgrade() -> None:
    # For tests and disposable databases only (docs/backend.md); fails while such a revision exists, like 0019's.
    with op.batch_alter_table('record_revisions') as batch:
        batch.drop_constraint('ck_record_revisions_entity_type', type_='check')
        batch.create_check_constraint('ck_record_revisions_entity_type', _in('entity_type', OLD_ENTITY_TYPES))
    op.drop_table('budget_wish_revisions')
    op.drop_table('budget_idea_revisions')
    op.drop_table('budget_wishes')
    op.drop_table('budget_ideas')
