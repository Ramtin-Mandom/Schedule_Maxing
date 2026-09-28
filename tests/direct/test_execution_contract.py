"""The execution lifecycle and the explicit reschedule contract
(docs/execution-rescheduling.md), driven identically against every storage
path: the desktop's local SQLite services, the direct PostgreSQL services
(app/persistence -- SQLite server schema by default, PostgreSQL with
BACKEND_TESTS_ON_POSTGRES=1) and the hosted REST API over the same server
database. One transition table, one set of invariants. The REST driver needs
FastAPI; in the direct-only environment (requirements-direct.txt) it is
skipped and the other two still run."""

from __future__ import annotations

import importlib.util
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from app.execution.db import get_connection
from app.execution.errors import InvalidTransitionError
from app.execution.lifecycle import TRANSITIONS
from app.execution.models import ExecutionStatus
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning import workflow
from app.planning.application import PlanningService
from app.planning.errors import HistoryProtectedError, RescheduleRejectedError, VersionConflictError
from app.planning.models import ScheduledTask, Task
from app.planning.repository import PlanningRepository
from tests.direct.conftest import PASSWORD, account

HAS_FASTAPI = importlib.util.find_spec("fastapi") is not None


def _refusal(error: Exception) -> "Refused":
    """The API error code and reason of a planning refusal (as backend.planning_api.api_error maps it)."""
    if isinstance(error, HistoryProtectedError):
        return Refused("history_protected", error.status)
    if isinstance(error, RescheduleRejectedError):
        return Refused("reschedule_rejected", error.problems[0].reason)
    if isinstance(error, VersionConflictError):
        return Refused("deleted" if error.deleted else "version_conflict")
    return Refused(type(error).__name__)

MONDAY = date(2026, 3, 2)
ACTIONS = ("start", "pause", "resume", "complete", "skip", "cancel")
#: How each source status is reached from a fresh (scheduled) execution.
SETUP = {
    "scheduled": (),
    "in_progress": ("start",),
    "paused": ("start", "pause"),
    "completed": ("start", "complete"),
    "skipped": ("skip",),
    "cancelled": ("cancel",),
}


class Illegal(Exception):
    """The backend refused a lifecycle action as an invalid transition."""


