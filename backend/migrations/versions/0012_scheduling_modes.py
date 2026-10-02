"""Five scheduling modes (docs/scheduling-modes.md).

Widens the mode CHECK constraints so a preference layer may choose
early_finish, night_owl or catch_up, and a schedule record may name them:

    preferences, preference_revisions                      optimizer_mode
    schedule_generations, schedule_generation_revisions    engine_mode

No row changes: "precise_greedy" keeps meaning Normal and "adhd_friendly"
ADHD. A server of the previous version keeps working against the upgraded
schema (it never writes the new values). Downgrade (disposable databases
only) fails if a row uses a new mode -- the narrow constraint cannot hold.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

from alembic import op

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None

OLD = ('precise_greedy', 'adhd_friendly')
NEW = (*OLD, 'early_finish', 'night_owl', 'catch_up')
TABLES = (
    ('preferences', 'ck_preferences_mode', 'optimizer_mode', True),
    ('preference_revisions', 'ck_preference_revisions_mode', 'optimizer_mode', True),
    ('schedule_generations', 'ck_schedule_generations_mode', 'engine_mode', False),
    ('schedule_generation_revisions', 'ck_schedule_generation_revisions_mode', 'engine_mode', False),
)


def _check(column: str, nullable: bool, modes: tuple[str, ...]) -> str:
    allowed = f"{column} IN ({', '.join(repr(mode) for mode in modes)})"
    return f"{column} IS NULL OR {allowed}" if nullable else allowed


def _replace(modes: tuple[str, ...]) -> None:
    for table, name, column, nullable in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(name, type_='check')
            batch.create_check_constraint(name, _check(column, nullable, modes))


def upgrade() -> None:
    _replace(NEW)


def downgrade() -> None:
    _replace(OLD)
