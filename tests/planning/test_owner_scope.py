"""Request-scoped ownership (app/planning/scope.py) at the service layer: account
A, account B, and the ownerless local workspace are separate scopes for
every operation family -- tasks, projects, references, fixed blocks,
placements, preferences, provenance, executions, reset and CSV -- while the
legacy device-wide service keeps seeing everything."""

from __future__ import annotations

import csv
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from app.execution.errors import ExecutionError, ExecutionNotFoundError
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning.application import PlanningService, RecordBatch
from app.planning.csv_export import export_planning_csv
from app.planning.errors import EntityNotFoundError, InvalidReferenceError, ScopeError, VersionConflictError
from app.planning.models import FixedBlock, Project, ScheduledTask, Task
from app.planning.preferences import DayWindowSpec, OptimizerMode, PreferenceOverrides
from app.planning.provenance import GenerationRecord
from app.planning.scope import OwnerScope

DAY = date(2026, 9, 23)
A, B = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001"), uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
SCOPES = {"A": OwnerScope.account(A), "B": OwnerScope.account(B), "local": OwnerScope.ownerless()}
OWNERS = {"A": A, "B": B, "local": None}


def at(hour: int) -> datetime:
    return datetime(DAY.year, DAY.month, DAY.day, hour, tzinfo=timezone.utc)


@pytest.fixture
def services(planning_service: PlanningService, connection) -> dict[str, PlanningService]:
    return {name: planning_service.scoped(scope) for name, scope in SCOPES.items()}


@pytest.fixture
def seeded(services, connection) -> dict[str, dict]:
    """The same kind of records, at the same times, in each of the three scopes."""
    records = {}
    for name, service in services.items():
        owner = OWNERS[name]
        project = service.create_project(Project(name=f"{name} project", user_id=owner))
        task = service.create_task(Task(name=f"{name} task", category="study", estimated_duration_minutes=60, priority=5,
                                        user_id=owner, project_id=project.id, required_date=DAY))
        block = service.create_fixed_block(FixedBlock(label=f"{name} block", planned_date=DAY, timezone="UTC",
                                                      planned_start=at(12), planned_end=at(13), user_id=owner))
        replacement = service.replace_placements(DAY, DAY, [ScheduledTask(
            task_id=task.id, planned_date=DAY, timezone="UTC", planned_start=at(9), planned_end=at(10))])
        layer = service.save_date_preferences(
            DAY, PreferenceOverrides(day_window=DayWindowSpec(start_minute=60 * {"A": 6, "B": 7, "local": 8}[name],
                                                              end_minute=1440)))
        user_layer = service.save_user_preferences(PreferenceOverrides(optimizer_mode=OptimizerMode.ADHD_FRIENDLY)) \
            if name == "A" else None
        service._repository.insert_generation(GenerationRecord(
            user_id=owner, planned_date=DAY, timezone="UTC", engine_mode="precise_greedy", range_start=DAY,
            range_end=DAY, range_scope="planned", allocation_id=uuid.uuid4(), fingerprint="f" * 64,
            placements_digest="d" * 64, placement_count=1, unscheduled_count=0, total_score=1.0, generated_at=at(8),
        ))
        executions = ExecutionService(ExecutionRepository(connection)).scoped(SCOPES[name])
        execution = executions.create_canonical_execution(task, replacement.placements[0], user_id=owner)
        records[name] = dict(project=project, task=task, block=block, placement=replacement.placements[0],
                             layer=layer, user_layer=user_layer, execution=execution)
    return records


def test_every_read_sees_only_its_own_scope(services, seeded, planning_service) -> None:
    for name, service in services.items():
        own = seeded[name]
        assert [t.id for t in service.list_tasks()] == [own["task"].id]
        assert [p.id for p in service.list_projects()] == [own["project"].id]
        assert [b.id for b in service.fixed_blocks_for_date(DAY)] == [own["block"].id]
        assert [p.id for p in service.placements_for_date(DAY)] == [own["placement"].id]
        assert service.date_preferences(DAY).id == own["layer"].id
        assert [r.user_id for r in service.generation_records(DAY, DAY).values()] == [OWNERS[name]]
        loaded = service.load_range(DAY, DAY)
        assert loaded.task_ids == [own["task"].id]
        for other in set(services) - {name}:
            theirs = seeded[other]
            assert service.get_task(theirs["task"].id) is None
            assert service.get_project(theirs["project"].id) is None
            assert service.get_tasks_including_deleted([theirs["task"].id]) == {}
    # The legacy device-wide service still sees every owner.
    assert len(planning_service.list_tasks()) == 3 and len(planning_service.fixed_blocks_for_date(DAY)) == 3