class Refused(Exception):
    """The backend refused a reschedule; `code` is the API error code."""

    def __init__(self, code: str, reason: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason


def at(hour: int, minute: int = 0, day: date = MONDAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


def _instant(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ServiceDriver:
    """PlanningService + ExecutionService: the local SQLite ones or the direct PostgreSQL ones."""

    def __init__(self, planning, executions, owner) -> None:
        self.planning, self.executions, self.owner = planning, executions, owner

    def placement(self, hour: int = 9, day: date = MONDAY, **task_fields) -> tuple[uuid.UUID, uuid.UUID]:
        task = self.planning.create_task(Task(user_id=self.owner, name=task_fields.pop("name", "Study"), category="study",
                                              estimated_duration_minutes=60, priority=5, **task_fields))
        placement = ScheduledTask(task_id=task.id, user_id=self.owner, planned_date=day, timezone="UTC",
                                  planned_start=at(hour, day=day), planned_end=at(hour + 1, day=day))
        existing = self.planning.placements_for_date(day)
        self.planning.replace_placements(day, day, [*existing, placement],
                                         expected_versions={p.id: p.version for p in existing})
        return task.id, placement.id

    def execution(self, task_id, placement_id) -> str:
        task = self.planning.get_task(task_id)
        return self.executions.get_or_create_canonical_execution(task, self.planning.get_placement(placement_id)).id

    def act(self, execution_id: str, action: str) -> None:
        try:
            getattr(self.executions, action)(execution_id)
        except InvalidTransitionError as error:
            raise Illegal(action) from error

    def read(self, execution_id: str) -> dict:
        execution = self.executions.get_execution(execution_id)
        return {
            "status": execution.status.value, "version": execution.version,
            "sessions": [(_instant(s.started_at), _instant(s.ended_at)) for s in self.executions.list_sessions(execution_id)],
            "first_start": execution.actual_first_start_at, "final_end": execution.actual_final_end_at,
            "active": execution.actual_active_duration_minutes, "scheduled_task_id": execution.scheduled_task_id,
        }

    def reschedule(self, placement_id, hour: int, day: date = MONDAY, *, version: int | None = None, minutes: int = 60,
                   replacement_id=None) -> dict:
        stored = self.planning.get_placement(placement_id, include_deleted=True)
        try:
            result = workflow.reschedule_placement(
                self.planning, placement_id, expected_version=version or stored.version, planned_date=day,
                timezone_name="UTC", planned_start=at(hour, day=day),
                planned_end=at(hour, day=day) + timedelta(minutes=minutes), replacement_id=replacement_id,
            )
        except Exception as error:  # noqa: BLE001 - mapped to the API's code, as every client sees it
            raise _refusal(error) from error
        return {"previous": result.previous, "replacement": result.replacement,
                "cancelled": result.cancelled_execution_id}

    def stored_placement(self, placement_id) -> ScheduledTask | None:
        return self.planning.get_placement(placement_id, include_deleted=True)

    def live_placements(self, day: date = MONDAY) -> list[ScheduledTask]:
        return self.planning.placements_for_date(day)


class RestDriver:
    """The hosted HTTP API over the same server database (records, actions, the planning reschedule)."""

    def __init__(self, client, email: str) -> None:
        self.client, self.email = client, email
        self._token = None

    def request(self, method: str, path: str, **kwargs):
        """One call, signing in again when the (fake-clock) access token has expired."""
        for _ in range(2):
            if self._token is None:
                self._token = self.client.post("/auth/login", json={"email": self.email, "password": PASSWORD}).json()
            response = self.client.request(method, path, headers={"Authorization": f"Bearer {self._token['access_token']}"},
                                           **kwargs)
            if response.status_code != 401:
                return response
            self._token = None
        return response

    def get(self, path: str, **params):
        return self.request("GET", path, params=params)

    def _post(self, path: str, body: dict) -> dict:
        response = self.request("POST", path, json=body)
        assert response.status_code in (200, 201), response.text
        return response.json()

    def placement(self, hour: int = 9, day: date = MONDAY, **task_fields) -> tuple[uuid.UUID, uuid.UUID]:
        task = self._post("/tasks", {"name": task_fields.pop("name", "Study"), "category": "study",
                                     "estimated_duration_minutes": 60, "priority": 5,
                                     **{k: str(v) if isinstance(v, date) else v for k, v in task_fields.items()}})
        placement = self._post("/placements", {
            "task_id": task["id"], "planned_date": day.isoformat(), "timezone": "UTC",
            "planned_start": at(hour, day=day).isoformat(), "planned_end": at(hour + 1, day=day).isoformat()})
        return uuid.UUID(task["id"]), uuid.UUID(placement["id"])

    def execution(self, task_id, placement_id) -> str:
        placement = self.get(f"/placements/{placement_id}").json()
        return self._post("/executions", {
            "task_id": str(task_id), "scheduled_task_id": str(placement_id), "task_name": "Study",
            "category": "study", "planned_duration": 60, "priority": 5,
            "canonical_planned_date": placement["planned_date"], "canonical_timezone": "UTC",
            "canonical_planned_start": placement["planned_start"], "canonical_planned_end": placement["planned_end"],
        })["id"]

    def act(self, execution_id: str, action: str) -> None:
        version = self.get(f"/executions/{execution_id}").json()["version"]
        response = self.request("POST", f"/executions/{execution_id}/actions/{action}", json={"base_version": version})
        if response.status_code == 409 and response.json()["error"]["code"] == "invalid_transition":
            raise Illegal(action)
        assert response.status_code == 200, response.text

    def read(self, execution_id: str) -> dict:
        record = self.get(f"/executions/{execution_id}", include_deleted=True).json()
        return {
            "status": record["status"], "version": record["version"],
            "sessions": [(_instant(s["started_at"]), _instant(s["ended_at"])) for s in record["sessions"]],
            "first_start": _instant(record["actual_first_start_at"]), "final_end": _instant(record["actual_final_end_at"]),
            "active": record["actual_active_duration_minutes"],
            "scheduled_task_id": uuid.UUID(record["scheduled_task_id"]) if record["scheduled_task_id"] else None,
        }

    def reschedule(self, placement_id, hour: int, day: date = MONDAY, *, version: int | None = None, minutes: int = 60,
                   replacement_id=None) -> dict:
        stored = self.get(f"/placements/{placement_id}", include_deleted=True).json()
        start = at(hour, day=day)
        body = {"base_version": version or stored["version"], "planned_date": day.isoformat(), "timezone": "UTC",
                "planned_start": start.isoformat(), "planned_end": (start + timedelta(minutes=minutes)).isoformat()}
        if replacement_id is not None:
            body["replacement_id"] = str(replacement_id)
        response = self.request("POST", f"/planning/placements/{placement_id}/reschedule", json=body)
        if response.status_code != 200:
            error = response.json()["error"]
            raise Refused(error["code"], error.get("reason"))
        result = response.json()
        return {"previous": ScheduledTask.model_validate(result["previous"]),
                "replacement": ScheduledTask.model_validate(result["replacement"]),
                "cancelled": result["cancelled_execution_id"]}

    def stored_placement(self, placement_id) -> ScheduledTask | None:
        response = self.get(f"/placements/{placement_id}", include_deleted=True)
        return ScheduledTask.model_validate(response.json()) if response.status_code == 200 else None

    def live_placements(self, day: date = MONDAY) -> list[ScheduledTask]:
        items = self.get("/placements", limit=500).json()["items"]
        return [ScheduledTask.model_validate(item) for item in items if item["planned_date"] == day.isoformat()]


@pytest.fixture(params=["local", "direct", pytest.param("rest", marks=pytest.mark.skipif(
    not HAS_FASTAPI, reason="the REST driver needs FastAPI (not in the direct-only environment)"))])
def driver(request, tmp_path, backend, engine, clock):
    if request.param == "local":
        connection = get_connection(tmp_path / "device.db")
        yield ServiceDriver(PlanningService(PlanningRepository(connection), clock),
                            ExecutionService(ExecutionRepository(connection), clock), None)
        connection.close()
    elif request.param == "direct":
        alice = account(backend, "alice@example.com")
        yield ServiceDriver(alice.planning_service(), alice.execution_service(), alice.user_id)
    else:
        from fastapi.testclient import TestClient

        from backend.app import create_app
        from backend.settings import BackendSettings
        from tests.backend.conftest import TEST_SECRET

        app = create_app(BackendSettings(database_url="sqlite://", jwt_secret=TEST_SECRET), engine=engine, clock=clock)
        with TestClient(app) as client:
            assert client.post("/auth/register", json={"email": "alice@example.com", "password": PASSWORD}).status_code == 201
            yield RestDriver(client, "alice@example.com")


# -----------------------------------------------------------------------------
# The lifecycle
# -----------------------------------------------------------------------------


def test_every_transition_is_legal_or_refused_exactly_as_the_table_says(driver, clock) -> None:
    for source, steps in SETUP.items():
        for action in ACTIONS:
            task_id, placement_id = driver.placement(hour=9)
            execution_id = driver.execution(task_id, placement_id)
            for step in steps:
                clock.advance(seconds=5)
                driver.act(execution_id, step)
            before = driver.read(execution_id)
            assert before["status"] == source
            allowed, target = TRANSITIONS[action]
            clock.advance(seconds=5)
            if ExecutionStatus(source) in allowed:
                driver.act(execution_id, action)
                after = driver.read(execution_id)
                assert after["status"] == target.value, (source, action)
                assert after["version"] == before["version"] + 1, (source, action)
            else:
                with pytest.raises(Illegal):
                    driver.act(execution_id, action)
                assert driver.read(execution_id) == before, (source, action)  # nothing changed at all


def test_paused_time_is_excluded_and_first_start_and_final_end_are_kept(driver, clock) -> None:
    task_id, placement_id = driver.placement(hour=9)
    execution_id = driver.execution(task_id, placement_id)
    started = clock.now
    driver.act(execution_id, "start")
    clock.advance(minutes=10)
    driver.act(execution_id, "pause")
    clock.advance(minutes=30)  # paused: never active time
    driver.act(execution_id, "resume")
    clock.advance(minutes=20)
    finished = clock.now
    driver.act(execution_id, "complete")

    state = driver.read(execution_id)
    assert state["status"] == "completed" and state["active"] == 30.0
    assert state["first_start"] == started and state["final_end"] == finished
    assert [end - start for start, end in state["sessions"]] == [timedelta(minutes=10), timedelta(minutes=20)]


@pytest.mark.parametrize("ending", ["skip", "cancel"])
def test_skipping_or_cancelling_after_work_keeps_sessions_but_is_no_duration_sample(driver, clock, ending) -> None:
    task_id, placement_id = driver.placement(hour=9)
    execution_id = driver.execution(task_id, placement_id)
    started = clock.now
    driver.act(execution_id, "start")
    clock.advance(minutes=15)
    driver.act(execution_id, "pause")
    clock.advance(minutes=5)
    driver.act(execution_id, "resume")
    clock.advance(minutes=7)
    driver.act(execution_id, ending)  # closes the open session

    state = driver.read(execution_id)
    assert state["status"] == TRANSITIONS[ending][1].value
    assert len(state["sessions"]) == 2 and all(end is not None for _, end in state["sessions"])
    assert state["first_start"] == started and state["final_end"] == clock.now
    assert state["active"] is None  # not a completed duration sample
    for action in ACTIONS:  # terminal: nothing reopens it
        with pytest.raises(Illegal):
            driver.act(execution_id, action)


# -----------------------------------------------------------------------------
# The reschedule contract
# -----------------------------------------------------------------------------


def test_moving_an_unstarted_placement_keeps_the_original_and_cancels_its_unstarted_attempt(driver, clock) -> None:
    task_id, placement_id = driver.placement(hour=9)
    execution_id = driver.execution(task_id, placement_id)
    before = driver.read(execution_id)
    clock.advance(minutes=3)

    moved = driver.reschedule(placement_id, 14)

    previous, replacement = moved["previous"], moved["replacement"]
    assert previous.id == placement_id and previous.deleted_at is not None
    assert (previous.planned_start, previous.planned_end) == (at(9), at(10))  # the original plan stays readable
    assert previous.removal_reason.value == "rescheduled" and previous.superseded_by_id == replacement.id
    assert replacement.task_id == task_id and replacement.deleted_at is None
    assert (replacement.planned_start, replacement.planned_end) == (at(14), at(15))
    assert replacement.task_category == "study"
    assert moved["cancelled"] == execution_id

    cancelled = driver.read(execution_id)
    assert cancelled["status"] == "cancelled" and cancelled["sessions"] == [] and cancelled["active"] is None
    assert cancelled["version"] == before["version"] + 1 and cancelled["scheduled_task_id"] == placement_id
    assert cancelled["final_end"] == clock.now and cancelled["first_start"] is None
    with pytest.raises(Illegal):  # not actionable as current work any more
        driver.act(execution_id, "start")

    # The replacement is ordinary current work: it gets its own execution.
    fresh = driver.execution(task_id, replacement.id)
    assert fresh != execution_id
    driver.act(fresh, "start")
    assert [p.id for p in driver.live_placements()] == [replacement.id]


@pytest.mark.parametrize("steps", [("start",), ("start", "pause"), ("start", "complete"), ("skip",), ("cancel",)])
def test_started_or_finished_attempts_are_never_moved(driver, clock, steps) -> None:
    task_id, placement_id = driver.placement(hour=9)
    execution_id = driver.execution(task_id, placement_id)
    for step in steps:
        clock.advance(minutes=5)
        driver.act(execution_id, step)
    before = driver.read(execution_id)

    with pytest.raises(Refused) as refused:
        driver.reschedule(placement_id, 14)

    assert refused.value.code == "history_protected" and refused.value.reason == before["status"]
    assert driver.read(execution_id) == before
    stored = driver.stored_placement(placement_id)
    assert stored.deleted_at is None and stored.planned_start == at(9)
    assert [p.id for p in driver.live_placements()] == [placement_id]


def test_invalid_destinations_and_stale_versions_change_nothing(driver, clock) -> None:
    task_id, placement_id = driver.placement(hour=9)
    other_task, other_placement = driver.placement(hour=12, name="Other")
    execution_id = driver.execution(task_id, placement_id)
    before = (driver.stored_placement(placement_id), driver.read(execution_id))
    version = before[0].version

    cases = [
        ({"hour": 12}, "reschedule_rejected", "overlaps_placement"),
        ({"hour": 14, "minutes": 45}, "reschedule_rejected", "duration_changed"),
        ({"hour": 9}, "reschedule_rejected", "unchanged"),
        ({"hour": 14, "version": version + 1}, "version_conflict", None),
    ]
    for kwargs, code, reason in cases:
        with pytest.raises(Refused) as refused:
            driver.reschedule(placement_id, **kwargs)
        assert refused.value.code == code, kwargs
        if reason is not None:
            assert refused.value.reason == reason, kwargs
        assert (driver.stored_placement(placement_id), driver.read(execution_id)) == before
        assert sorted(p.id for p in driver.live_placements()) == sorted([placement_id, other_placement])

    moved = driver.reschedule(placement_id, 14)  # the same move, valid now
    with pytest.raises(Refused) as again:  # a repeated move of the (now moved) placement: a clear conflict
        driver.reschedule(placement_id, 16, version=version)
    assert again.value.code == "deleted"
    assert driver.stored_placement(placement_id).superseded_by_id == moved["replacement"].id


def test_dependencies_and_dependents_are_hard_rules_for_a_move(driver) -> None:
    first_task, first = driver.placement(hour=9, name="First")
    _, second = driver.placement(hour=11, name="Second", dependency_ids=[str(first_task)])
    with pytest.raises(Refused) as dependent:  # the dependent would start before its dependency ends
        driver.reschedule(first, 13)
    assert dependent.value.code == "reschedule_rejected" and dependent.value.reason == "dependent_starts_first"
    with pytest.raises(Refused) as dependency:
        driver.reschedule(second, 8)
    assert dependency.value.reason == "dependency_not_satisfied"
    assert driver.reschedule(second, 15)["replacement"].planned_start == at(15)
