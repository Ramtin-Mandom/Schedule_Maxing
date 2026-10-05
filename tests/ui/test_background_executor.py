"""The bounded background executor: limits, ordering, superseded reads, failures, stale results and shutdown.

Deterministic: workers are held and released with events, and results are delivered by ticking a stand-in root's
timers -- no sleeps, no real window.
"""
import threading

import pytest

from app.ui import background
from app.ui.background import SATURATED_MESSAGE, WRITE_LANE, ControllerResult, WorkerRegistry, run_in_background


class Root:
    def __init__(self):
        self.pending = []
        self.exists = True

    def after(self, _delay, callback):
        self.pending.append(callback)
        return len(self.pending)

    def winfo_exists(self):
        return self.exists

    def tick(self):
        """Run the timers that are due now (callbacks they schedule wait for the next tick)."""
        due, self.pending = self.pending, []
        for callback in due:
            callback()


class Held:
    """Work that reports when it started and waits until released."""

    def __init__(self, value="done"):
        self.started, self.release, self.value = threading.Event(), threading.Event(), value

    def __call__(self):
        self.started.set()
        assert self.release.wait(10), "the test never released this job"
        return self.value


def idle(registry) -> None:
    with registry._condition:
        assert registry._condition.wait_for(lambda: registry._active == 0, timeout=10)


def deliver(root, registry) -> None:
    idle(registry)
    while registry.outstanding or root.pending:
        root.tick()


def test_jobs_beyond_the_worker_limit_wait_and_submission_never_blocks():
    registry, root, results = WorkerRegistry(max_workers=2), Root(), []
    jobs = [Held(index) for index in range(5)]
    for job in jobs:
        assert run_in_background(root, job, results.append, registry=registry)  # returns with workers still held
    assert jobs[0].started.wait(10) and jobs[1].started.wait(10)
    assert registry.active == 5 and registry._threads == 2 and len(root.pending) == 1  # one poll, not one per job
    assert not any(job.started.is_set() for job in jobs[2:])
    for job in jobs:
        job.release.set()
    deliver(root, registry)
    assert sorted(results) == [0, 1, 2, 3, 4] and registry._threads == 0 and not root.pending


def test_a_full_queue_refuses_the_job_visibly_instead_of_dropping_or_blocking():
    registry, root, results = WorkerRegistry(max_workers=1, max_queued=2), Root(), []
    running, ran = Held("running"), []
    run_in_background(root, running, results.append, registry=registry, serial=WRITE_LANE)
    assert running.started.wait(10)
    for name in ("first", "second", "refused"):
        assert run_in_background(root, lambda name=name: ran.append(name) or name, results.append, registry=registry,
                                 serial=WRITE_LANE)
    root.tick()  # the refusal is delivered while the worker is still held
    assert len(results) == 1 and not results[0].ok and results[0].error == SATURATED_MESSAGE
    running.release.set()
    deliver(root, registry)
    assert results[1:] == ["running", "first", "second"] and ran == ["first", "second"]  # the refused one never ran
    assert registry.active == 0


def test_a_serial_lane_runs_one_job_at_a_time_in_submission_order_while_reads_pass():
    registry, root, order = WorkerRegistry(max_workers=4), Root(), []
    first = Held()
    run_in_background(root, first, lambda _result: None, registry=registry, serial=WRITE_LANE)
    assert first.started.wait(10)
    for index in range(3):
        run_in_background(root, lambda index=index: order.append(index), lambda _result: None, registry=registry,
                          serial=WRITE_LANE)
    read = threading.Event()
    run_in_background(root, read.set, lambda _result: None, registry=registry)
    assert read.wait(10) and order == []  # a read is not held up by the lane; the lane waits for its first job
    first.release.set()
    deliver(root, registry)
    assert order == [0, 1, 2]


def test_a_newer_read_replaces_a_queued_one_and_discards_a_running_ones_result():
    registry, root, results, ran = WorkerRegistry(max_workers=1), Root(), [], []
    running = Held("old")
    run_in_background(root, running, results.append, registry=registry, supersede="load")
    assert running.started.wait(10)
    run_in_background(root, lambda: ran.append("queued") or "queued", results.append, registry=registry,
                      supersede="load")
    run_in_background(root, lambda: ran.append("newest") or "newest", results.append, registry=registry,
                      supersede="load")
    run_in_background(root, lambda: "write", results.append, registry=registry, serial=WRITE_LANE)
    assert registry.active == 3  # the queued read was removed, never to start
    running.release.set()
    deliver(root, registry)
    assert ran == ["newest"] and results == ["newest", "write"]  # a write is never superseded