def test_preferences_and_freshness_resolve_in_the_same_scope(services, seeded) -> None:
    resolved = {name: service.resolve_preferences([DAY], "UTC")[DAY] for name, service in services.items()}
    assert [resolved[n].day_window.start_minute for n in ("A", "B", "local")] == [360, 420, 480]
    assert resolved["A"].optimizer_mode == OptimizerMode.ADHD_FRIENDLY
    assert resolved["B"].optimizer_mode == resolved["local"].optimizer_mode == OptimizerMode.PRECISE_GREEDY
    assert services["B"].user_preferences() is None and services["local"].user_preferences() is None


def test_writes_cannot_reach_or_create_another_scopes_records(services, seeded, connection) -> None:
    a, b = services["A"], services["B"]
    theirs = seeded["B"]
    before = [tuple(r) for r in connection.execute("SELECT id, version, deleted_at FROM tasks ORDER BY id")]

    # B's task (and the B project it names) simply do not exist in A's scope.
    with pytest.raises((EntityNotFoundError, InvalidReferenceError)):
        a.update_task(theirs["task"].model_copy(update={"name": "stolen"}), expected_version=theirs["task"].version)
    with pytest.raises(EntityNotFoundError):
        a.update_task(theirs["task"].model_copy(update={"project_id": None}), expected_version=theirs["task"].version)
    assert a.delete_task(theirs["task"].id, expected_version=theirs["task"].version) is False
    assert a.delete_fixed_block(theirs["block"].id, expected_version=theirs["block"].version) is False
    with pytest.raises(EntityNotFoundError):
        a.update_fixed_block(theirs["block"], expected_version=theirs["block"].version)
    with pytest.raises(ScopeError):
        a.create_task(Task(name="for B", category="x", estimated_duration_minutes=5, priority=1, user_id=B))
    with pytest.raises(ScopeError):
        a.create_task(Task(name="ownerless", category="x", estimated_duration_minutes=5, priority=1))
    with pytest.raises(InvalidReferenceError):  # references resolve inside the scope only
        a.create_task(Task(name="dep", category="x", estimated_duration_minutes=5, priority=1, user_id=A,
                           dependency_ids=[theirs["task"].id]))
    with pytest.raises(InvalidReferenceError):
        a.create_task(Task(name="proj", category="x", estimated_duration_minutes=5, priority=1, user_id=A,
                           project_id=theirs["project"].id))
    with pytest.raises(InvalidReferenceError):  # a placement for B's task
        a.replace_placements(DAY, DAY, [ScheduledTask(task_id=theirs["task"].id, planned_date=DAY, timezone="UTC",
                                                      planned_start=at(15), planned_end=at(16))])
    assert [tuple(r) for r in connection.execute("SELECT id, version, deleted_at FROM tasks ORDER BY id")] == before
    assert b.get_task(theirs["task"].id) == theirs["task"]


def test_fixed_block_overlap_is_judged_per_owner(services, seeded) -> None:
    # Every scope already has a 12:00-13:00 block on DAY; a fourth account can add one at the same time.
    other = services["A"]._repository.scoped(OwnerScope.account(uuid.uuid4()))
    fourth = PlanningService(other)
    assert fourth.create_fixed_block(FixedBlock(label="Also noon", planned_date=DAY, timezone="UTC",
                                                planned_start=at(12), planned_end=at(13),
                                                user_id=other.owner.user_id)).label == "Also noon"


def test_reset_touches_only_its_own_scope(services, seeded, planning_service) -> None:
    preview = services["A"].reset_preview(DAY, DAY)
    assert preview.task_ids == [seeded["A"]["task"].id] and preview.fixed_block_ids == [seeded["A"]["block"].id]
    assert preview.date_preference_ids == [seeded["A"]["layer"].id]
    services["A"].reset_range(DAY, DAY, confirmation=preview.token)

    for name in ("B", "local"):
        assert [t.id for t in services[name].list_tasks()] == [seeded[name]["task"].id]
        assert services[name].date_preferences(DAY).id == seeded[name]["layer"].id
        assert len(services[name].generation_records(DAY, DAY)) == 1
    assert services["A"].list_tasks() == [] and services["A"].user_preferences() == seeded["A"]["user_layer"]
    # A scoped preview token is not valid in another scope.
    with pytest.raises(VersionConflictError):
        services["B"].reset_range(DAY, DAY, confirmation=preview.token)


