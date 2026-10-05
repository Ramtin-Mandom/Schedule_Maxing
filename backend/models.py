"""
backend/models.py

The server's database schema (SQLAlchemy 2 ORM). The versioned Alembic
migrations in backend/migrations/ create exactly this schema; the
migration test compares them so they cannot drift apart. It is the one
storage model for the server and for any future direct desktop adapter.

Design:
    - Every user-owned table's primary key is (user_id, id). Client-chosen
      UUIDs therefore never collide with, or reveal, another user's record,
      and every relationship is a *composite* foreign key that includes
      user_id -- a row can only ever reference rows of the same user, which
      the database itself enforces.
    - Every synchronizable record has the metadata of docs/sync-contract.md:
      created_at/updated_at (server clock, UTC), a server `version` (1 on
      create, +1 per accepted mutation; used by SQLAlchemy as the
      optimistic-concurrency column, so every UPDATE also compares it), and
      a `deleted_at` tombstone. Rows are never physically deleted by the API.
    - Structured content is relational (migration 0006): a task's tags,
      preferred dates, dependencies and recurrence weekdays are ordered or
      keyed child rows, recurrence is scalar columns (the desktop SQLite
      schema's own layout), and a preference layer is scalar columns plus
      category/tag-relation child rows that keep the absent/value/explicit-
      clear states of app.planning.preferences.PreferenceOverrides.
    - The only JSON column is a placement's optimization_metadata: a small
      (backend/record_mapping.py: MAX_OPTIMIZATION_METADATA_BYTES),
      genuinely unstructured extension object that the wire contract and
      the desktop allow. Nothing writes a task list or schedule into it.
    - change_log is the per-user, gap-free, commit-ordered feed of accepted
      mutations (see backend/mutations.py for the ordering strategy). Each
      entry and each recorded sync outcome references an immutable record
      revision (record_revisions + one typed table per entity type), so the
      feed and sync retries keep returning the historical record, never the
      current row.
    - Executions keep their historical task/placement identity
      (task_id/scheduled_task_id, possibly unresolved) and, separately, the
      database-enforced owned references linked_task_id/linked_placement_id
      (the placement must belong to that task and user).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    false,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column, relationship

from backend.database import JSONDocument, UTCDateTime

EXECUTION_STATUSES = ("scheduled", "in_progress", "paused", "completed", "skipped", "cancelled")
OPTIMIZER_MODES = ("precise_greedy", "adhd_friendly", "early_finish", "night_owl", "catch_up")
RECURRENCE_FREQUENCIES = ("daily", "weekly", "monthly")
#: app.planning.models.OccurrenceState (docs/recurrence.md).
OCCURRENCE_STATES = ("modified", "skipped", "deleted", "superseded")
ENTITY_TYPES = ("project", "task", "fixed_block", "placement", "preference", "schedule_generation", "execution",
                "task_type")
SYNC_STATUSES = ("applied", "conflict", "rejected")
#: app.planning.models.PlacementRemovalReason (docs/execution-rescheduling.md).
PLACEMENT_REMOVAL_REASONS = ("rescheduled", "regenerated", "deleted", "task_deleted", "reset")
#: app.planning.models.PlacementOrigin and app.execution.models.CancelReason (docs/execution-rescheduling.md).
PLACEMENT_ORIGINS = ("generated", "manual")
CANCEL_REASONS = ("user", "rescheduled", "superseded")

#: The reward fields of app.planning.preferences.RewardPreferencesOverride stored as reward_<name> columns.
REWARD_FLOAT_FIELDS = (
    "weight_importance", "weight_time_bonus", "weight_tag_relation", "weight_fragmentation_penalty",
    "weight_category_bonus", "short_gap_bonus_weight", "short_gap_bonus_cap",
)
REWARD_INT_FIELDS = (
    "max_time_distance_minutes", "same_tag_window_minutes", "min_gap_between_tasks_minutes",
    "short_gap_bonus_max_minutes",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _child_of(table: str, parent: str, columns: tuple[str, ...], parent_columns: tuple[str, ...]) -> ForeignKeyConstraint:
    """A value row's owner-aware composite foreign key to its parent row (removed with it)."""
    return ForeignKeyConstraint(
        ["user_id", *columns], [f"{parent}.user_id", *(f"{parent}.{name}" for name in parent_columns)],
        name=f"fk_{table}_parent", ondelete="CASCADE",
    )


def _position_check(table: str) -> CheckConstraint:
    return CheckConstraint("position >= 0", name=f"ck_{table}_position")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    #: Normalized (NFKC, trimmed, lower-case) -- uniqueness is enforced here, by the database.
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The last change-log sequence number allocated for this user (see backend/mutations.py).
    change_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    #: Raised by every credential change (a password reset): access tokens and browser sessions carry the epoch
    #: they were issued under, and one from an older epoch is refused (backend/recovery.py). Not the profile version.
    credential_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint("length(email) > 0", name="ck_users_email_nonempty"),
        CheckConstraint("change_seq >= 0", name="ck_users_change_seq"),
        CheckConstraint("version > 0", name="ck_users_version"),
        CheckConstraint("credential_epoch >= 0", name="ck_users_credential_epoch"),
    )
    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}


class _Record:
    """The metadata columns every synchronizable table shares."""

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), primary_key=True)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    @declared_attr.directive
    def __mapper_args__(cls) -> dict:
        # Every UPDATE also compares the version the row was loaded with (see backend/mutations.py).
        return {"version_id_col": cls.__table__.c.version, "version_id_generator": False}


def _record_checks(table: str) -> tuple:
    return (CheckConstraint("version > 0", name=f"ck_{table}_version"),)


# -----------------------------------------------------------------------------
# Content columns, shared by each live table and its revision table, so a
# record and its historical snapshots use the same field <-> column mapping
# (backend/resources.py reads and writes both through the same functions).
# -----------------------------------------------------------------------------


class _ProjectContent:
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


