"""One scenario, three read paths: the schedule-cohort report through the local
SQLite services, the direct PostgreSQL services (server schema; SQLite by
default, PostgreSQL with BACKEND_TESTS_ON_POSTGRES=1) and the HTTP route
GET /planning/analytics/schedule-cohort must agree, and each user sees only
their own history. The HTTP comparison needs FastAPI and is skipped in the
direct-only environment (requirements-direct.txt)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.execution.db import get_connection
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning import workflow
from app.planning.application import PlanningService
from app.planning.models import ScheduledTask, Task
from app.planning.repository import PlanningRepository
from app.productivity.reporting import ProductivityService
import pytest
from tests.direct.conftest import PASSWORD, account

VAN = "America/Vancouver"
MON, WED = date(2026, 3, 2), date(2026, 3, 4)
CUTOFF = datetime(2026, 3, 3, 12, tzinfo=timezone.utc)


def local_hour(day: date, hour: int) -> datetime:  # Vancouver is UTC-8 in early March 2026
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(hours=hour + 8)


def build(planning, executions, clock, owner) -> None:
    """Mon: done, skipped, untouched, in progress, cancelled, moved once; Wed: future work."""
    placements = {}
    for name, day, hour in (("Done", MON, 9), ("Skip", MON, 10), ("Untouched", MON, 11), ("Working", MON, 12),
                            ("Cancel", MON, 13), ("Moved", MON, 14), ("Future", WED, 9)):
        task = planning.create_task(Task(user_id=owner, name=name, category="study" if hour < 12 else "admin",
                                         estimated_duration_minutes=60, priority=5))
        placement = ScheduledTask(task_id=task.id, user_id=owner, planned_date=day, timezone=VAN,
                                  planned_start=local_hour(day, hour), planned_end=local_hour(day, hour + 1),
                                  task_category=task.category, created_at=clock.now, updated_at=clock.now)
        existing = planning.placements_for_date(day)
        planning.replace_placements(day, day, [*existing, placement], expected_versions={p.id: p.version for p in existing})
        placements[name] = (task, planning.get_placement(placement.id))

    def execution(name):
        task, placement = placements[name]
        return executions.get_or_create_canonical_execution(task, placement).id

    clock.now = local_hour(MON, 9)
    done = execution("Done")
    executions.start(done)
    clock.advance(minutes=20)
    executions.pause(done)
    clock.advance(minutes=10)
    executions.resume(done)
    clock.advance(minutes=20)
    executions.complete(done)
    clock.now = local_hour(MON, 10)
    executions.skip(execution("Skip"))
    clock.now = local_hour(MON, 12) + timedelta(minutes=5)
    executions.start(execution("Working"))
    clock.now = local_hour(MON, 13)
    executions.cancel(execution("Cancel"))
    moved = placements["Moved"][1]
    workflow.reschedule_placement(planning, moved.id, expected_version=moved.version, planned_date=MON,
                                  timezone_name=VAN, planned_start=local_hour(MON, 16), planned_end=local_hour(MON, 17))
    clock.now = CUTOFF


def projection(report: dict) -> dict:
    """The report without record identities, which differ between the SQLite and the server database."""
    return {key: value for key, value in report.items() if key not in ("occurrences", "underestimation")} | {
        "states": sorted((o["local_date"], o["planned_start"], o["state"], o["category"], o["reschedule_events"])
                         for o in report["occurrences"]),
    }


def _direct_report(backend, clock):
    alice = account(backend, "alice@example.com")
    build(alice.planning_service(), alice.execution_service(), clock, alice.user_id)
    account(backend, "bob@example.com")
    return alice.productivity_service(VAN).build_schedule_cohort_report(start_date=MON, end_date=WED, as_of=CUTOFF)


def test_local_and_direct_give_the_same_report(tmp_path, backend, clock) -> None:
    connection = get_connection(tmp_path / "device.db")
    try:
        local_clock = type(clock)()
        planning = PlanningService(PlanningRepository(connection), local_clock)
        build(planning, ExecutionService(ExecutionRepository(connection), local_clock), local_clock, None)
        local = ProductivityService(ExecutionRepository(connection), history=planning, timezone_name=VAN)
        local_report = local.build_schedule_cohort_report(start_date=MON, end_date=WED, as_of=CUTOFF)
    finally:
        connection.close()
    direct_report = _direct_report(backend, clock)

    # Done, Skip, Untouched, Working and the moved (still untouched) occurrence; Cancel is excluded.
    assert (direct_report.due_completion.numerator, direct_report.due_completion.denominator) == (1, 5)
    assert (direct_report.due_skip.numerator, direct_report.due_outcomes.cancelled) == (1, 1)
    assert (direct_report.due_outcomes.overdue_unattempted, direct_report.due_outcomes.in_progress) == (2, 1)
    assert direct_report.future_count == 1 and direct_report.reschedules.reschedule_events == 1
    assert direct_report.duration.median_signed_error_minutes == -20.0  # 40 active of a 60-minute estimate
    assert projection(local_report.model_dump(mode="json")) == projection(direct_report.model_dump(mode="json"))


def test_http_gives_the_direct_report_and_users_stay_apart(backend, engine, clock) -> None:
    pytest.importorskip("fastapi", reason="the HTTP adapter needs FastAPI (not in the direct-only environment)")
    from fastapi.testclient import TestClient

    from backend.app import create_app
    from backend.settings import BackendSettings
    from tests.backend.conftest import TEST_SECRET

    direct_report = _direct_report(backend, clock)
    app = create_app(BackendSettings(database_url="sqlite://", jwt_secret=TEST_SECRET), engine=engine,
                     clock=lambda: CUTOFF + timedelta(hours=1))
    with TestClient(app) as client:
        def report_for(email: str) -> dict:
            token = client.post("/auth/login", json={"email": email, "password": PASSWORD}).json()["access_token"]
            response = client.get("/planning/analytics/schedule-cohort", headers={"Authorization": f"Bearer {token}"},
                                  params={"start_date": "2026-03-02", "end_date": "2026-03-04", "timezone": VAN,
                                          "as_of": CUTOFF.isoformat()})
            assert response.status_code == 200, response.text
            return response.json()

        assert report_for("alice@example.com") == direct_report.model_dump(mode="json")
        bobs = report_for("bob@example.com")
        assert bobs["occurrence_count"] == 0 and bobs["due_completion"]["value"] is None

        token = client.post("/auth/login", json={"email": "alice@example.com", "password": PASSWORD}).json()
        headers = {"Authorization": f"Bearer {token['access_token']}"}
        future = client.get("/planning/analytics/schedule-cohort", headers=headers, params={
            "start_date": "2026-03-02", "end_date": "2026-03-04", "timezone": VAN, "as_of": "2026-03-09T00:00:00Z"})
        assert future.status_code == 422  # a cutoff after the server's now is refused
        bad_zone = client.get("/planning/analytics/schedule-cohort", headers=headers, params={
            "start_date": "2026-03-02", "end_date": "2026-03-04", "timezone": "Nowhere/Land"})
        assert bad_zone.status_code == 422
        assert client.get("/planning/analytics/schedule-cohort", params={
            "start_date": "2026-03-02", "end_date": "2026-03-04", "timezone": VAN}).status_code == 401
