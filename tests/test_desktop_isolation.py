"""The desktop app runs without any optional web/server component (docs/desktop-web-boundaries.md).

Each test starts a fresh interpreter (tests/isolation_probe.py) in which the web/server
packages -- FastAPI, Starlette, uvicorn, SQLAlchemy, Alembic, psycopg, PyJWT, argon2,
httpx, backend/ and app.web -- cannot be imported and every socket operation fails, with
no backend URL, DATABASE_URL or JWT_SECRET in the environment and the data directory set
to a temporary folder. That proves more than an import check: the desktop imports, a
complete create/edit/delete -> generate -> execute -> export/import -> reopen workflow,
and the real `python -m app.app` entry point all work offline, never import a web module,
never open a listening port or a connection, and leave no background thread behind.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.execution.instance_lock import InstanceLock

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "tests" / "isolation_probe.py"
CLOUD_VARIABLES = ("SCHEDULE_MAXING_BACKEND_URL", "DATABASE_URL", "JWT_SECRET", "TEST_DATABASE_URL")


def run_probe(tmp_path: Path, *args: str, timeout: float = 120) -> dict:
    env = {key: value for key, value in os.environ.items() if key not in CLOUD_VARIABLES}
    env.update(SCHEDULE_MAXING_DATA_DIR=str(tmp_path / "data"), SCHEDULE_MAXING_TIMEZONE="UTC",
               PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
    completed = subprocess.run([sys.executable, str(PROBE), *args], cwd=str(ROOT), env=env, capture_output=True,
                               text=True, timeout=timeout)
    assert completed.returncode == 0, completed.stderr[-4000:]
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["blocked_imports"] == [], "the desktop tried to import a web/server package"
    assert report["network_attempts"] == [], "the desktop tried to listen or connect"
    assert report["loaded_web_modules"] == []
    return report


def test_the_desktop_and_cli_import_no_web_or_server_package(tmp_path: Path) -> None:
    report = run_probe(tmp_path, "imports")
    assert report["threads"] == []


def test_a_complete_desktop_workflow_runs_offline_without_web_components(tmp_path: Path) -> None:
    db_path = tmp_path / "desktop.db"
    report = run_probe(tmp_path, "workflow", str(db_path), str(tmp_path))

    assert report["sync_configured"] is False and report["sync_status"] == "inert"
    assert report["workspace"] == "the ownerless local workspace"
    assert report["rows"] == ["Read (edited)", "Sleep", "Write"]
    assert report["placed"] == 2 and report["second_generation"] == "already_current"
    assert report["csv_preview_created"] == 0 and report["csv_import_created"] == 0
    assert report["closed_cleanly"] and report["reclosed_cleanly"]
    assert report["rows_survived"] and report["placements_survived"] and report["day_status_after_reopen"]
    assert report["execution_status_after_reopen"] == "completed"
    assert report["threads"] == []  # no sync loop, server or worker left running
    assert not (tmp_path / "data").exists()  # the explicit path was used; nothing else was created


def test_python_m_app_app_launches_offline_and_closes_cleanly(tmp_path: Path) -> None:
    report = run_probe(tmp_path, "launch")
    if not report["display"]:
        pytest.skip("no display available for Tk")
    assert report["startup_error"] is None
    assert report["pages"] == ["about", "account", "day", "guide", "month", "productivity", "projects",
                               "settings", "week"]
    assert report["current_page"] == "day" and report["sidebar_open"] is False
    assert report["workspace"] == "the ownerless local workspace"
    assert Path(report["db_path"]) == tmp_path / "data" / "executions.db"  # never the user's real database
    assert not any("sync" in name for name in report["threads_while_running"])  # sync stays inert
    assert report["closed"] is True and report["threads"] == []
    InstanceLock(tmp_path / "data" / "executions.db", "test").acquire().release()  # released when the app closed


# -----------------------------------------------------------------------------
# Static layering: the import direction of docs/desktop-web-boundaries.md
# -----------------------------------------------------------------------------

SHARED_LAYERS = ("app/planning", "app/execution", "app/sync", "app/productivity", "config")
WEB_AND_SERVER = ("backend", "app.web", "fastapi", "starlette", "uvicorn", "sqlalchemy", "alembic", "psycopg", "jwt",
                  "argon2", "httpx")
UI = ("app.ui", "app.app", "tkinter", "customtkinter")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def _violations(folders: tuple[str, ...], forbidden: tuple[str, ...]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for folder in folders:
        for path in sorted((ROOT / folder).rglob("*.py")):
            bad = sorted(name for name in _imports(path)
                         if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden))
            if bad:
                result[str(path.relative_to(ROOT))] = bad
    return result


def test_shared_domain_layers_import_no_ui_web_or_server_code() -> None:
    assert _violations(SHARED_LAYERS, WEB_AND_SERVER + UI) == {}


def test_the_desktop_ui_imports_no_web_or_server_code() -> None:
    assert _violations(("app/ui",), WEB_AND_SERVER) == {}
    assert not [name for name in _imports(ROOT / "app" / "app.py")
                if any(name == p or name.startswith(p + ".") for p in WEB_AND_SERVER)]


def test_web_and_server_code_never_imports_the_desktop_ui() -> None:
    assert _violations(("backend", "app/web"), UI) == {}