def _project_checks(table: str) -> tuple:
    return (CheckConstraint("length(name) > 0", name=f"ck_{table}_name"),)


class _TaskTypeContent:
    #: The display label of a reusable task type (app.planning.models.TaskType); its id is the identity.
    label: Mapped[str] = mapped_column(String(500), nullable=False)


def _task_type_checks(table: str) -> tuple:
    return (CheckConstraint("length(label) > 0", name=f"ck_{table}_label"),)


class _TaskContent:
    project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    estimated_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The user's productivity value (app.planning.models.Task.points); not a scheduling input.
    points: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    #: The task's reusable type (one of the user's task_types; NULL: not assigned yet). Not a scheduling input.
    task_type_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    required_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    preferred_window_start_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    preferred_window_end_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: ISO 8601 with its original UTC offset (exact round trip), plus a UTC twin for queries.
    deadline: Mapped[str | None] = mapped_column(String(40), nullable=True)
    deadline_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: RecurrenceSpec as scalars (NULL frequency = no recurrence); weekdays are child rows.
    recurrence_frequency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    recurrence_interval: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recurrence_day_of_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recurrence_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    recurrence_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: A series' explicit anchor (local date) and IANA time zone (both NULL: it needs configuration).
    recurrence_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    recurrence_timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: An occurrence's immutable identity: its series (one of the user's tasks) and original local slot date.
    series_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    occurrence_slot: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: An occurrence's exception state (OCCURRENCE_STATES; NULL: it follows its series).
    occurrence_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    #: Provenance: the series version the occurrence was materialized (or last refreshed) from.
    series_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: A series segment's lineage: the series it continues.
    series_predecessor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


def _task_checks(table: str) -> tuple:
    return (
        CheckConstraint("length(name) > 0", name=f"ck_{table}_name"),
        CheckConstraint("length(category) > 0", name=f"ck_{table}_category"),
        CheckConstraint("estimated_duration_minutes > 0", name=f"ck_{table}_duration"),
        CheckConstraint("priority BETWEEN 1 AND 10", name=f"ck_{table}_priority"),
        CheckConstraint("points BETWEEN 0 AND 1000", name=f"ck_{table}_points"),
        CheckConstraint(
            "(preferred_window_start_minute IS NULL) = (preferred_window_end_minute IS NULL)", name=f"ck_{table}_window"
        ),
        CheckConstraint("(deadline IS NULL) = (deadline_utc IS NULL)", name=f"ck_{table}_deadline"),
        CheckConstraint(
            f"recurrence_frequency IS NULL OR {_in('recurrence_frequency', RECURRENCE_FREQUENCIES)}",
            name=f"ck_{table}_recurrence_frequency",
        ),
        CheckConstraint(
            "(recurrence_frequency IS NULL) = (recurrence_interval IS NULL)"
            " AND (recurrence_interval IS NULL OR recurrence_interval > 0)"
            " AND (recurrence_count IS NULL OR recurrence_count > 0)"
            " AND (recurrence_end_date IS NULL OR recurrence_count IS NULL)",
            name=f"ck_{table}_recurrence",
        ),
        CheckConstraint(
            "recurrence_day_of_month IS NULL"
            " OR (recurrence_frequency = 'monthly' AND recurrence_day_of_month BETWEEN 1 AND 31)",
            name=f"ck_{table}_recurrence_day",
        ),
        CheckConstraint(
            "recurrence_frequency IS NOT NULL OR (recurrence_end_date IS NULL AND recurrence_count IS NULL)",
            name=f"ck_{table}_recurrence_bounds",
        ),
        CheckConstraint(
            "(recurrence_start_date IS NULL) = (recurrence_timezone IS NULL)"
            " AND (recurrence_start_date IS NULL OR recurrence_frequency IS NOT NULL)",
            name=f"ck_{table}_recurrence_anchor",
        ),
        CheckConstraint(
            "(series_id IS NULL) = (occurrence_slot IS NULL)"
            " AND (series_id IS NULL OR recurrence_frequency IS NULL)"
            " AND (series_id IS NOT NULL OR (occurrence_state IS NULL AND series_version IS NULL))"
            " AND (series_version IS NULL OR series_version > 0)",
            name=f"ck_{table}_occurrence",
        ),
        CheckConstraint(
            f"occurrence_state IS NULL OR {_in('occurrence_state', OCCURRENCE_STATES)}",
            name=f"ck_{table}_occurrence_state",
        ),
        CheckConstraint(
            "series_predecessor_id IS NULL OR recurrence_start_date IS NOT NULL", name=f"ck_{table}_series_lineage",
        ),
        # The live row's own id and tombstone (a revision row's id is the revision's, its tombstone in the header).
        *((
            CheckConstraint(
                "(series_id IS NULL OR series_id <> id) AND (series_predecessor_id IS NULL OR series_predecessor_id <> id)"
                " AND (occurrence_state IS NULL OR occurrence_state = 'modified' OR deleted_at IS NOT NULL)",
                name="ck_tasks_recurrence_identity",
            ),
        ) if table == "tasks" else ()),
    )


class _FixedBlockContent:
    label: Mapped[str] = mapped_column(String(500), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    planned_date: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    planned_start: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    planned_end: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


def _fixed_block_checks(table: str) -> tuple:
    return (
        CheckConstraint("length(label) > 0", name=f"ck_{table}_label"),
        CheckConstraint("length(category) > 0", name=f"ck_{table}_category"),
        CheckConstraint("planned_end > planned_start", name=f"ck_{table}_order"),
    )


class _PlacementContent:
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    planned_date: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    planned_start: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    planned_end: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    #: The bounded extension object (see the module docstring); never a task list or schedule.
    optimization_metadata: Mapped[dict] = mapped_column(JSONDocument, nullable=False)
    #: The task's category when the placement was saved (a historical snapshot; NULL = not recorded).
    task_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    #: The rest of the planning snapshot (app.planning.models.ScheduledTask): the task's name, points, estimate
    #: and type when the placement was saved; each NULL = not recorded. Its tags are ordered child rows
    #: (task_tag_rows), recorded only when task_tags_recorded -- so "no tags" and "unknown" stay distinct.
    #: task_type_id is history, not a live reference: not a foreign key.
    task_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    task_tags_recorded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    task_points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    task_estimate_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    task_type_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    task_type_label: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: Why a tombstone left the plan (PLACEMENT_REMOVAL_REASONS; NULL while live, or unknown for old tombstones)
    #: and the placement that replaced it. History, not a live reference: not a foreign key.
    removal_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    #: How the placement came to be (PLACEMENT_ORIGINS; NULL = unknown, saved before Milestone 6) and the user's
    #: manual intent: a preserved placement is kept by generation until released.
    origin: Mapped[str | None] = mapped_column(String(20), nullable=True)
    preserved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())


