"""The local administrator report is read-only and samples no private content."""

import json

from sqlalchemy import text

from backend.inspect_db import inspect_database, main
from tests.backend.conftest import create, task_payload


def test_inspection_reports_schema_counts_and_safe_metadata(engine, client, alice):
    task = create(client, alice, "tasks", task_payload(name="Private sensitive name"))
    report = inspect_database(engine, samples=1)
    assert report["connected"] and report["migrations_current"]
    tasks = report["tables"]["tasks"]
    assert tasks["count"] == 1 and str(tasks["samples"][0]["id"]).replace("-", "") == task["id"].replace("-", "")
    assert all(fk["orphan_count"] == 0 for table in report["tables"].values() for fk in table["foreign_keys"])
    rendered = json.dumps(report, default=str)
    assert "Private sensitive name" not in rendered
    assert "alice@example.com" not in rendered
    assert "$argon2" not in rendered
    assert report["tables"]["refresh_credentials"]["samples"]
    assert "token_hash" not in report["tables"]["refresh_credentials"]["samples"][0]


def test_inspection_does_not_create_schema(engine):
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM alembic_version"))
    assert not inspect_database(engine)["migrations_current"]
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM alembic_version")) == 0


def test_cli_failure_never_echoes_credentials(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "invalid://user:private-password@host/db")
    assert main([]) == 2
    output = capsys.readouterr().out
    assert "private-password" not in output and "user" not in output


def test_missing_explicit_file_does_not_fall_back_to_environment(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    assert main(["--env-file", str(tmp_path / "missing.env")]) == 2
    assert json.loads(capsys.readouterr().out)["connected"] is False
