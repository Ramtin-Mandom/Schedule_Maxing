"""The Day page's Uncompleted | Tasks | Completed board through the desktop's own services (no
display): only saved placements are on it, their column is their execution's status, and it is
keyed by placement id -- so Make Schedule again (incremental or a full regeneration), adding and
removing tasks, duplicate names, recurring occurrences and restarts never move a status, never
put an optimizer-unscheduled task in a column, and never overwrite actual execution history."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.execution.lifecycle import TaskOutcome
from app.execution.models import ExecutionStatus
from app.planning.models import FixedBlock, LocalTimeWindow, RecurrenceFrequency, RecurrenceSpec, Task
from app.ui import background
from app.ui.app_services import open_app_services
from app.ui.day_controller import DayScheduleController
from app.ui.schedule_page_controller import RowRef
from app.ui.task_status import TaskStatusController

DAY = date(2026, 9, 24)
TZ = "America/Vancouver"
P, C, U = TaskOutcome.PENDING, TaskOutcome.COMPLETED, TaskOutcome.UNCOMPLETED


@pytest.fixture(autouse=True)
def _restore_installed_registry():
    previous = background.current_registry()
    yield
    background.install_registry(previous)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "status.db"


@pytest.fixture
def services(db_path: Path, tmp_path: Path):
    opened = open_app_services(db_path, timezone=TZ, project_root=str(tmp_path))
    yield opened
    opened.close()


def ok(result):
    assert result.ok, result.error
    return result.value


def add(services, name: str, *, minutes: int = 60, window: tuple[int, int] | None = None, **extra) -> Task:
    fields = dict(name=name, category="study", estimated_duration_minutes=minutes, priority=5, preferred_dates=[DAY])
    if window is not None:
        fields["preferred_time_window"] = LocalTimeWindow(start_minute=window[0], end_minute=window[1])
    fields.update(extra)
    return ok(services.planning_controller.add_or_update_task(Task(**fields)))


class Day:
    """The Day page's two presenters for one date."""

    def __init__(self, services) -> None:
        self.page = DayScheduleController(services.planning_controller, anchor_date=DAY, timezone=TZ)
        self.status = TaskStatusController(services.execution_controller)

    def board(self):
        return ok(self.status.board(DAY, ok(self.page.load()).executables))

    def columns(self) -> dict[TaskOutcome, list[str]]:
        board = self.board()
        return {outcome: sorted(card.name for card in board.column(outcome)) for outcome in (U, P, C)}

    def move(self, name: str, target: TaskOutcome, *, index: int = 0):
        card = [card for card in self.board().cards if card.name == name][index]
        return ok(self.status.move(card, target))

    def make_schedule(self):
        return ok(self.page.make_schedule())


def test_a_new_scheduled_task_waits_in_tasks_and_moves_through_every_column(services) -> None:
    add(services, "Read")
    day = Day(services)
    assert day.columns() == {U: [], P: [], C: []}  # nothing is scheduled before Make Schedule
    day.make_schedule()
    assert day.columns() == {U: [], P: ["Read"], C: []}
    assert services.execution_controller.list_executions().value == []  # viewing the board creates nothing

    day.move("Read", C)
    assert day.columns() == {U: [], P: [], C: ["Read"]}  # Tasks -> Completed
    day.move("Read", P)
    assert day.columns() == {U: [], P: ["Read"], C: []}  # Completed -> Tasks
    day.move("Read", U)
    assert day.columns() == {U: ["Read"], P: [], C: []}  # Tasks -> Uncompleted
    day.move("Read", P)
    assert day.columns() == {U: [], P: ["Read"], C: []}  # Uncompleted -> Tasks
    [execution] = services.execution_controller.list_executions().value  # one execution, moved by actions
    assert execution.status == ExecutionStatus.SCHEDULED and execution.version == 5


def test_make_schedule_again_keeps_every_status_and_only_new_work_enters_tasks(services) -> None:
    add(services, "A", window=(480, 600))
    add(services, "B", window=(600, 720))
    add(services, "C", window=(720, 840))
    day = Day(services)
    day.make_schedule()
    day.move("A", C)
    day.move("C", U)
    before = {card.name: card.key for card in day.board().cards}

    add(services, "D", window=(840, 960))
    run = day.make_schedule()  # incremental: the saved work that fits stays, D is added
    assert run.status == "generated"
    assert day.columns() == {U: ["C"], P: ["B", "D"], C: ["A"]}
    after = {card.name: card.key for card in day.board().cards}
    assert (after["A"], after["B"], after["C"]) == (before["A"], before["B"], before["C"])  # same placements

    ok(day.page.regenerate_for(DAY))  # a full regeneration protects answered work, never resets it
    assert day.columns() == {U: ["C"], P: ["B", "D"], C: ["A"]}
    regenerated = {card.name: card.key for card in day.board().cards}
    assert (regenerated["A"], regenerated["C"]) == (before["A"], before["C"])


