"""The opt-in verification command (app/persistence/verify_render.py) on the
disposable database of tests/direct/conftest.py -- never on a real one: the
backend factory is injected and the password comes from a test function, not
a prompt. Import without side effects, check-only, the opt-in guard,
idempotent reruns, a wrong password for an existing account, user-edited
seeds, concurrent reruns, two accounts, partial failure and recovery,
date/time-zone conversion, the dependency fixture, and secret-free output."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, inspect, select

from app.persistence import verify_render
from app.persistence.direct import DirectBackend
from backend import models
from backend.database import session_factory
from backend.migrate import upgrade

ROOT = Path(__file__).resolve().parents[2]
SECRET = "Tr1cky-Pa$$word %s"
EMAIL, NAME = "Ramtin.Test@Example.com", "Ramtin"
ARGS = ["--write-sample", "--email", EMAIL, "--display-name", NAME, "--anchor-date", "2026-09-28",
        "--timezone", "America/Vancouver"]
ENV = {"DATABASE_URL": "postgresql://verifier@127.0.0.1:5432/injected_test"}  # never contacted: the backend is injected


def run(engine, args, *, password: str = SECRET, repeat: str | None = None) -> tuple[int, str]:
    answers = iter([password, repeat if repeat is not None else password])
    lines: list[str] = []
    code = verify_render.main(args, backend_factory=lambda _settings: DirectBackend(engine), environ=ENV,
                              password_source=lambda _prompt: next(answers), out=lines.append)
    return code, "\n".join(lines)


def count(engine, model, **where) -> int:
    with session_factory(engine)() as session:
        query = select(func.count()).select_from(model)
        for name, value in where.items():
            query = query.where(getattr(model, name) == value)
        return session.scalar(query)


def seeded_ids(engine) -> set:
    with session_factory(engine)() as session:
        return set(session.scalars(select(models.Task.id))) | set(session.scalars(select(models.FixedBlock.id)))


def test_importing_the_module_reads_nothing_and_connects_nowhere(tmp_path) -> None:
    (tmp_path / ".env").write_text("DATABASE_URL=postgresql://u:p@db.example.com/x\n", encoding="utf-8")
    code = ("import os, sys, app.persistence.verify_render; "
            "print('DATABASE_URL' in os.environ, 'sqlalchemy' in sys.modules, 'dotenv' in sys.modules)")
    env = {key: value for key, value in os.environ.items() if key not in ("DATABASE_URL", "TEST_DATABASE_URL")}
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["False", "False", "False"]


def test_a_mode_must_be_chosen_explicitly(engine) -> None:
    with pytest.raises(SystemExit):
        verify_render.main([], environ=ENV)
    with pytest.raises(SystemExit):  # no default date, zone, email or name
        verify_render.main(["--write-sample"], environ=ENV)
    assert count(engine, models.User) == 0


def test_check_only_reports_the_revision_and_writes_nothing(blank_engine) -> None:
    code, out = run(blank_engine, ["--check-only"])
    assert code == 1 and "revision none" in out and "backend.migrate --env-file .env upgrade" in out
    assert inspect(blank_engine).get_table_names() == []  # the check created nothing (not even alembic_version)

    upgrade(blank_engine)
    code, out = run(blank_engine, ["--check-only"])
    assert code == 0 and "SUCCESS" in out and "schema current" in out and "local server (TLS not required)" in out
    assert count(blank_engine, models.User) == 0 and count(blank_engine, models.ChangeLogEntry) == 0


def test_the_sample_is_seeded_once_and_a_rerun_changes_nothing(engine, caplog) -> None:
    caplog.set_level("DEBUG")
    code, first = run(engine, ARGS)
    assert code == 0, first
    assert "Ramtin <ramtin.test@example.com> (created, password verified)" in first
    assert "3 tasks, 4 fixed blocks, 3 placements, 1 execution (scheduled)" in first
    assert "none in the main sample (not applicable)" in first and "generation generated" in first
    ids, changes = seeded_ids(engine), count(engine, models.ChangeLogEntry)

    code, second = run(engine, ARGS)
    assert code == 0, second
    assert "(existing, password verified)" in second and "seed already seeded" in second
    assert "generation already_current" in second and "0 new change-log entries" in second
    assert seeded_ids(engine) == ids and count(engine, models.ChangeLogEntry) == changes
    assert count(engine, models.User) == 1 and count(engine, models.Placement) == 3
    assert count(engine, models.Execution) == 1 and count(engine, models.WorkSession) == 0  # no invented work

    for text in (first, second, caplog.text):  # the password never appears in output or logs
        assert SECRET not in text and "$argon2" not in text and "injected_test" not in text


def test_times_follow_the_anchor_date_and_time_zone(engine) -> None:
    assert run(engine, ARGS)[0] == 0
    with session_factory(engine)() as session:
        blocks = {row.label: row for row in session.scalars(select(models.FixedBlock))}
        tasks = {row.name: row for row in session.scalars(select(models.Task))}
        placements = list(session.scalars(select(models.Placement)))
    # 2026-09-28 in Vancouver is UTC-7: the sample's local midnight Sleep block starts at 07:00 UTC.
    assert blocks["Sleep"].planned_start == datetime(2026, 9, 28, 7, 0, tzinfo=timezone.utc)
    assert blocks["Dinner"].planned_end == datetime(2026, 9, 29, 2, 0, tzinfo=timezone.utc)
    study = tasks["Study Session"]
    assert [tag.tag for tag in study.tag_rows] == ["focus"] and study.category == "study"
    assert (study.estimated_duration_minutes, study.priority) == (90, 7)
    assert (study.preferred_window_start_minute, study.preferred_window_end_minute) == (540, 720)
    assert [row.preferred_date.isoformat() for row in study.preferred_date_rows] == ["2026-09-28"]
    assert all(placement.planned_date.isoformat() == "2026-09-28" for placement in placements)


def test_a_wrong_password_for_an_existing_account_stops_everything(engine) -> None:
    assert run(engine, ARGS)[0] == 0
    changes = count(engine, models.ChangeLogEntry)
    code, out = run(engine, ARGS, password="not the right password")
    assert code == 2 and "does not match the existing account" in out and "never reset" in out
    assert "account FAILED" in out and "seed not run" in out
    assert count(engine, models.ChangeLogEntry) == changes
    assert run(engine, ARGS)[0] == 0  # the original password still works


def test_a_new_account_needs_the_password_twice(engine) -> None:
    code, out = run(engine, ARGS, repeat="a different one")
    assert code == 2 and "differ" in out and count(engine, models.User) == 0


def test_user_edits_are_never_overwritten(backend, engine) -> None:
    assert run(engine, ARGS)[0] == 0
    account = backend.sign_in(email=EMAIL, password=SECRET)
    planning = account.planning_service()
    study = next(task for task in planning.list_tasks() if task.name == "Study Session")
    planning.update_task(study.model_copy(update={"priority": 2}), expected_version=study.version)
    changes = count(engine, models.ChangeLogEntry)

    code, out = run(engine, ARGS)
    assert code == 1 and "was edited" in out and "nothing was overwritten" in out
    assert planning.get_task(study.id).priority == 2 and count(engine, models.ChangeLogEntry) == changes


def test_a_date_with_other_records_is_refused(backend, engine) -> None:
    from app.planning.models import Task

    backend.register(email=EMAIL, password=SECRET, display_name=NAME)
    account = backend.sign_in(email=EMAIL, password=SECRET)
    account.planning_service().create_task(Task(user_id=account.user_id, name="Mine", category="c",
                                                estimated_duration_minutes=30, priority=5))  # undated: eligible
    code, out = run(engine, ARGS)
    assert code == 1 and "already holds 1 other" in out and "choose another --anchor-date" in out
    assert count(engine, models.Task) == 1 and count(engine, models.FixedBlock) == 0


def test_a_failed_run_resumes_safely(engine, monkeypatch) -> None:
    def interrupted(*_args, **_kwargs):
        raise RuntimeError("simulated interruption with private detail 4711")

    monkeypatch.setattr(verify_render, "_generate", interrupted)
    code, out = run(engine, ARGS)
    assert code == 2 and "seed 7 record(s) created" in out and "generation FAILED" in out and "execution not run" in out
    assert "4711" not in out  # an unexpected error is named, never echoed
    monkeypatch.undo()
    code, out = run(engine, ARGS)
    assert code == 0 and "seed already seeded" in out and "generation generated" in out
    assert count(engine, models.Task) == 3 and count(engine, models.Placement) == 3


def test_concurrent_reruns_leave_one_consistent_seed(backend, engine) -> None:
    backend.register(email=EMAIL, password=SECRET, display_name=NAME)
    codes: list[int] = []
    barrier = threading.Barrier(3)

    def attempt() -> None:
        barrier.wait()
        codes.append(run(engine, ARGS)[0])

    threads = [threading.Thread(target=attempt) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)
    assert len(codes) == 3 and 0 in codes  # a loser reports a safe conflict; nothing is duplicated
    code, out = run(engine, ARGS)
    assert code == 0 and "0 new change-log entries" in out
    assert count(engine, models.Task) == 3 and count(engine, models.FixedBlock) == 4
    assert count(engine, models.Placement, deleted_at=None) == 3 and count(engine, models.Execution) == 1


def test_two_accounts_get_their_own_seed(engine) -> None:
    assert run(engine, ARGS)[0] == 0
    other = ["--write-sample", "--email", "other@example.com", "--display-name", "Other", "--anchor-date",
             "2026-09-28", "--timezone", "America/Vancouver"]
    assert run(engine, other)[0] == 0
    with session_factory(engine)() as session:
        owners = {row.user_id for row in session.scalars(select(models.Task))}
        assert len(owners) == 2 and count(engine, models.Task) == 6  # distinct ids per account


def test_the_dependency_fixture_round_trips_on_its_own_date(engine) -> None:
    code, out = run(engine, [*ARGS, "--dependency-date", "2026-09-29"])
    assert code == 0, out
    assert "dependency_chain_linear.csv on 2026-09-29: 3 dependencies" in out
    with session_factory(engine)() as session:
        tasks = {row.name: row.id for row in session.scalars(select(models.Task))}
        order = {row.task_id: row.planned_start for row in session.scalars(select(models.Placement).where(
            models.Placement.planned_date == datetime(2026, 9, 29).date()))}
        assert order[tasks["Task A"]] < order[tasks["Task B"]] < order[tasks["Task C"]] < order[tasks["Task D"]]
    assert run(engine, [*ARGS, "--dependency-date", "2026-09-29"])[0] == 0  # idempotent too


def test_pytest_never_takes_its_database_from_database_url() -> None:
    """Only TEST_DATABASE_URL (never DATABASE_URL or a .env file) selects a PostgreSQL test database."""
    for path in [ROOT / "tests" / "backend" / "conftest.py", ROOT / "tests" / "direct" / "conftest.py",
                 ROOT / "tests" / "backend" / "test_postgres.py"]:
        text = path.read_text(encoding="utf-8")
        assert "dotenv" not in text and "env_file" not in text
        assert text.replace("TEST_DATABASE_URL", "").count('"DATABASE_URL"') == 0, path