def _placement_checks(table: str) -> tuple:
    return (
        CheckConstraint("planned_end > planned_start", name=f"ck_{table}_order"),
        CheckConstraint(f"origin IS NULL OR {_in('origin', PLACEMENT_ORIGINS)}", name=f"ck_{table}_origin"),
        CheckConstraint("NOT preserved OR (origin IS NOT NULL AND origin = 'manual')", name=f"ck_{table}_preserved"),
        CheckConstraint(
            f"removal_reason IS NULL OR {_in('removal_reason', PLACEMENT_REMOVAL_REASONS)}",
            name=f"ck_{table}_removal_reason",
        ),
        CheckConstraint("superseded_by_id IS NULL OR superseded_by_id <> id", name=f"ck_{table}_superseded_by"),
        CheckConstraint(
            "(task_name IS NULL OR length(task_name) > 0) AND (task_type_label IS NULL OR length(task_type_label) > 0)"
            " AND (task_points IS NULL OR task_points >= 0)"
            " AND (task_estimate_minutes IS NULL OR task_estimate_minutes > 0)",
            name=f"ck_{table}_snapshot",
        ),
        # A coarse database backstop for every writer (the exact 4 KiB compact-JSON limit is enforced by
        # backend/record_mapping.py); PostgreSQL only, because SQLite has no jsonb functions.
        CheckConstraint(
            "jsonb_typeof(optimization_metadata) = 'object' AND octet_length(optimization_metadata::text) <= 8192",
            name=f"ck_{table}_metadata",
        ).ddl_if(dialect="postgresql"),
    )


class _PreferenceContent:
    scope: Mapped[str] = mapped_column(String(10), nullable=False)
    scope_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: "user" or the ISO date: one live layer per (user, scope_key).
    scope_key: Mapped[str] = mapped_column(String(16), nullable=False)
    optimizer_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    #: DayWindowSpec; all three NULL = the layer does not set a day window.
    day_window_start_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    day_window_end_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    day_window_end_day_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: RewardPreferencesOverride scalars; NULL = the layer does not override that field.
    reward_weight_importance: Mapped[float | None] = mapped_column(Float, nullable=True)
    reward_weight_time_bonus: Mapped[float | None] = mapped_column(Float, nullable=True)
    reward_weight_tag_relation: Mapped[float | None] = mapped_column(Float, nullable=True)
    reward_weight_fragmentation_penalty: Mapped[float | None] = mapped_column(Float, nullable=True)
    reward_weight_category_bonus: Mapped[float | None] = mapped_column(Float, nullable=True)
    reward_max_time_distance_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reward_same_tag_window_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reward_min_gap_between_tasks_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reward_short_gap_bonus_weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    reward_short_gap_bonus_max_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reward_short_gap_bonus_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: False = reward.tag_relations is absent (inherit); True = present, even as {} (tag-relation rows).
    reward_tag_relations_present: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )


def _preference_checks(table: str) -> tuple:
    return (
        CheckConstraint("scope IN ('user', 'date')", name=f"ck_{table}_scope"),
        CheckConstraint("(scope = 'date') = (scope_date IS NOT NULL)", name=f"ck_{table}_scope_date"),
        CheckConstraint(f"optimizer_mode IS NULL OR {_in('optimizer_mode', OPTIMIZER_MODES)}", name=f"ck_{table}_mode"),
        CheckConstraint(
            "(day_window_start_minute IS NULL) = (day_window_end_minute IS NULL)"
            " AND (day_window_start_minute IS NULL) = (day_window_end_day_offset IS NULL)",
            name=f"ck_{table}_day_window",
        ),
    )


class _GenerationContent:
    planned_date: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    engine_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    range_start: Mapped[date] = mapped_column(Date, nullable=False)
    range_end: Mapped[date] = mapped_column(Date, nullable=False)
    range_scope: Mapped[str] = mapped_column(String(20), nullable=False)
    allocation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint_version: Mapped[int] = mapped_column(Integer, nullable=False)
    placements_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    placement_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unscheduled_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


def _generation_checks(table: str) -> tuple:
    return (
        CheckConstraint(_in("engine_mode", OPTIMIZER_MODES), name=f"ck_{table}_mode"),
        CheckConstraint("range_start <= planned_date AND planned_date <= range_end", name=f"ck_{table}_range"),
        CheckConstraint("placement_count >= 0 AND unscheduled_count >= 0", name=f"ck_{table}_counts"),
    )


