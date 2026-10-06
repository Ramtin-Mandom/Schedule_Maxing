"""
app/selftest.py

The packaged application's self-test: `ScheduleMaxing.exe --self-test REPORT.json`
(app/desktop.py), run by packaging/windows/smoke_test.py after every build
and by the release workflow against the installed program. It answers, from
inside the real executable, the questions a successful PyInstaller build
does not: are the bundled resources there, do the compiled dependencies
load, is the database created in the data directory, does the scheduler
work, does saved data survive a restart, and does the window open.

It is not a feature: nothing in the interface starts it, and it refuses to
run unless SCHEDULE_MAXING_DATA_DIR names the folder to use, so it can never
touch a person's real data. Synchronization is not exercised (run it with
SCHEDULE_MAXING_BACKEND_URL=off); the run makes no network request.

The report is JSON: {"ok": bool, "checks": {name: {"ok": bool, ...}}}. A
second run against the same folder sees the first run's records
("restart": the data survived).
"""

from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Callable
from datetime import date
from pathlib import Path

from app import runtime
from app.version import __version__

DAY = date(2024, 6, 3)
TASK_NAMES = ("Self-test read", "Self-test write")
EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2


def _check_resources() -> dict:
    from app.reward import load_reward_settings

    template = runtime.resource_path("config", "task_preference.yaml")
    assert template.is_file(), f"missing bundled resource: {template}"
    load_reward_settings()
    return {"resource_root": str(runtime.resource_root()), "template": str(template)}


def _check_dependencies() -> dict:
    """The packages whose data files or compiled parts a build can silently leave out."""
    from zoneinfo import ZoneInfo

    import customtkinter
    import joblib
    import pandas
    import pydantic
    import sklearn
    import yaml
    from sklearn.linear_model import Ridge

    theme = Path(customtkinter.__file__).parent / "assets" / "themes" / "blue.json"
    assert theme.is_file(), f"missing CustomTkinter theme: {theme}"
    assert ZoneInfo("America/Toronto").key == "America/Toronto"  # tzdata: Windows has no IANA database of its own
    frame = pandas.DataFrame({"x": [0.0, 1.0, 2.0, 3.0], "y": [1.0, 3.0, 5.0, 7.0]})
    model = Ridge(alpha=0.0).fit(frame[["x"]], frame["y"])
    assert abs(float(model.predict(pandas.DataFrame({"x": [4.0]}))[0]) - 9.0) < 1e-6
    return {"customtkinter": customtkinter.__version__, "pandas": pandas.__version__, "sklearn": sklearn.__version__,
            "joblib": joblib.__version__, "pydantic": pydantic.VERSION, "yaml": yaml.__version__}


def _check_credential_store() -> dict:
    """"Keep me signed in" needs keyring's Windows backend; without it the option is just not offered."""
    from app.sync import credentials

    vault = credentials.system_vault()
    if sys.platform.startswith("win"):
        assert vault is not None, "the Windows credential store backend is not available"
    return {"available": vault is not None}


