"""The opt-in UI diagnostics: off by default, bounded, and free of callback arguments."""
import gc
import json
import threading

import pytest

from app.ui import background, diagnostics
from app.ui.layout import Coalescer


class Root:
    """Stands in for the Tk root: timers only (no interpreter, so no event counters)."""

    def __init__(self):
        self.pending = {}
        self.next = 0

    def after(self, delay, callback):
        self.next += 1
        self.pending[self.next] = callback
        return self.next

    after_idle = lambda self, callback: self.after(0, callback)  # noqa: E731

    def after_cancel(self, timer):
        self.pending.pop(timer, None)

    def winfo_exists(self):
        return True

    def winfo_width(self):
        return self.width

    def winfo_height(self):
        return 300

    width = 400

    def tick(self):
        timer = next(iter(self.pending))
        self.pending.pop(timer)()


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def recorder():
    root, clock, state = Root(), Clock(), {"modal": False, "jobs": 0}
    installed = diagnostics.UIDiagnostics(root, jobs=lambda: state["jobs"], clock=clock,
                                          move_size=lambda: state["modal"])
    diagnostics._active = installed
    try:
        yield installed, root, clock, state
    finally:
        installed.close()
        assert diagnostics.active() is None


def test_disabled_by_default_and_hooks_are_shared_noops(monkeypatch):
    monkeypatch.delenv(diagnostics.ENV_VAR, raising=False)
    callbacks = list(gc.callbacks)
    assert diagnostics.install_from_env(Root()) is None
    monkeypatch.setenv(diagnostics.ENV_VAR, "off")
    assert diagnostics.install_from_env(Root()) is None
    assert diagnostics.active() is None and gc.callbacks == callbacks
    assert diagnostics.span("anything") is diagnostics.span("else", print)
    with diagnostics.span("anything"):
        diagnostics.count("ignored")
        diagnostics.mark("ignored")


def test_environment_enables_and_close_writes_the_report_once(monkeypatch, tmp_path):
    output = tmp_path / "nested" / "report.json"
    monkeypatch.setenv(diagnostics.ENV_VAR, str(output))
    root = Root()
    installed = diagnostics.install_from_env(root)
    assert diagnostics.active() is installed and len(root.pending) == 1
    with diagnostics.span("app.example"):
        pass
    assert installed.close() is not None and installed.close() is None
    assert root.pending == {} and installed._on_gc not in gc.callbacks
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["phases"]["startup"]["timings"]["app.example"]["n"] == 1
    assert "python" in report["environment"]


def test_late_ticks_are_attributed_to_app_work_workers_or_nothing_measured(recorder):
    installed, root, clock, state = recorder
    diagnostics.mark("scenario")

    def late_tick(seconds, *, work=0.0):
        if work:
            with diagnostics.span("app.work"):
                clock.now += work
        clock.now += seconds - work
        root.tick()

    late_tick(.021)  # on time (1 ms late)
    late_tick(.220, work=.150)
    late_tick(.220)
    state["jobs"] = 2
    late_tick(.220)
    counts = installed.report()["phases"]["scenario"]["counts"]
    assert counts["ticks"] == 4 and counts["late_ticks"] == 3 and counts["max_active_jobs"] == 2
    assert (counts["late.app"], counts["late.unattributed"], counts["late.worker_contention"]) == (1, 1, 1)
    timings = installed.report()["phases"]["scenario"]["timings"]
    assert timings["heartbeat_late"]["max_ms"] == pytest.approx(200, abs=.01)
    assert timings["heartbeat_late.jobs_active"]["n"] == 1
    assert timings["app.work"]["max_ms"] == pytest.approx(150, abs=.01)


def test_native_move_and_resize_loops_are_separate_from_application_delays(recorder):
    installed, root, clock, state = recorder
    state["modal"] = True
    for _ in range(3):  # a move: the size never changes
        clock.now += .520
        root.tick()
    state["modal"] = False
    clock.now += .020
    root.tick()
    state["modal"] = True
    for width in (400, 420, 440):
        root.width = width
        clock.now += .020
        root.tick()
    state["modal"] = False
    clock.now += .020
    root.tick()
    report = installed.report()
    assert [episode["kind"] for episode in report["native_episodes"]] == ["native_move", "native_resize"]
    assert report["native_episodes"][0]["late_ticks"] == 3
    assert report["phases"]["native_move"]["counts"]["late.native_modal"] == 3
    assert report["phases"]["native_resize"]["counts"]["ticks"] == 3
    assert "late_ticks" not in report["phases"]["startup"]["counts"]


def test_nested_spans_count_once_and_gc_is_recorded_with_its_generation(recorder):
    installed, root, clock, _state = recorder
    with diagnostics.span("outer"):
        with diagnostics.span("inner"):
            clock.now += .010
    assert installed._busy == pytest.approx(.010)
    installed._on_gc("start", {"generation": 2})
    clock.now += .030
    installed._on_gc("stop", {"generation": 2, "collected": 7})
    timings = installed.report()["phases"]["startup"]["timings"]
    assert timings["gc.gen2"]["max_ms"] == pytest.approx(30, abs=.01)
    assert installed.report()["slow_events"][-1]["collected"] == 7


def test_report_allows_gc_to_add_a_timing_during_summary(recorder, monkeypatch):
    installed, _root, clock, _state = recorder
    with diagnostics.span("app.example"):
        clock.now += .010
    original = diagnostics._Stat.summary
    collected = False

    def collect_during_summary(stat):
        nonlocal collected
        if not collected:
            collected = True
            # A new phase ensures this adds a key even if GC ran before the report.
            installed.set_phase("report_gc")
            gc.collect(2)
        return original(stat)

    monkeypatch.setattr(diagnostics._Stat, "summary", collect_during_summary)
    report = installed.report()
    assert collected
    assert report["phases"]["startup"]["timings"]["app.example"]["n"] == 1
    assert installed.report()["phases"]["report_gc"]["timings"]["gc.gen2"]["n"] >= 1


def test_logs_stay_bounded_and_record_names_never_arguments(recorder):
    installed, root, clock, _state = recorder

    def private_callback(secret):
        return secret

    for _ in range(1000):
        with diagnostics.span("io.deliver", private_callback):
            clock.now += .050
        clock.now += .300
        root.tick()
    report = installed.report()
    assert len(report["slow_events"]) == 200
    stat = installed._stats["startup", "io.deliver:" + private_callback.__qualname__]
    assert stat.n == 1000 and len(stat.recent) == 256
    assert "secret" not in json.dumps(report).replace("private_callback", "")


def test_application_hooks_report_coalesced_layout_and_worker_runs(recorder):
    installed, root, _clock, _state = recorder

    def relayout():
        pass

    coalescer = Coalescer(root, relayout)
    coalescer.request()
    coalescer.flush()
    registry, delivered = background.WorkerRegistry(), []
    assert background.run_in_background(root, lambda: threading.get_ident(), delivered.append, registry=registry)
    assert registry.shutdown(timeout=5)
    names = set(installed.report()["phases"]["startup"]["timings"])
    assert "coalesced:" + relayout.__qualname__ in names
    assert any(name.startswith("worker:") for name in names)
    assert installed._busy == 0 or installed._depth == 0  # the worker's time is not Tk-thread work