class _ExecutionContent:
    """The TaskExecution snapshot: its audit copy of what was planned is history, not a live reference."""

    legacy_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Historical identity (docs/sync-contract.md section 7): may name a task/placement never persisted.
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    scheduled_task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    historical_reference: Mapped[bool] = mapped_column(Boolean, nullable=False)
    task_name: Mapped[str] = mapped_column(String(500), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    tag: Mapped[str] = mapped_column(String(100), nullable=False)
    planned_date: Mapped[int | None] = mapped_column(Integer, nullable=True)
    planned_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    planned_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    planned_duration: Mapped[int] = mapped_column(Integer, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The task's points when the execution was created (a snapshot); NULL when unknown (older executions).
    points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    actual_active_duration_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_variance_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_delay_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    focus_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    energy_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interruption_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_planned_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    canonical_timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    canonical_planned_start: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    canonical_planned_end: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    actual_first_start_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    actual_final_end_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: Why a cancelled execution was cancelled (CANCEL_REASONS); NULL otherwise or when unknown.
    cancel_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)


def _execution_checks(table: str) -> tuple:
    return (
        CheckConstraint(_in("status", EXECUTION_STATUSES), name=f"ck_{table}_status"),
        CheckConstraint(
            f"cancel_reason IS NULL OR (status = 'cancelled' AND {_in('cancel_reason', CANCEL_REASONS)})",
            name=f"ck_{table}_cancel_reason",
        ),
        CheckConstraint("priority BETWEEN 1 AND 10", name=f"ck_{table}_priority"),
        CheckConstraint("points IS NULL OR points >= 0", name=f"ck_{table}_points"),
        CheckConstraint("focus_rating IS NULL OR focus_rating BETWEEN 1 AND 5", name=f"ck_{table}_focus"),
        CheckConstraint("energy_rating IS NULL OR energy_rating BETWEEN 1 AND 5", name=f"ck_{table}_energy"),
        CheckConstraint("interruption_count IS NULL OR interruption_count >= 0", name=f"ck_{table}_interruptions"),
        CheckConstraint("scheduled_task_id IS NULL OR task_id IS NOT NULL", name=f"ck_{table}_link"),
    )


# -----------------------------------------------------------------------------
# Live records
# -----------------------------------------------------------------------------


class Project(_Record, _ProjectContent, Base):
    __tablename__ = "projects"

    __table_args__ = (*_record_checks("projects"), *_project_checks("projects"))


class TaskType(_Record, _TaskTypeContent, Base):
    __tablename__ = "task_types"

    __table_args__ = (*_record_checks("task_types"), *_task_type_checks("task_types"))


class Task(_Record, _TaskContent, Base):
    __tablename__ = "tasks"

    tag_rows: Mapped[list[TaskTag]] = relationship(
        order_by="TaskTag.position", cascade="all, delete-orphan", lazy="selectin")
    preferred_date_rows: Mapped[list[TaskPreferredDate]] = relationship(
        order_by="TaskPreferredDate.position", cascade="all, delete-orphan", lazy="selectin")
    dependency_rows: Mapped[list[TaskDependency]] = relationship(
        primaryjoin="and_(Task.user_id == TaskDependency.user_id, Task.id == TaskDependency.task_id)",
        foreign_keys="[TaskDependency.user_id, TaskDependency.task_id]",
        order_by="TaskDependency.position", cascade="all, delete-orphan", lazy="selectin")
    recurrence_weekday_rows: Mapped[list[TaskRecurrenceWeekday]] = relationship(
        order_by="TaskRecurrenceWeekday.weekday", cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (
        *_record_checks("tasks"),
        ForeignKeyConstraint(["user_id", "project_id"], ["projects.user_id", "projects.id"], name="fk_tasks_project"),
        # An occurrence's series is one of the same user's tasks (rows are only ever tombstoned, never removed).
        ForeignKeyConstraint(["user_id", "series_id"], ["tasks.user_id", "tasks.id"], name="fk_tasks_series"),
        # A task's type is one of the same user's types: another account's type can never be referenced.
        ForeignKeyConstraint(["user_id", "task_type_id"], ["task_types.user_id", "task_types.id"],
                             name="fk_tasks_task_type"),
        *_task_checks("tasks"),
        Index("ix_tasks_user_project", "user_id", "project_id"),
        Index("ix_tasks_user_task_type", "user_id", "task_type_id"),
        # One record per (user, series, original slot), tombstones included: a suppressed slot stays reserved.
        Index("uq_tasks_user_series_slot", "user_id", "series_id", "occurrence_slot", unique=True,
              sqlite_where=text("series_id IS NOT NULL"), postgresql_where=text("series_id IS NOT NULL")),
        Index("ix_tasks_user_series_predecessor", "user_id", "series_predecessor_id"),
    )


class TaskTag(Base):
    """Task.tags, in order; a repeated tag is kept (the list is stored exactly)."""

    __tablename__ = "task_tags"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    tag: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (_child_of("task_tags", "tasks", ("task_id",), ("id",)), _position_check("task_tags"))


class TaskPreferredDate(Base):
    """Task.preferred_dates, in order (exactly as given)."""

    __tablename__ = "task_preferred_dates"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    preferred_date: Mapped[date] = mapped_column(Date, nullable=False)

    __table_args__ = (
        _child_of("task_preferred_dates", "tasks", ("task_id",), ("id",)), _position_check("task_preferred_dates"),
    )


class TaskDependency(Base):
    """Task.dependency_ids, in order. Both ends must be tasks of the same user (composite foreign keys)."""

    __tablename__ = "task_dependencies"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    depends_on_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["user_id", "task_id"], ["tasks.user_id", "tasks.id"], name="fk_task_dependencies_task"),
        ForeignKeyConstraint(
            ["user_id", "depends_on_id"], ["tasks.user_id", "tasks.id"], name="fk_task_dependencies_depends_on"
        ),
        CheckConstraint("task_id <> depends_on_id", name="ck_task_dependencies_not_self"),
        CheckConstraint("position >= 0", name="ck_task_dependencies_position"),
        Index("ix_task_dependencies_depends_on", "user_id", "depends_on_id"),
    )


class TaskRecurrenceWeekday(Base):
    """RecurrenceSpec.weekdays (a set: the canonical model keeps it sorted and unique)."""

    __tablename__ = "task_recurrence_weekdays"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    weekday: Mapped[int] = mapped_column(Integer, primary_key=True)

    __table_args__ = (
        _child_of("task_recurrence_weekdays", "tasks", ("task_id",), ("id",)),
        CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_task_recurrence_weekdays_weekday"),
    )


