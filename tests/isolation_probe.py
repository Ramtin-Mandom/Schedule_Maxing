"""
A child-process probe for tests/test_desktop_isolation.py (not collected by pytest).

    python tests/isolation_probe.py imports
    python tests/isolation_probe.py workflow DB_PATH PROJECT_ROOT
    python tests/isolation_probe.py launch

Before anything of the application is imported, it makes every optional
web/server package unimportable (FastAPI, Starlette, uvicorn, SQLAlchemy,
Alembic, psycopg, PyJWT, argon2, httpx, backend/, app.web) and every socket
operation fail -- no listening port, no outgoing connection, no internet --
recording each attempt. The last line printed is a JSON report; the parent
test asserts on it. The parent runs it with the backend/cloud environment
variables removed and SCHEDULE_MAXING_DATA_DIR pointing at a temporary
directory, so the user's real database is never touched.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BLOCKED_PACKAGES = {"fastapi", "starlette", "uvicorn", "sqlalchemy", "alembic", "psycopg", "psycopg2", "jwt", "argon2",
                    "httpx", "backend"}
blocked_imports: list[str] = []
network_attempts: list[str] = []


class _BlockWebPackages:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED_PACKAGES or name == "app.web" or name.startswith("app.web."):
            blocked_imports.append(name)
            raise ModuleNotFoundError(f"{name} is not available in a desktop-only runtime", name=name)
        return None


def _refuse(operation: str):
    def refuse(self, *args, **kwargs):
        network_attempts.append(f"{operation}{args!r}")
        raise OSError(f"network use is not allowed in the desktop isolation probe ({operation})")

    return refuse


sys.meta_path.insert(0, _BlockWebPackages())
for _operation in ("bind", "listen", "connect", "connect_ex", "sendto"):
    setattr(socket.socket, _operation, _refuse(_operation))


def _report(**fields) -> None:
    loaded = sorted(name for name in sys.modules
                    if name.split(".")[0] in BLOCKED_PACKAGES or name == "app.web" or name.startswith("app.web."))
    fields.update(blocked_imports=blocked_imports, network_attempts=network_attempts, loaded_web_modules=loaded,
                  threads=sorted(thread.name for thread in threading.enumerate() if thread is not threading.main_thread()))
    print(json.dumps(fields, default=str))


def probe_imports() -> None:
    import app.app  # noqa: F401 - the desktop entry point
    import app.main  # noqa: F401 - the CLI
    import app.ui.app_services  # noqa: F401

    _report(ok=True)


def probe_workflow(db_path: str, project_root: str) -> None:
    from datetime import date

    from app.planning.csv_import import ImportMode
    from app.planning.service import DayResultStatus
    from app.ui.app_services import open_app_services
    from app.ui.schedule_page_controller import SchedulePageController

    mon = date(2024, 6, 3)
    form = {"day": "1", "category": "study", "tag": "t", "fixed": "False", "start_time": "480", "end_time": "1200",
            "duration": "60", "priority": "5"}
    facts: dict = {}

    services = open_app_services(db_path, timezone="UTC", project_root=project_root)
    facts["sync_configured"] = services.sync_service.configured
    facts["sync_status"] = services.sync_service.sync_now().status
    facts["workspace"] = services.workspace.scope.describe()
    page = SchedulePageController(services.planning_controller, number_of_days=1, anchor_date=mon, timezone="UTC")

    # create, edit and delete (tasks and a fixed block) through the desktop's own form boundary
    for name in ("Read", "Write", "Scratch"):
        assert page.submit_task_form({**form, "name": name}).ok
    assert page.submit_task_form({**form, "name": "Sleep", "fixed": "True", "category": "sleep",
                                  "start_time": "0", "end_time": "420"}).ok
    rows = {row.name: row for row in page.load().value.rows}
    state = page.form_state_for(rows["Read"].ref).value
    assert page.submit_task_form({**state.values, "name": "Read (edited)"}, editing=state.ref).ok
    assert page.delete(rows["Scratch"].ref).ok

    # generate (legacy Make Schedule), then the shared workflow's no-op
    run = page.make_schedule()
    assert run.ok, run.error
    facts["placed"] = run.value.placed_count
    facts["second_generation"] = services.planning_controller.generate(mon, mon).value.status

    # execute one placement
    target = run.value.snapshot.executables[0]
    executions = services.execution_controller
    execution = executions.get_or_create_canonical_execution(target.task, target.placement).value
    for action in ("start", "pause", "resume", "complete"):
        assert getattr(executions, action)(execution.id).ok, action

    # export, preview and import the canonical CSV (identity-preserving: nothing changes)
    exported = Path(db_path).with_name("export.csv")
    assert services.planning_controller.export_planning_csv(str(exported)).ok
    preview = services.planning_controller.preview_csv_file(str(exported)).value
    imported = services.planning_controller.import_csv_file(str(exported), anchor_date=mon, mode=ImportMode.APPEND)
    assert imported.ok, imported.error
    facts["csv_preview_created"] = sum(preview.created.values())
    facts["csv_import_created"] = sum(imported.value.created.values())
    before = {row.name for row in page.load().value.rows}
    placement_ids = sorted(str(p.id) for p in services.planning_controller.get_placements(mon).value)
    facts["closed_cleanly"] = services.close()

    # reopen: everything persisted, the day is still current, the execution kept its history
    services = open_app_services(db_path, timezone="UTC", project_root=project_root)
    page = SchedulePageController(services.planning_controller, number_of_days=1, anchor_date=mon, timezone="UTC")
    facts["rows_survived"] = {row.name for row in page.load().value.rows} == before
    facts["rows"] = sorted(before)
    facts["placements_survived"] = sorted(
        str(p.id) for p in services.planning_controller.get_placements(mon).value) == placement_ids
    facts["day_status_after_reopen"] = services.planning_controller.day_state(mon).value.status == DayResultStatus.GENERATED
    restored = services.execution_controller.find_execution_for_placement(target.placement.id).value
    facts["execution_status_after_reopen"] = restored.status.value
    facts["reclosed_cleanly"] = services.close()
    _report(ok=True, **facts)


def probe_launch() -> None:
    """The real entry point (python -m app.app's main()) with a temporary data directory; closes itself."""
    import runpy
    import tkinter as tk

    from tests import window_placement

    window_placement.install()  # on the left monitor, like the in-process widget tests
    try:
        tk.Tk().destroy()
    except tk.TclError:
        _report(ok=True, display=False)
        return

    import customtkinter

    seen: dict = {}
    original = customtkinter.CTk.mainloop

    def mainloop(self, *args, **kwargs):
        seen["startup_error"] = self.startup_error
        seen["pages"] = sorted(self.pages)
        seen["current_page"] = self.shell.current if self.shell else None
        seen["sidebar_open"] = self.shell_state.sidebar_open
        seen["workspace"] = self.services.workspace.scope.describe() if self.services else None
        seen["db_path"] = str(self.services.db_path) if self.services else None
        seen["threads_while_running"] = sorted(t.name for t in threading.enumerate() if t is not threading.main_thread())
        self.after(1500, self._on_close)
        seen["app"] = self
        return original(self, *args, **kwargs)

    customtkinter.CTk.mainloop = mainloop
    sys.argv = ["app.app"]  # the entry point's own arguments: none (local storage, the default)
    runpy.run_module("app.app", run_name="__main__")
    app = seen.pop("app")
    _report(ok=True, display=True, closed=app.services.closed if app.services else None, **seen)


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "imports":
        probe_imports()
    elif mode == "workflow":
        probe_workflow(sys.argv[2], sys.argv[3])
    elif mode == "launch":
        probe_launch()
    else:
        raise SystemExit(f"unknown probe mode {mode!r}")
