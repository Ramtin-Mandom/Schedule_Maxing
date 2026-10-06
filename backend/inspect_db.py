"""Read-only, local administrator inspection. Never mounted as an API route.

Uses DATABASE_URL (or one explicitly named env file); does not require JWT
settings. Samples contain only allowlisted identifiers and lifecycle metadata,
never names, emails, task content, password hashes or credential digests.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from sqlalchemy import MetaData, Table, and_, func, inspect, select, text

from backend.database import create_backend_engine
from backend.migrate import current_revision, head_revision
from backend.settings import normalize_database_url

SAMPLE_COLUMNS = frozenset({"id", "user_id", "version", "created_at", "updated_at", "deleted_at",
                            "status", "planned_date"})
PURPOSES = {
    "users": "Account identity and password hashes (values never sampled).",
    "projects": "Project plans, defaults and completion state.",
    "tasks": "Canonical tasks, recurrence rules, occurrence identities and estimates.",
    "task_types": "Reusable task types for historical productivity comparisons.",
    "placements": "ScheduledTask intervals and immutable planning snapshots.",
    "executions": "TaskExecution lifecycle, outcomes, feedback and historical snapshots.",
    "work_sessions": "Execution start/pause/resume intervals.",
    "fixed_blocks": "Hard scheduling constraints.",
    "preferences": "User/date preference overrides.",
    "schedule_generations": "Schedule provenance and freshness.",
    "record_revisions": "Immutable historical versions referenced by sync and change feed.",
    "change_log": "User-scoped committed change sequence.",
    "sync_operations": "Idempotent sync operation results.",
    "browser_sessions": "Hashed browser credentials, expiration and revocation.",
    "native_sessions": "Native-client refresh families and revocation state.",
    "refresh_credentials": "Hashed single-use refresh credentials and consumption state.",
    "password_recovery_tokens": "Hashed single-use password reset credentials.",
    "rate_limit_buckets": "Shared hashed-subject abuse counters.",
    "alembic_version": "Applied server schema revision.",
}


def describe_table(name: str) -> str:
    if name in PURPOSES:
        return PURPOSES[name]
    if "revision" in name:
        return "Normalized immutable historical snapshot or its child rows."
    if name.startswith("sync_operation"):
        return "Normalized sync outcomes, related records or validation problems."
    return "Normalized child records; foreign keys identify their owning aggregate."


def inspect_database(engine, *, samples: int = 0) -> dict:
    if not 0 <= samples <= 10:
        raise ValueError("samples must be between 0 and 10")
    with engine.connect() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(text("SET LOCAL statement_timeout = 30000"))
        connection.execute(text("SELECT 1"))
        inspector = inspect(connection)
        names = inspector.get_table_names()
        metadata = MetaData()
        tables = {name: Table(name, metadata, autoload_with=connection) for name in names}
        current, head = current_revision(connection), head_revision()
        output = {"connected": True, "dialect": connection.dialect.name,
                  "revision": current, "head_revision": head, "migrations_current": current == head, "tables": {}}
        for name, table in tables.items():
            foreign_keys = []
            for constraint in table.foreign_key_constraints:
                elements = list(constraint.elements)
                parent = elements[0].column.table.alias()
                linked = and_(*(element.parent == parent.c[element.column.name] for element in elements))
                present = and_(*(element.parent.is_not(None) for element in elements))
                missing = ~select(1).select_from(parent).where(linked).exists()
                orphans = connection.scalar(select(func.count()).select_from(table).where(present, missing))
                foreign_keys.append({"columns": [element.parent.name for element in elements],
                                     "target_table": elements[0].column.table.name,
                                     "target_columns": [element.column.name for element in elements],
                                     "orphan_count": orphans})
            safe_columns = [column for column in table.columns if column.name in SAMPLE_COLUMNS]
            rows = []
            if samples and safe_columns:
                query = select(*safe_columns).limit(samples)
                if len(table.primary_key.columns):
                    query = query.order_by(*table.primary_key.columns)
                rows = [dict(row) for row in connection.execute(query).mappings()]
            output["tables"][name] = {
                "purpose": describe_table(name),
                "count": connection.scalar(select(func.count()).select_from(table)),
                "columns": [{"name": column.name, "type": str(column.type), "nullable": column.nullable,
                             "primary_key": column.primary_key} for column in table.columns],
                "foreign_keys": foreign_keys, "samples": rows,
            }
        return output


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", help="Explicit administrator env file; process environment wins.")
    parser.add_argument("--samples", type=int, choices=range(0, 11), default=0,
                        help="Allowlisted metadata sample rows per table (0-10).")
    args = parser.parse_args(argv)
    engine = None
    try:
        values = dict(os.environ)
        if args.env_file:
            from dotenv import dotenv_values
            if not Path(args.env_file).is_file():
                raise ValueError("The explicit env file does not exist")
            values = {**dotenv_values(args.env_file, interpolate=False), **values}
        url = normalize_database_url(values.get("DATABASE_URL") or "")
        if not url:
            raise ValueError("DATABASE_URL is required")
        options = {"connect_args": {"connect_timeout": 10}, "pool_timeout": 10} if url.startswith("postgresql") else {}
        engine = create_backend_engine(url, hide_parameters=True, **options)
        report = inspect_database(engine, samples=args.samples)
        print(json.dumps(report, default=str, indent=2))
        broken = any(fk["orphan_count"] for table in report["tables"].values() for fk in table["foreign_keys"])
        return 0 if report["migrations_current"] and not broken else 1
    except Exception as error:
        # A driver/configuration error can include the URL or SQL. Never echo it.
        print(json.dumps({"connected": False, "error": "Database inspection failed", "kind": type(error).__name__}))
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