def _check_storage_and_scheduling() -> dict:
    from app.execution.db import LATEST_SCHEMA_VERSION, resolve_db_path
    from app.planning.csv_import import ImportMode
    from app.planning.preferences import OptimizerMode
    from app.ui.app_services import open_app_services
    from app.ui.schedule_page_controller import SchedulePageController

    db_path = resolve_db_path()
    existed = db_path.exists()
    services = open_app_services(timezone="UTC", background_sync=False)
    try:
        assert services.db_path == db_path and db_path.is_file(), f"the database was not created at {db_path}"
        assert db_path.parent == runtime.data_dir(), "the database is outside the data directory"
        version = services.connection.execute("PRAGMA user_version").fetchone()[0]
        assert version == LATEST_SCHEMA_VERSION, f"schema v{version}, expected v{LATEST_SCHEMA_VERSION}"

        planning = services.planning_controller
        page = SchedulePageController(planning, number_of_days=1, anchor_date=DAY, timezone="UTC")
        names = {row.name for row in page.load().value.rows}
        restart = existed and set(TASK_NAMES) <= names
        if not restart:
            form = {"day": "1", "category": "study", "tag": "selftest", "fixed": "False", "start_time": "480",
                    "end_time": "1200", "duration": "60", "priority": "5"}
            for name in TASK_NAMES:
                created = page.submit_task_form({**form, "name": name})
                assert created.ok, created.error

        modes = {}
        for mode in OptimizerMode:
            saved = planning.set_engine_mode(mode)
            assert saved.ok, f"{mode.value}: {saved.error}"
            run = page.make_schedule()
            assert run.ok, f"{mode.value}: {run.error}"
            assert run.value.placed_count == len(TASK_NAMES), f"{mode.value}: placed {run.value.placed_count}"
            modes[mode.value] = run.value.placed_count
        assert planning.set_engine_mode(OptimizerMode.PRECISE_GREEDY).ok

        target = page.make_schedule().value.snapshot.executables[0]
        executions = services.execution_controller
        execution = executions.get_or_create_canonical_execution(target.task, target.placement).value
        if execution.status.value == "scheduled":
            for action in ("start", "pause", "resume", "complete"):
                result = getattr(executions, action)(execution.id)
                assert result.ok, f"{action}: {result.error}"

        exported = runtime.data_dir() / "selftest-export.csv"
        assert planning.export_planning_csv(str(exported)).ok and exported.stat().st_size > 0
        imported = planning.import_csv_file(str(exported), anchor_date=DAY, mode=ImportMode.APPEND)
        assert imported.ok, imported.error
        rows = sorted(row.name for row in page.load().value.rows)
        assert rows == sorted(TASK_NAMES), f"unexpected tasks after the CSV round trip: {rows}"
        return {"database": str(db_path), "schema_version": version, "restart": restart, "modes": modes,
                "csv_created_on_reimport": sum(imported.value.created.values())}
    finally:
        assert services.close(), "the database did not close cleanly"


def _check_window() -> dict:
    """The real window, never shown: every page is built and visited, then it closes through the normal path."""
    import tkinter as tk

    from app.app import ScheduleOptimizerApp

    window = ScheduleOptimizerApp(storage="local", background_sync=False)
    try:
        window.withdraw()
        window.update()
        assert window.startup_error is None, window.startup_error
        pages = sorted(window.pages)
        for name in pages:
            window.show_page(name)
            window.update()
        icon = runtime.resource_path("assets", "ScheduleMaxing.ico")
        return {"pages": pages, "icon_bundled": icon.is_file()}
    finally:
        window._on_close()
        for _ in range(600):
            if window._closed:
                break
            try:
                window.update()
                window.after(50)
            except tk.TclError:
                break


CHECKS: tuple[tuple[str, Callable[[], dict]], ...] = (
    ("resources", _check_resources),
    ("dependencies", _check_dependencies),
    ("credential_store", _check_credential_store),
    ("storage_and_scheduling", _check_storage_and_scheduling),
    ("window", _check_window),
)


def run(report_path: str | Path, *, checks: tuple[tuple[str, Callable[[], dict]], ...] = CHECKS) -> int:
    """Run every check, write the JSON report, and return the process exit code."""
    from config import settings

    report: dict = {"version": __version__, "frozen": runtime.is_frozen(), "executable": sys.executable,
                    "data_dir": str(runtime.data_dir()), "checks": {}}
    if not settings.DATA_DIR_OVERRIDDEN:
        report.update(ok=False, error=f"refused: set {settings.DATA_DIR_ENV_VAR} to a scratch folder first")
        _write(report_path, report)
        return EXIT_REFUSED
    for name, check in checks:
        try:
            report["checks"][name] = {"ok": True, **check()}
        except BaseException as error:  # noqa: BLE001 - every failure belongs in the report, not on a console
            report["checks"][name] = {"ok": False, "error": f"{type(error).__name__}: {error}",
                                      "traceback": traceback.format_exc()}
    report["ok"] = all(entry["ok"] for entry in report["checks"].values())
    _write(report_path, report)
    return EXIT_OK if report["ok"] else EXIT_FAILED


def _write(report_path: str | Path, report: dict) -> None:
    path = Path(report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