def test_a_failing_job_delivers_a_failed_result_and_a_failing_callback_does_not_stall_the_rest():
    registry, root, results = WorkerRegistry(max_workers=1), Root(), []

    def broken():
        raise RuntimeError("storage exploded")

    def bad_callback(_result):
        raise ValueError("callback bug")

    run_in_background(root, broken, results.append, registry=registry)
    run_in_background(root, lambda: "second", bad_callback, registry=registry)
    run_in_background(root, lambda: "third", results.append, registry=registry)
    idle(registry)
    with pytest.raises(ValueError):
        root.tick()
    deliver(root, registry)
    assert isinstance(results[0], ControllerResult) and not results[0].ok
    assert "storage exploded" in results[0].error and isinstance(results[0].cause, RuntimeError)
    assert results[1:] == ["third"] and registry.outstanding == 0


def test_stale_and_destroyed_targets_receive_nothing():
    registry, root, gone, results = WorkerRegistry(), Root(), Root(), []
    epoch = {"value": 1}
    registry.result_guard = lambda: (lambda started=epoch["value"]: epoch["value"] == started)
    run_in_background(root, lambda: "workspace one", results.append, registry=registry)
    run_in_background(root, lambda: "explicit guard", results.append, registry=registry, still_current=lambda: False)
    run_in_background(root, lambda: "kept", results.append, registry=registry, still_current=lambda: True)
    run_in_background(gone, lambda: "destroyed", results.append, registry=registry)
    idle(registry)
    epoch["value"] = 2  # the workspace changed while the work ran
    gone.exists = False
    root.tick()
    gone.tick()
    assert results == ["kept"]
    run_in_background(root, lambda: "after", results.append, registry=registry)  # the dead root's state is dropped
    deliver(root, registry)
    assert results == ["kept", "after"] and registry.outstanding == 0 and list(registry._roots) == []


def test_shutdown_refuses_new_work_finishes_writes_drops_queued_reads_and_delivers_nothing():
    registry, root, results, ran = WorkerRegistry(max_workers=1), Root(), [], []
    running = Held()
    run_in_background(root, running, results.append, registry=registry)
    assert running.started.wait(10)
    run_in_background(root, lambda: ran.append("read"), results.append, registry=registry, supersede="status")
    run_in_background(root, lambda: ran.append("write"), results.append, registry=registry, serial=WRITE_LANE)
    assert registry.shutdown(timeout=0) is False  # still working: the caller must not close storage yet
    assert registry.active == 2 and registry.closing
    assert run_in_background(root, lambda: ran.append("late"), results.append, registry=registry) is False
    running.release.set()
    assert registry.shutdown(timeout=10) is True
    root.tick()
    assert ran == ["write"] and results == [] and registry.active == 0


def test_a_thread_that_cannot_start_fails_the_waiting_jobs_and_leaves_no_count_behind():
    def no_threads(**_kwargs):
        raise RuntimeError("can't start new thread")

    registry, root, results = WorkerRegistry(thread_factory=no_threads), Root(), []
    assert run_in_background(root, lambda: "never", results.append, registry=registry, serial=WRITE_LANE)
    assert registry.active == 0 and registry._threads == 0 and not registry._lanes
    root.tick()
    assert len(results) == 1 and not results[0].ok and "could not be started" in results[0].error


def test_one_poll_delivers_for_a_bounded_time_then_yields_to_the_event_loop(monkeypatch):
    registry, root, results = WorkerRegistry(), Root(), []
    clock = {"now": 0.0}
    monkeypatch.setattr(background.time, "perf_counter", lambda: clock["now"])

    def slow_callback(result):
        results.append(result)
        clock["now"] += 0.005  # each callback costs 5 ms; the budget is 8 ms

    for index in range(6):
        run_in_background(root, lambda index=index: index, slow_callback, registry=registry, serial="ordered")
    idle(registry)
    root.tick()
    assert results == [0, 1] and len(root.pending) == 1  # the rest waits for the next turn
    root.tick()
    assert results == [0, 1, 2, 3]
    deliver(root, registry)
    assert results == [0, 1, 2, 3, 4, 5]


def test_external_work_counts_until_it_ends():
    registry = WorkerRegistry()
    assert registry.begin() and registry.active == 1
    assert registry.shutdown(timeout=0) is False and registry.begin() is False
    registry.end()
    assert registry.shutdown(timeout=0) is True