def test_executions_are_scoped(connection, seeded) -> None:
    device = ExecutionService(ExecutionRepository(connection))
    scoped = {name: device.scoped(scope) for name, scope in SCOPES.items()}
    for name, service in scoped.items():
        assert [e.id for e in service.list_executions()] == [seeded[name]["execution"].id]
        for other in set(scoped) - {name}:
            with pytest.raises(ExecutionNotFoundError):
                service.get_execution(seeded[other]["execution"].id)
            with pytest.raises(ExecutionNotFoundError):
                service.start(seeded[other]["execution"].id)
    with pytest.raises(ExecutionError):
        scoped["A"].create_canonical_execution(seeded["B"]["task"], None, user_id=B)

    assert scoped["B"].reset_all_history() == 1  # the purge is scoped too
    assert len(device.list_executions()) == 2
    assert scoped["A"].list_executions()[0] == seeded["A"]["execution"]


def test_csv_export_and_import_are_scoped(services, seeded, tmp_path) -> None:
    path = tmp_path / "a.csv"
    result = export_planning_csv(services["A"], path, start_date=DAY, end_date=DAY)
    assert (result.tasks, result.fixed_blocks, result.placements, result.projects) == (1, 1, 1, 1)
    with path.open(encoding="utf-8") as file:
        owners = {row["user_id"] for row in csv.DictReader(file)}
    assert owners == {str(A)}

    b_batch = RecordBatch(tasks=[Task(name="B import", category="x", estimated_duration_minutes=5, priority=1, user_id=B)])
    with pytest.raises(ScopeError):
        services["A"].apply_record_batch(b_batch)
    services["B"].apply_record_batch(b_batch)
    assert {t.name for t in services["B"].list_tasks()} == {"B task", "B import"}

    # A legacy CSV carries no owner: its records belong to the importing scope.
    legacy = Task(name="legacy row", category="x", estimated_duration_minutes=5, priority=1)
    applied = services["A"].apply_import([legacy], [])
    assert applied.tasks[0].user_id == A
    assert services["local"].get_task(legacy.id) is None


def test_the_ownerless_scope_never_creates_records_for_an_active_account(services, connection) -> None:
    connection.execute(
        "INSERT INTO sync_accounts (account_key, backend_url, user_id, active, created_at) VALUES (?, ?, ?, 1, ?)",
        ("https://api#" + str(A), "https://api", str(A), "2026-09-23T00:00:00Z"),
    )
    before = [tuple(r) for r in connection.execute("SELECT * FROM tasks")]
    with pytest.raises(ScopeError, match="account is active"):
        services["local"].create_task(Task(name="offline", category="x", estimated_duration_minutes=5, priority=1))
    assert [tuple(r) for r in connection.execute("SELECT * FROM tasks")] == before
    # An explicit account scope creates its own records normally.
    assert services["A"].create_task(
        Task(name="mine", category="x", estimated_duration_minutes=5, priority=1, user_id=A)).user_id == A


def test_scoped_requires_an_owner_scope(planning_service: PlanningService) -> None:
    with pytest.raises(TypeError):
        planning_service.scoped(A)
    with pytest.raises(ValueError):
        OwnerScope.account(None)
    assert planning_service.owner_scope is None and planning_service.scoped(SCOPES["A"]).owner_scope == SCOPES["A"]


def test_scope_filters_placement_history_and_external_dependencies(services, seeded) -> None:
    a = services["A"]
    later = a.create_task(Task(name="later", category="x", estimated_duration_minutes=5, priority=1, user_id=A,
                               dependency_ids=[seeded["A"]["task"].id], required_date=DAY + timedelta(days=1)))
    external = a.external_dependencies([later], DAY + timedelta(days=1), DAY + timedelta(days=1), "UTC")
    assert set(external) == {seeded["A"]["task"].id}
    assert external[seeded["A"]["task"].id].state.value == "scheduled"


def test_generation_and_freshness_stay_in_the_scope(services, seeded) -> None:
    from app.planning.service import DayResultStatus
    from app.ui.planning_controller import PlanningController

    controllers = {name: PlanningController(service=service, timezone="UTC") for name, service in services.items()}
    run = controllers["A"].schedule_range(DAY, DAY)
    assert run.ok, run.error
    assert {p.task_id for p in run.value.replacement.placements} == {seeded["A"]["task"].id}
    [record] = services["A"].generation_records(DAY, DAY).values()
    assert record.user_id == A

    assert controllers["A"].day_state(DAY).value.status == DayResultStatus.GENERATED
    # B's and the ownerless workspace's saved schedules are neither replaced nor judged by A's run.
    for name in ("B", "local"):
        assert [p.id for p in services[name].placements_for_date(DAY)] == [seeded[name]["placement"].id]
        assert controllers[name].day_state(DAY).value.status == DayResultStatus.STALE