class FixedBlock(_Record, _FixedBlockContent, Base):
    __tablename__ = "fixed_blocks"

    __table_args__ = (
        *_record_checks("fixed_blocks"),
        *_fixed_block_checks("fixed_blocks"),
        Index("ix_fixed_blocks_user_date", "user_id", "planned_date"),
    )


class Placement(_Record, _PlacementContent, Base):
    """A ScheduledTask."""

    __tablename__ = "placements"

    task_tag_rows: Mapped[list[PlacementTaskTag]] = relationship(
        order_by="PlacementTaskTag.position", cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (
        *_record_checks("placements"),
        ForeignKeyConstraint(["user_id", "task_id"], ["tasks.user_id", "tasks.id"], name="fk_placements_task"),
        *_placement_checks("placements"),
        Index("ix_placements_user_date", "user_id", "planned_date"),
        # The target of an execution's (user, task, placement) reference -- so the database checks that a
        # linked placement belongs to that task and user -- and the index of placements-by-task queries.
        Index("uq_placements_user_task_id", "user_id", "task_id", "id", unique=True),
        # Removal provenance is only ever set on a tombstone.
        CheckConstraint(
            "deleted_at IS NOT NULL OR (removal_reason IS NULL AND superseded_by_id IS NULL)",
            name="ck_placements_removal_tombstone",
        ),
        # Walking a chain of moves backwards (which tombstones did this placement supersede?).
        Index("ix_placements_user_superseded_by", "user_id", "superseded_by_id"),
    )


class PlacementTaskTag(Base):
    """A placement's snapshot of its task's tags, in order (see _PlacementContent.task_tags_recorded)."""

    __tablename__ = "placement_task_tags"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    placement_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    tag: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        _child_of("placement_task_tags", "placements", ("placement_id",), ("id",)),
        _position_check("placement_task_tags"),
    )


class Execution(_Record, _ExecutionContent, Base):
    """
    A TaskExecution. `id` is the wire id (docs/sync-contract.md section 3);
    `legacy_id` keeps a non-UUID local id exactly. task_id/scheduled_task_id
    are historical identity and never cascade: they may name records that
    were never persisted (historical_reference). linked_task_id /
    linked_placement_id are the same ids when they resolve to the user's own
    task and a placement of that task -- enforced by composite foreign keys
    and required for every non-historical execution. Tasks and placements
    are only ever tombstoned, so a linked row, and its history, stays.
    """

    __tablename__ = "executions"

    linked_task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    linked_placement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    __table_args__ = (
        *_record_checks("executions"),
        *_execution_checks("executions"),
        ForeignKeyConstraint(["user_id", "linked_task_id"], ["tasks.user_id", "tasks.id"], name="fk_executions_task"),
        ForeignKeyConstraint(
            ["user_id", "linked_task_id", "linked_placement_id"],
            ["placements.user_id", "placements.task_id", "placements.id"], name="fk_executions_placement",
        ),
        CheckConstraint("linked_task_id IS NULL OR linked_task_id = task_id", name="ck_executions_linked_task"),
        CheckConstraint(
            "linked_placement_id IS NULL OR (linked_placement_id = scheduled_task_id AND linked_task_id IS NOT NULL)",
            name="ck_executions_linked_placement",
        ),
        CheckConstraint(
            "historical_reference OR ((task_id IS NULL OR linked_task_id IS NOT NULL)"
            " AND (scheduled_task_id IS NULL OR linked_placement_id IS NOT NULL))",
            name="ck_executions_canonical_links",
        ),
        # One execution per placement (like the local partial unique index), and legacy ids stay unique.
        Index("uq_executions_user_placement", "user_id", "scheduled_task_id", unique=True),
        Index("uq_executions_user_legacy_id", "user_id", "legacy_id", unique=True),
        # PlanningService's execution facts of tasks: WHERE user_id = ? AND task_id IN (...).
        Index("ix_executions_user_task", "user_id", "task_id"),
    )


class WorkSession(Base):
    """One session of an execution aggregate; identified within it by position (chronological)."""

    __tablename__ = "work_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    execution_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "execution_id"], ["executions.user_id", "executions.id"], name="fk_work_sessions_execution"
        ),
        CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="ck_work_sessions_order"),
        CheckConstraint("position >= 0", name="ck_work_sessions_position"),
    )