def test_generation_never_touches_actual_execution_history(services) -> None:
    add(services, "Timed", window=(480, 600))
    day = Day(services)
    day.make_schedule()
    card = day.board().cards[0]
    execution = ok(services.execution_controller.get_or_create_canonical_execution(card.task, card.placement))
    execution = ok(services.execution_controller.start(execution.id))
    execution = ok(services.execution_controller.complete(execution.id))
    snapshot = execution.model_dump()

    add(services, "Later", window=(900, 960))
    day.make_schedule()
    ok(day.page.regenerate_for(DAY))
    assert ok(services.execution_controller.get_execution(execution.id)).model_dump() == snapshot
    assert day.columns()[C] == ["Timed"]


def test_an_optimizer_unscheduled_task_is_in_no_column(services) -> None:
    ok(services.planning_controller.save_fixed_block(FixedBlock(
        label="Shift", category="work", planned_date=DAY, timezone=TZ,
        planned_start=datetime(2026, 9, 24, 2, tzinfo=ZoneInfo(TZ)),
        planned_end=datetime(2026, 9, 24, 22, 30, tzinfo=ZoneInfo(TZ)))))  # 3.5 h free around it
    add(services, "Fits")
    add(services, "Far too long", minutes=300)  # no free stretch (or total) is long enough: left out
    day = Day(services)
    run = day.make_schedule()
    assert any("Far too long" in reason for reason in run.reasons)
    assert day.columns() == {U: [], P: ["Fits"], C: []}
    assert [task.name for task in run.snapshot.unplaced] == ["Far too long"]  # shown with Available tasks instead


def test_duplicate_names_keep_separate_statuses(services) -> None:
    add(services, "Review", window=(480, 540))
    add(services, "Review", window=(900, 960))
    day = Day(services)
    day.make_schedule()
    day.move("Review", C, index=0)
    board = day.board()
    assert [card.outcome for card in board.cards] == [C, P]  # by placement, in time order -- never by name
    day.make_schedule()
    assert [card.outcome for card in day.board().cards] == [C, P]


def test_recurring_occurrences_do_not_share_a_status(services) -> None:
    walk = add(services, "Walk", recurrence=RecurrenceSpec(frequency=RecurrenceFrequency.DAILY), preferred_dates=[])
    today, tomorrow = Day(services), Day(services)
    tomorrow.page = DayScheduleController(services.planning_controller, anchor_date=date(2026, 9, 25), timezone=TZ)
    today.make_schedule()
    ok(tomorrow.page.make_schedule())
    today.move("Walk", U)
    tomorrow_board = ok(tomorrow.status.board(date(2026, 9, 25), ok(tomorrow.page.load()).executables))
    assert [card.outcome for card in tomorrow_board.cards] == [P]  # the next occurrence is untouched
    assert tomorrow_board.cards[0].task.id == walk.id != None  # noqa: E711 - same template, own placement


def test_removing_a_scheduled_task_is_safe_and_keeps_its_history(services) -> None:
    done = add(services, "Done", window=(480, 540))
    add(services, "Dropped", window=(600, 660))
    day = Day(services)
    day.make_schedule()
    day.move("Done", C)
    dropped = next(card for card in day.board().cards if card.name == "Dropped")
    ok(day.page.delete(RowRef("task", dropped.task.id, dropped.task.version)))
    assert day.columns() == {U: [], P: [], C: ["Done"]}  # the removed task simply leaves the board

    stored = ok(services.planning_controller.get_task(done.id))
    ok(day.page.delete(RowRef("task", done.id, stored.version)))
    assert day.columns() == {U: [], P: [], C: []}  # an answered task that is removed leaves the board...
    [execution] = ok(services.execution_controller.list_executions())  # ...while its completion stays as history
    assert execution.status == ExecutionStatus.COMPLETED and execution.task_id == done.id


def test_statuses_survive_a_restart(db_path: Path, tmp_path: Path) -> None:
    services = open_app_services(db_path, timezone=TZ, project_root=str(tmp_path))
    try:
        add(services, "Kept", window=(480, 540))
        add(services, "Missed", window=(600, 660))
        add(services, "Open", window=(720, 780))
        day = Day(services)
        day.make_schedule()
        day.move("Kept", C)
        day.move("Missed", U)
    finally:
        services.close()
    services = open_app_services(db_path, timezone=TZ, project_root=str(tmp_path))
    try:
        assert Day(services).columns() == {U: ["Missed"], P: ["Open"], C: ["Kept"]}
    finally:
        services.close()


def test_a_stale_board_is_refused_not_overwritten(services) -> None:
    add(services, "Twice")
    day = Day(services)
    day.make_schedule()
    shown = day.board().cards[0]  # a second window still shows "no execution"
    day.move("Twice", C)
    result = day.status.move(shown, U)
    assert not result.ok and "changed elsewhere" in result.error
    assert day.columns() == {U: [], P: [], C: ["Twice"]}
