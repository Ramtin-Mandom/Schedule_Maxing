"""The day engine in a separate process (app/planning/engine_process.py): same results, safe fallbacks."""

from __future__ import annotations

import multiprocessing
import threading
import uuid
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from datetime import date

import pytest

from app.planning import engine_process
from app.planning.engine_process import EngineProcess, run_engine


class _Pool:
    def __init__(self, outcome) -> None:
        self.outcome = outcome

    def submit(self, function, *args, **kwargs) -> Future:
        future = Future()
        if isinstance(self.outcome, BaseException):
            future.set_exception(self.outcome)
        else:
            future.set_result(self.outcome)
        return future


def test_without_a_runner_the_engine_is_called_directly() -> None:
    assert engine_process._runner is None
    assert run_engine(lambda value, *, plus: value + plus, 1, plus=2) == 3


def test_a_broken_process_falls_back_to_this_thread_for_good(monkeypatch) -> None:
    engine = EngineProcess()
    monkeypatch.setattr(engine, "_pool", lambda: _Pool(BrokenProcessPool("gone")))
    assert engine(lambda: "here") == "here"
    assert engine.local_calls == 1 and engine._failed
    monkeypatch.setattr(engine, "_pool", lambda: pytest.fail("not tried again"))
    assert engine(lambda: "again") == "again"


def test_an_engine_error_keeps_its_type_and_the_process(monkeypatch) -> None:
    engine = EngineProcess()
    monkeypatch.setattr(engine, "_pool", lambda: _Pool(ValueError("no window")))

    def fails():
        raise ValueError("no window")

    with pytest.raises(ValueError, match="no window"):
        engine(fails)
    assert not engine._failed  # a domain error says nothing about the process


def test_a_shut_down_engine_runs_calls_here(monkeypatch) -> None:
    engine = EngineProcess()
    engine.shutdown()
    assert engine(lambda: 5) == 5 and engine.remote_calls == 0


def _background_threads() -> set[threading.Thread]:
    return {thread for thread in threading.enumerate() if thread is not threading.main_thread()}


@pytest.mark.slow
def test_shutting_down_a_starting_process_leaves_no_thread_or_process_behind() -> None:
    before = _background_threads()
    engine = EngineProcess()
    engine.warm_up()  # the worker is still starting: what closing the app right after the window appears meets
    assert _background_threads() - before  # the pool's manager thread is running
    engine.shutdown()
    assert _background_threads() - before == set()
    assert multiprocessing.active_children() == []
    assert engine(lambda: 5) == 5 and engine.remote_calls == 0


def _generate(tmp_path, name: str, *, process: bool):
    from app.planning.models import Task
    from app.ui.app_services import open_app_services
    from app.ui.day_controller import DayScheduleController

    services = open_app_services(tmp_path / f"{name}.db", timezone="UTC", project_root=str(tmp_path),
                                 background_sync=False, engine_process=process)
    try:
        day = date(2026, 9, 21)
        for index in range(12):
            assert services.planning_controller.add_or_update_task(Task(
                id=uuid.uuid5(uuid.NAMESPACE_URL, f"engine:{index}"), name=f"Task {index}", category="study",
                estimated_duration_minutes=20 + index * 5, preferred_dates=[day])).ok
        controller = DayScheduleController(services.planning_controller, anchor_date=day, timezone="UTC",
                                           today=lambda: day)
        assert controller.make_schedule().ok
        placements = sorted((p.task_id, p.planned_start, p.planned_end)
                            for p in services.planning_controller.get_placements(day).value)
        return placements, services.engine
    finally:
        services.close()


@pytest.mark.slow
def test_a_generation_in_the_engine_process_saves_the_same_schedule(tmp_path) -> None:
    here, _ = _generate(tmp_path, "thread", process=False)
    there, engine = _generate(tmp_path, "process", process=True)
    assert there == here and len(here) == 12
    assert engine.remote_calls >= 1 and engine.local_calls == 0
    assert engine_process._runner is None  # closing the storage uninstalled it
