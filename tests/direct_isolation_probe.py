"""
A child-process probe for tests/direct/test_direct_isolation.py (not collected by pytest).

    python tests/direct_isolation_probe.py workflow DB_PATH PROJECT_ROOT
    python tests/direct_isolation_probe.py launch DB_PATH

Direct PostgreSQL mode must need no HTTP stack: before the application is
imported, the HTTP framework and server packages (FastAPI, Starlette,
uvicorn, HTTPX), PyJWT, the local web service (app.web) and the backend's
HTTP modules (backend.api, backend.app, backend.sync, backend.security,
backend.browser_sessions, backend.planning_api, backend.http_errors) are
made unimportable, and every socket operation fails. The database is a
migrated SQLite file injected as the DirectBackend's engine (the schema is
the same; PostgreSQL itself is covered by tests/direct on a disposable
server), so the probe needs no network. The last line printed is a JSON
report.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BLOCKED_PACKAGES = {"fastapi", "starlette", "uvicorn", "httpx", "jwt"}
BLOCKED_MODULES = {"app.web", "backend.api", "backend.app", "backend.sync", "backend.security",
                   "backend.browser_sessions", "backend.planning_api", "backend.http_errors"}
PASSWORD = "correct horse battery"
blocked_imports: list[str] = []
network_attempts: list[str] = []


def _blocked(name: str) -> bool:
    return name.split(".")[0] in BLOCKED_PACKAGES or any(name == m or name.startswith(m + ".") for m in BLOCKED_MODULES)


class _Block:
    def find_spec(self, name, path=None, target=None):
        if _blocked(name):
            blocked_imports.append(name)
            raise ModuleNotFoundError(f"{name} is not available in the direct-mode probe", name=name)
        return None


def _refuse(operation: str):
    def refuse(self, *args, **kwargs):
        network_attempts.append(operation)
        raise OSError(f"network use is not allowed in the direct-mode probe ({operation})")

    return refuse


sys.meta_path.insert(0, _Block())
for _operation in ("bind", "listen", "connect", "connect_ex", "sendto"):
    setattr(socket.socket, _operation, _refuse(_operation))


def _report(**fields) -> None:
    fields.update(blocked_imports=blocked_imports, network_attempts=network_attempts,
                  loaded=sorted(name for name in sys.modules if _blocked(name)),
                  threads=sorted(t.name for t in threading.enumerate() if t is not threading.main_thread()))
    print(json.dumps(fields, default=str))


def _backend(db_path: str):
    from app.persistence.direct import DirectBackend
    from backend.database import create_backend_engine
    from backend.migrate import upgrade

    engine = create_backend_engine(f"sqlite:///{db_path}")
    upgrade(engine)
    return DirectBackend(engine)


def probe_workflow(db_path: str, project_root: str) -> None:
    from datetime import date

    from app.ui.background import WorkerRegistry
    from app.ui.direct_services import DirectAccountController, open_direct_app_services
    from app.ui.schedule_page_controller import SchedulePageController

    mon = date(2026, 3, 2)
    form = {"day": "1", "category": "study", "tag": "t", "fixed": "False", "start_time": "480", "end_time": "1200",
            "duration": "60", "priority": "5", "name": "Read"}
    facts: dict = {}
    services = open_direct_app_services(backend=_backend(db_path), timezone="UTC", project_root=project_root,
                                        registry=WorkerRegistry())
    account = DirectAccountController(services)
    facts["registered"] = account.register("probe@example.com", PASSWORD).ok
    facts["signed_in"] = account.sign_in("probe@example.com", PASSWORD).ok
    services.switch_workspace()
    page = SchedulePageController(services.planning_controller, number_of_days=1, anchor_date=mon, timezone="UTC")
    facts["created"] = page.submit_task_form(form).ok
    generated = services.planning_controller.generate(mon, mon)
    facts["generated"] = generated.value.status if generated.ok else generated.error
    placement = services.planning_controller.get_placements(mon).value[0]
    task = services.planning_controller.get_task(placement.task_id).value
    execution = services.execution_controller.get_or_create_canonical_execution(task, placement).value
    facts["started"] = services.execution_controller.start(execution.id).ok
    facts["closed"] = services.close()
    _report(ok=True, **facts)


def probe_launch(db_path: str) -> None:
    """The real desktop window in direct mode (injected backend), closed after it started."""
    import tkinter as tk

    from tests import window_placement

    window_placement.install()  # on the left monitor, like the in-process widget tests
    try:
        tk.Tk().destroy()
    except tk.TclError:
        _report(ok=True, display=False)
        return

    import customtkinter

    from app.app import ScheduleOptimizerApp

    seen: dict = {}
    original = customtkinter.CTk.mainloop

    def mainloop(self, *args, **kwargs):
        seen["startup_error"] = self.startup_error
        seen["current_page"] = self.shell.current if self.shell else None
        seen["storage_mode"] = self.services.storage_mode if self.services else None
        seen["sync_button_shown"] = bool(self.shell.status_bar.sync_button.winfo_manager()) if self.shell else None
        seen["footer"] = self.services.location_label() if self.services else None
        self.after(1500, self._on_close)
        return original(self, *args, **kwargs)

    customtkinter.CTk.mainloop = mainloop
    app = ScheduleOptimizerApp(storage="postgres", direct_backend=_backend(db_path),
                               ui_settings_path=str(Path(db_path).with_suffix(".ui.json")))
    app.mainloop()
    _report(ok=True, display=True, closed=app.services.closed if app.services else None, **seen)


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "workflow":
        probe_workflow(sys.argv[2], sys.argv[3])
    elif mode == "launch":
        probe_launch(sys.argv[2])
    else:
        raise SystemExit(f"unknown probe mode {mode!r}")