class Preference(_Record, _PreferenceContent, Base):
    """A user-level (scope 'user') or per-date (scope 'date') PreferenceOverrides layer."""

    __tablename__ = "preferences"

    category_multiplier_rows: Mapped[list[PreferenceCategoryMultiplier]] = relationship(
        order_by="PreferenceCategoryMultiplier.category", cascade="all, delete-orphan", lazy="selectin")
    category_window_rows: Mapped[list[PreferenceCategoryWindow]] = relationship(
        order_by="PreferenceCategoryWindow.category", cascade="all, delete-orphan", lazy="selectin")
    tag_relation_rows: Mapped[list[PreferenceTagRelation]] = relationship(
        order_by="PreferenceTagRelation.tag", cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (
        *_record_checks("preferences"),
        *_preference_checks("preferences"),
        Index(
            "uq_preferences_live_scope", "user_id", "scope_key", unique=True,
            sqlite_where=text("deleted_at IS NULL"), postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class PreferenceCategoryMultiplier(Base):
    """One category_multipliers key. The row is the key's presence; multiplier NULL = an explicit clear."""

    __tablename__ = "preference_category_multipliers"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    preference_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    category: Mapped[str] = mapped_column(Text, primary_key=True)
    multiplier: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (_child_of("preference_category_multipliers", "preferences", ("preference_id",), ("id",)),)


class PreferenceCategoryWindow(Base):
    """One category_preferred_windows key. The row is the key's presence; both minutes NULL = an explicit clear."""

    __tablename__ = "preference_category_windows"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    preference_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    category: Mapped[str] = mapped_column(Text, primary_key=True)
    start_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        _child_of("preference_category_windows", "preferences", ("preference_id",), ("id",)),
        CheckConstraint("(start_minute IS NULL) = (end_minute IS NULL)", name="ck_preference_category_windows_window"),
    )


class PreferenceTagRelation(Base):
    """One reward.tag_relations key (only when the mapping is present); its related tags may be an empty list."""

    __tablename__ = "preference_tag_relations"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    preference_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tag: Mapped[str] = mapped_column(Text, primary_key=True)

    related_rows: Mapped[list[PreferenceRelatedTag]] = relationship(
        order_by="PreferenceRelatedTag.position", cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (_child_of("preference_tag_relations", "preferences", ("preference_id",), ("id",)),)


class PreferenceRelatedTag(Base):
    """One related tag of a tag relation, in order (exactly as given)."""

    __tablename__ = "preference_related_tags"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    preference_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tag: Mapped[str] = mapped_column(Text, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    related_tag: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        _child_of("preference_related_tags", "preference_tag_relations", ("preference_id", "tag"),
                  ("preference_id", "tag")),
        _position_check("preference_related_tags"),
    )


class ScheduleGeneration(_Record, _GenerationContent, Base):
    """Persisted schedule freshness/provenance of one date (app/planning/provenance.GenerationRecord)."""

    __tablename__ = "schedule_generations"

    __table_args__ = (
        *_record_checks("schedule_generations"),
        *_generation_checks("schedule_generations"),
        Index(
            "uq_schedule_generations_live_date", "user_id", "planned_date", unique=True,
            sqlite_where=text("deleted_at IS NULL"), postgresql_where=text("deleted_at IS NULL"),
        ),
    )


# -----------------------------------------------------------------------------
# Immutable record revisions: the historical snapshots the change log and the
# recorded sync outcomes return (the record exactly as the API returned it
# then). One header row per snapshot plus one typed row (joined-table
# inheritance, keyed like the header) and ordered child rows. Revisions are
# history: their ids are not foreign keys to the live tables.
# -----------------------------------------------------------------------------


class RecordRevision(Base):
    __tablename__ = "record_revisions"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), primary_key=True)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    #: The record's own metadata, as it was in this snapshot.
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(_in("entity_type", ENTITY_TYPES), name="ck_record_revisions_entity_type"),
        CheckConstraint("version > 0", name="ck_record_revisions_version"),
    )
    __mapper_args__ = {"polymorphic_on": "entity_type"}


def _revision_of(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["user_id", "id"], ["record_revisions.user_id", "record_revisions.id"], name=f"fk_{table}_revision",
        ondelete="CASCADE",
    )


class _RevisionKey:
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)


class ProjectRevision(_RevisionKey, _ProjectContent, RecordRevision):
    __tablename__ = "project_revisions"
    __table_args__ = (_revision_of("project_revisions"), *_project_checks("project_revisions"))
    __mapper_args__ = {"polymorphic_identity": "project", "polymorphic_load": "selectin"}


class TaskTypeRevision(_RevisionKey, _TaskTypeContent, RecordRevision):
    __tablename__ = "task_type_revisions"
    __table_args__ = (_revision_of("task_type_revisions"), *_task_type_checks("task_type_revisions"))
    __mapper_args__ = {"polymorphic_identity": "task_type", "polymorphic_load": "selectin"}


class TaskRevision(_RevisionKey, _TaskContent, RecordRevision):
    __tablename__ = "task_revisions"

    tag_rows: Mapped[list[TaskRevisionTag]] = relationship(
        order_by="TaskRevisionTag.position", cascade="all, delete-orphan", lazy="selectin")
    preferred_date_rows: Mapped[list[TaskRevisionPreferredDate]] = relationship(
        order_by="TaskRevisionPreferredDate.position", cascade="all, delete-orphan", lazy="selectin")
    dependency_rows: Mapped[list[TaskRevisionDependency]] = relationship(
        order_by="TaskRevisionDependency.position", cascade="all, delete-orphan", lazy="selectin")
    recurrence_weekday_rows: Mapped[list[TaskRevisionRecurrenceWeekday]] = relationship(
        order_by="TaskRevisionRecurrenceWeekday.weekday", cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (_revision_of("task_revisions"), *_task_checks("task_revisions"))
    __mapper_args__ = {"polymorphic_identity": "task", "polymorphic_load": "selectin"}


class TaskRevisionTag(Base):
    __tablename__ = "task_revision_tags"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    tag: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        _child_of("task_revision_tags", "task_revisions", ("revision_id",), ("id",)),
        _position_check("task_revision_tags"),
    )


class TaskRevisionPreferredDate(Base):
    __tablename__ = "task_revision_preferred_dates"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    preferred_date: Mapped[date] = mapped_column(Date, nullable=False)

    __table_args__ = (
        _child_of("task_revision_preferred_dates", "task_revisions", ("revision_id",), ("id",)),
        _position_check("task_revision_preferred_dates"),
    )


class TaskRevisionDependency(Base):
    """A dependency id as it was (history: not a foreign key)."""

    __tablename__ = "task_revision_dependencies"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    depends_on_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    __table_args__ = (
        _child_of("task_revision_dependencies", "task_revisions", ("revision_id",), ("id",)),
        _position_check("task_revision_dependencies"),
    )


class TaskRevisionRecurrenceWeekday(Base):
    __tablename__ = "task_revision_recurrence_weekdays"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    weekday: Mapped[int] = mapped_column(Integer, primary_key=True)

    __table_args__ = (
        _child_of("task_revision_recurrence_weekdays", "task_revisions", ("revision_id",), ("id",)),
        CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_task_revision_recurrence_weekdays_weekday"),
    )


class FixedBlockRevision(_RevisionKey, _FixedBlockContent, RecordRevision):
    __tablename__ = "fixed_block_revisions"
    __table_args__ = (_revision_of("fixed_block_revisions"), *_fixed_block_checks("fixed_block_revisions"))
    __mapper_args__ = {"polymorphic_identity": "fixed_block", "polymorphic_load": "selectin"}


