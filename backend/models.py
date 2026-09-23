"""
backend/models.py

The server's database schema (SQLAlchemy 2 ORM). The versioned Alembic
migrations in backend/migrations/ create exactly this schema; the
migration test compares them so they cannot drift apart.

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
    - change_log is the per-user, gap-free, commit-ordered feed of accepted
      mutations (see backend/mutations.py for the ordering strategy).
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
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

from backend.database import JSONDocument, UTCDateTime

EXECUTION_STATUSES = ("scheduled", "in_progress", "paused", "completed", "skipped", "cancelled")
OPTIMIZER_MODES = ("precise_greedy", "adhd_friendly")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint("length(email) > 0", name="ck_users_email_nonempty"),
        CheckConstraint("change_seq >= 0", name="ck_users_change_seq"),
        CheckConstraint("version > 0", name="ck_users_version"),
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


class Project(_Record, Base):
    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (*_record_checks("projects"), CheckConstraint("length(name) > 0", name="ck_projects_name"))


class Task(_Record, Base):
    __tablename__ = "tasks"

    project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    tags: Mapped[list] = mapped_column(JSONDocument, nullable=False)
    estimated_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    required_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    preferred_dates: Mapped[list] = mapped_column(JSONDocument, nullable=False)
    preferred_window_start_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    preferred_window_end_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: ISO 8601 with its original UTC offset (exact round trip), plus a UTC twin for queries.
    deadline: Mapped[str | None] = mapped_column(String(40), nullable=True)
    deadline_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    recurrence: Mapped[dict | None] = mapped_column(JSONDocument, nullable=True)

    __table_args__ = (
        *_record_checks("tasks"),
        ForeignKeyConstraint(["user_id", "project_id"], ["projects.user_id", "projects.id"], name="fk_tasks_project"),
        CheckConstraint("length(name) > 0", name="ck_tasks_name"),
        CheckConstraint("length(category) > 0", name="ck_tasks_category"),
        CheckConstraint("estimated_duration_minutes > 0", name="ck_tasks_duration"),
        CheckConstraint("priority BETWEEN 1 AND 10", name="ck_tasks_priority"),
        CheckConstraint(
            "(preferred_window_start_minute IS NULL) = (preferred_window_end_minute IS NULL)", name="ck_tasks_window"
        ),
        CheckConstraint("(deadline IS NULL) = (deadline_utc IS NULL)", name="ck_tasks_deadline"),
        Index("ix_tasks_user_project", "user_id", "project_id"),
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


class FixedBlock(_Record, Base):
    __tablename__ = "fixed_blocks"

    label: Mapped[str] = mapped_column(String(500), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    planned_date: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    planned_start: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    planned_end: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        *_record_checks("fixed_blocks"),
        CheckConstraint("length(label) > 0", name="ck_fixed_blocks_label"),
        CheckConstraint("length(category) > 0", name="ck_fixed_blocks_category"),
        CheckConstraint("planned_end > planned_start", name="ck_fixed_blocks_order"),
        Index("ix_fixed_blocks_user_date", "user_id", "planned_date"),
    )


class Placement(_Record, Base):
    """A ScheduledTask."""

    __tablename__ = "placements"

    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    planned_date: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    planned_start: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    planned_end: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    optimization_metadata: Mapped[dict] = mapped_column(JSONDocument, nullable=False)

    __table_args__ = (
        *_record_checks("placements"),
        ForeignKeyConstraint(["user_id", "task_id"], ["tasks.user_id", "tasks.id"], name="fk_placements_task"),
        CheckConstraint("planned_end > planned_start", name="ck_placements_order"),
        Index("ix_placements_user_date", "user_id", "planned_date"),
        Index("ix_placements_user_task", "user_id", "task_id"),
    )


class Execution(_Record, Base):
    """
    A TaskExecution. `id` is the wire id (docs/sync-contract.md section 3);
    `legacy_id` keeps a non-UUID local id exactly. task_id/scheduled_task_id
    are historical identity, not foreign keys: they may name records that
    were never persisted (historical_reference) and never cascade.
    """

    __tablename__ = "executions"

    legacy_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
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

    __table_args__ = (
        *_record_checks("executions"),
        CheckConstraint(_in("status", EXECUTION_STATUSES), name="ck_executions_status"),
        CheckConstraint("priority BETWEEN 1 AND 10", name="ck_executions_priority"),
        CheckConstraint("focus_rating IS NULL OR focus_rating BETWEEN 1 AND 5", name="ck_executions_focus"),
        CheckConstraint("energy_rating IS NULL OR energy_rating BETWEEN 1 AND 5", name="ck_executions_energy"),
        CheckConstraint("interruption_count IS NULL OR interruption_count >= 0", name="ck_executions_interruptions"),
        CheckConstraint("scheduled_task_id IS NULL OR task_id IS NOT NULL", name="ck_executions_link"),
        # One execution per placement (like the local partial unique index), and legacy ids stay unique.
        Index("uq_executions_user_placement", "user_id", "scheduled_task_id", unique=True),
        Index("uq_executions_user_legacy_id", "user_id", "legacy_id", unique=True),
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


class Preference(_Record, Base):
    """A user-level (scope 'user') or per-date (scope 'date') PreferenceOverrides layer."""

    __tablename__ = "preferences"

    scope: Mapped[str] = mapped_column(String(10), nullable=False)
    scope_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: "user" or the ISO date: one live layer per (user, scope_key).
    scope_key: Mapped[str] = mapped_column(String(16), nullable=False)
    optimizer_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    overrides: Mapped[dict] = mapped_column(JSONDocument, nullable=False)

    __table_args__ = (
        *_record_checks("preferences"),
        CheckConstraint("scope IN ('user', 'date')", name="ck_preferences_scope"),
        CheckConstraint("(scope = 'date') = (scope_date IS NOT NULL)", name="ck_preferences_scope_date"),
        CheckConstraint(f"optimizer_mode IS NULL OR {_in('optimizer_mode', OPTIMIZER_MODES)}", name="ck_preferences_mode"),
        Index(
            "uq_preferences_live_scope", "user_id", "scope_key", unique=True,
            sqlite_where=text("deleted_at IS NULL"), postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class ScheduleGeneration(_Record, Base):
    """Persisted schedule freshness/provenance of one date (app/planning/provenance.GenerationRecord)."""

    __tablename__ = "schedule_generations"

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

    __table_args__ = (
        *_record_checks("schedule_generations"),
        CheckConstraint(_in("engine_mode", OPTIMIZER_MODES), name="ck_schedule_generations_mode"),
        CheckConstraint("range_start <= planned_date AND planned_date <= range_end", name="ck_schedule_generations_range"),
        CheckConstraint("placement_count >= 0 AND unscheduled_count >= 0", name="ck_schedule_generations_counts"),
        Index(
            "uq_schedule_generations_live_date", "user_id", "planned_date", unique=True,
            sqlite_where=text("deleted_at IS NULL"), postgresql_where=text("deleted_at IS NULL"),
        ),
    )


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
    #: The complete record after the mutation (a tombstone for a delete), as the API returns it.
    payload: Mapped[dict] = mapped_column(JSONDocument, nullable=False)

    __table_args__ = (
        CheckConstraint("seq > 0", name="ck_change_log_seq"),
        CheckConstraint("operation IN ('upsert', 'delete')", name="ck_change_log_operation"),
        Index("ix_change_log_entity", "user_id", "entity_type", "entity_id"),
    )


class SyncOperation(Base):
    """
    The recorded outcome of one pushed sync operation (backend/sync.py), keyed
    by its client-chosen op_id. A retry of the same op_id returns this stored
    result instead of applying the operation again; reusing an op_id for a
    different operation is refused (request_hash). Applied operations are
    recorded in the same transaction as their mutation; rejected ones after
    it rolled back.
    """

    __tablename__ = "sync_operations"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), primary_key=True)
    op_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    result: Mapped[dict] = mapped_column(JSONDocument, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        CheckConstraint("status IN ('applied', 'conflict', 'rejected')", name="ck_sync_operations_status"),
    )