class PlacementRevision(_RevisionKey, _PlacementContent, RecordRevision):
    __tablename__ = "placement_revisions"

    task_tag_rows: Mapped[list[PlacementRevisionTaskTag]] = relationship(
        order_by="PlacementRevisionTaskTag.position", cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (_revision_of("placement_revisions"), *_placement_checks("placement_revisions"))
    __mapper_args__ = {"polymorphic_identity": "placement", "polymorphic_load": "selectin"}


class PlacementRevisionTaskTag(Base):
    __tablename__ = "placement_revision_task_tags"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    tag: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        _child_of("placement_revision_task_tags", "placement_revisions", ("revision_id",), ("id",)),
        _position_check("placement_revision_task_tags"),
    )


class PreferenceRevision(_RevisionKey, _PreferenceContent, RecordRevision):
    __tablename__ = "preference_revisions"

    category_multiplier_rows: Mapped[list[PreferenceRevisionCategoryMultiplier]] = relationship(
        order_by="PreferenceRevisionCategoryMultiplier.category", cascade="all, delete-orphan", lazy="selectin")
    category_window_rows: Mapped[list[PreferenceRevisionCategoryWindow]] = relationship(
        order_by="PreferenceRevisionCategoryWindow.category", cascade="all, delete-orphan", lazy="selectin")
    tag_relation_rows: Mapped[list[PreferenceRevisionTagRelation]] = relationship(
        order_by="PreferenceRevisionTagRelation.tag", cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (_revision_of("preference_revisions"), *_preference_checks("preference_revisions"))
    __mapper_args__ = {"polymorphic_identity": "preference", "polymorphic_load": "selectin"}


class PreferenceRevisionCategoryMultiplier(Base):
    __tablename__ = "preference_revision_category_multipliers"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    category: Mapped[str] = mapped_column(Text, primary_key=True)
    multiplier: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        _child_of("preference_revision_category_multipliers", "preference_revisions", ("revision_id",), ("id",)),
    )


class PreferenceRevisionCategoryWindow(Base):
    __tablename__ = "preference_revision_category_windows"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    category: Mapped[str] = mapped_column(Text, primary_key=True)
    start_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        _child_of("preference_revision_category_windows", "preference_revisions", ("revision_id",), ("id",)),
        CheckConstraint(
            "(start_minute IS NULL) = (end_minute IS NULL)", name="ck_preference_revision_category_windows_window"
        ),
    )


class PreferenceRevisionTagRelation(Base):
    __tablename__ = "preference_revision_tag_relations"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tag: Mapped[str] = mapped_column(Text, primary_key=True)

    related_rows: Mapped[list[PreferenceRevisionRelatedTag]] = relationship(
        order_by="PreferenceRevisionRelatedTag.position", cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (
        _child_of("preference_revision_tag_relations", "preference_revisions", ("revision_id",), ("id",)),
    )


class PreferenceRevisionRelatedTag(Base):
    __tablename__ = "preference_revision_related_tags"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tag: Mapped[str] = mapped_column(Text, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    related_tag: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        _child_of("preference_revision_related_tags", "preference_revision_tag_relations", ("revision_id", "tag"),
                  ("revision_id", "tag")),
        _position_check("preference_revision_related_tags"),
    )


class ScheduleGenerationRevision(_RevisionKey, _GenerationContent, RecordRevision):
    __tablename__ = "schedule_generation_revisions"
    __table_args__ = (_revision_of("schedule_generation_revisions"), *_generation_checks("schedule_generation_revisions"))
    __mapper_args__ = {"polymorphic_identity": "schedule_generation", "polymorphic_load": "selectin"}


class ExecutionRevision(_RevisionKey, _ExecutionContent, RecordRevision):
    __tablename__ = "execution_revisions"

    session_rows: Mapped[list[ExecutionRevisionSession]] = relationship(
        order_by="ExecutionRevisionSession.position", cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (_revision_of("execution_revisions"), *_execution_checks("execution_revisions"))
    __mapper_args__ = {"polymorphic_identity": "execution", "polymorphic_load": "selectin"}


class ExecutionRevisionSession(Base):
    """A work session of the execution as it was in the snapshot, in order."""

    __tablename__ = "execution_revision_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        _child_of("execution_revision_sessions", "execution_revisions", ("revision_id",), ("id",)),
        _position_check("execution_revision_sessions"),
        CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="ck_execution_revision_sessions_order"),
    )


# -----------------------------------------------------------------------------
# Change log and sync outcomes
# -----------------------------------------------------------------------------


class ChangeLogEntry(Base):
    """One accepted mutation of one record, in the user's commit order (seq is gap-free per user)."""

    __tablename__ = "change_log"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    operation: Mapped[str] = mapped_column(String(10), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    #: The complete record after the mutation (a tombstone for a delete), as the API returned it.
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    revision: Mapped[RecordRevision] = relationship(lazy="selectin")

    __table_args__ = (
        CheckConstraint("seq > 0", name="ck_change_log_seq"),
        CheckConstraint("operation IN ('upsert', 'delete')", name="ck_change_log_operation"),
        ForeignKeyConstraint(
            ["user_id", "revision_id"], ["record_revisions.user_id", "record_revisions.id"],
            name="fk_change_log_revision",
        ),
        Index("ix_change_log_entity", "user_id", "entity_type", "entity_id"),
    )


def _outcome_revision(column: str) -> dict:
    """A many-to-one from a sync outcome to one of its snapshots; user_id is the outcome's own (never copied)."""
    return {
        "primaryjoin": f"and_(SyncOperation.user_id == RecordRevision.user_id, SyncOperation.{column} == RecordRevision.id)",
        "foreign_keys": f"[SyncOperation.{column}]",
        "lazy": "selectin",
    }


class SyncOperation(Base):
    """
    The recorded outcome of one pushed sync operation (backend/sync.py), keyed
    by its client-chosen op_id. A retry of the same op_id returns this stored
    result instead of applying the operation again; reusing an op_id for a
    different operation is refused (request_hash). Applied operations are
    recorded in the same transaction as their mutation; rejected ones after
    it rolled back.

    The outcome is typed: an applied operation references the snapshot of
    its resulting record; a conflict/rejection stores its error code,
    message and details (the version pair, the reason, the failing op of a
    group, validation problems as child rows) and references the snapshot
    of the `current`/`conflicting` record it reported. Snapshots are
    immutable, so a retry answers the same even after later edits.
    """

    __tablename__ = "sync_operations"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), primary_key=True)
    op_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    record_revision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Present together (a version conflict/tombstone): supplied_version may still be NULL.
    error_supplied_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_current_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_current_revision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    error_conflicting_revision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    error_reason: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_failed_op_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    #: Whether the error carried a `problems` list (which may be empty); the problems are child rows.
    error_problems_present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())

    record_revision: Mapped[RecordRevision | None] = relationship(**_outcome_revision("record_revision_id"))
    error_current_revision: Mapped[RecordRevision | None] = relationship(**_outcome_revision("error_current_revision_id"))
    error_conflicting_revision: Mapped[RecordRevision | None] = relationship(
        **_outcome_revision("error_conflicting_revision_id"))
    problem_rows: Mapped[list[SyncOperationProblem]] = relationship(
        order_by="SyncOperationProblem.position", cascade="all, delete-orphan", lazy="selectin")
    #: An applied operation that changed several records (a placement reschedule): the others, in order.
    related_rows: Mapped[list[SyncOperationRelatedRecord]] = relationship(
        order_by="SyncOperationRelatedRecord.position", cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (
        CheckConstraint(_in("status", SYNC_STATUSES), name="ck_sync_operations_status"),
        CheckConstraint(
            "(status = 'applied') = (record_revision_id IS NOT NULL)"
            " AND (status = 'applied') = (error_code IS NULL)"
            " AND (error_code IS NULL) = (error_message IS NULL)",
            name="ck_sync_operations_outcome",
        ),
        CheckConstraint(
            "error_supplied_version IS NULL OR error_current_version IS NOT NULL", name="ck_sync_operations_versions"
        ),
        ForeignKeyConstraint(
            ["user_id", "record_revision_id"], ["record_revisions.user_id", "record_revisions.id"],
            name="fk_sync_operations_record",
        ),
        ForeignKeyConstraint(
            ["user_id", "error_current_revision_id"], ["record_revisions.user_id", "record_revisions.id"],
            name="fk_sync_operations_current",
        ),
        ForeignKeyConstraint(
            ["user_id", "error_conflicting_revision_id"], ["record_revisions.user_id", "record_revisions.id"],
            name="fk_sync_operations_conflicting",
        ),
    )


class SyncOperationRelatedRecord(Base):
    """
    One further record an applied sync operation changed, besides its own
    (e.g. a reschedule's replacement placement and cancelled execution), as
    the immutable snapshot the change log also references -- so a retry of
    the op_id answers every record exactly as the first response did.
    """

    __tablename__ = "sync_operation_related_records"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    op_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    revision: Mapped[RecordRevision] = relationship(
        primaryjoin="and_(SyncOperationRelatedRecord.user_id == RecordRevision.user_id, "
                    "SyncOperationRelatedRecord.revision_id == RecordRevision.id)",
        foreign_keys="[SyncOperationRelatedRecord.revision_id]", lazy="selectin",
    )

    __table_args__ = (
        _child_of("sync_operation_related_records", "sync_operations", ("op_id",), ("op_id",)),
        ForeignKeyConstraint(
            ["user_id", "revision_id"], ["record_revisions.user_id", "record_revisions.id"],
            name="fk_sync_operation_related_records_revision",
        ),
        _position_check("sync_operation_related_records"),
    )


class SyncOperationProblem(Base):
    """One validation problem of a recorded sync error, in order."""

    __tablename__ = "sync_operation_problems"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    op_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    location_rows: Mapped[list[SyncOperationProblemLocation]] = relationship(
        order_by="SyncOperationProblemLocation.position", cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (
        _child_of("sync_operation_problems", "sync_operations", ("op_id",), ("op_id",)),
        _position_check("sync_operation_problems"),
    )


class SyncOperationProblemLocation(Base):
    """One part of a problem's location path, in order."""

    __tablename__ = "sync_operation_problem_locations"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    op_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    problem_position: Mapped[int] = mapped_column(Integer, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    part: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        _child_of("sync_operation_problem_locations", "sync_operation_problems", ("op_id", "problem_position"),
                  ("op_id", "position")),
        _position_check("sync_operation_problem_locations"),
    )


class BrowserSession(Base):
    """
    A signed-in browser (backend/browser_sessions.py). The browser holds a
    random session token only in an HttpOnly cookie; the server stores its
    SHA-256 digest, never the token. Logout sets revoked_at, so the cookie
    stops working at once even before it expires.
    """

    __tablename__ = "browser_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: The user's credential epoch when the session started; a session from an older epoch is refused.
    credential_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))

    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="ck_browser_sessions_expiry"),
        Index("ix_browser_sessions_user", "user_id"),
    )


class PasswordRecoveryToken(Base):
    """
    One issued password-recovery credential (backend/recovery.py). Only the
    SHA-256 digest of the random token is stored; it is bound to one user,
    expires, and is consumed or revoked at most once (conditional updates).
    """

    __tablename__ = "password_recovery_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="ck_password_recovery_tokens_expiry"),
        CheckConstraint("consumed_at IS NULL OR revoked_at IS NULL", name="ck_password_recovery_tokens_state"),
        Index("ix_password_recovery_tokens_user", "user_id"),
    )


class RateLimitBucket(Base):
    """
    A fixed-window request counter shared by every worker and replica
    (backend/rate_limit.py). `key` is a digest of (scope, subject, window);
    rows past `expires_at` are deleted as new windows start.
    """

    __tablename__ = "rate_limit_buckets"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(40), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        CheckConstraint("count > 0", name="ck_rate_limit_buckets_count"),
        Index("ix_rate_limit_buckets_expiry", "expires_at"),
    )
