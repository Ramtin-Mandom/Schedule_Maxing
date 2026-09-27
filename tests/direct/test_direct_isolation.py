"""Direct PostgreSQL mode needs no HTTP stack (tests/direct_isolation_probe.py):
with FastAPI, Starlette, uvicorn, HTTPX, PyJWT, the local web service and the
backend's HTTP modules unimportable and every socket refused, the desktop's
direct-mode services and its real window work. Also: storage selection is
explicit -- DATABASE_URL alone keeps the desktop on local storage."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from config import settings

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "tests" / "direct_isolation_probe.py"


def run_probe(tmp_path: Path, *args: str) -> dict:
    env = {key: value for key, value in os.environ.items()
           if key not in ("DATABASE_URL", "JWT_SECRET", "TEST_DATABASE_URL", "SCHEDULE_MAXING_BACKEND_URL",
                          "SCHEDULE_MAXING_STORAGE", "SCHEDULE_MAXING_ENV_FILE")}
    env.update(SCHEDULE_MAXING_DATA_DIR=str(tmp_path / "data"), SCHEDULE_MAXING_TIMEZONE="UTC",
               PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
    completed = subprocess.run([sys.executable, str(PROBE), *args], cwd=str(ROOT), env=env, capture_output=True,
                               text=True, timeout=180)
    assert completed.returncode == 0, completed.stderr[-4000:]
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["blocked_imports"] == [], "direct mode tried to import an HTTP/JWT/local-web module"
    assert report["network_attempts"] == [] and report["loaded"] == []
    return report


def test_direct_mode_services_need_no_http_stack_or_network(tmp_path: Path) -> None:
    report = run_probe(tmp_path, "workflow", (tmp_path / "direct.db").as_posix(), str(ROOT))
    assert report["registered"] and report["signed_in"] and report["created"] and report["started"]
    assert report["generated"] == "generated" and report["closed"] is True
    assert report["threads"] == []
    assert not (tmp_path / "data").exists()  # no local SQLite database was created as a fallback


def test_the_direct_mode_window_starts_signed_out_without_sync_and_closes(tmp_path: Path) -> None:
    report = run_probe(tmp_path, "launch", (tmp_path / "direct.db").as_posix())
    if not report["display"]:
        pytest.skip("no display available for Tk")
    assert report["startup_error"] is None and report["storage_mode"] == "postgres"
    assert report["current_page"] == "account" and report["sync_button_shown"] is False
    assert "PostgreSQL" in report["footer"] and "executions.db" not in report["footer"]
    assert report["closed"] is True and report["threads"] == []
    assert not (tmp_path / "data" / settings.EXECUTION_DB_FILENAME).exists()


def test_database_url_alone_never_selects_direct_storage() -> None:
    assert settings.resolve_storage_mode({"DATABASE_URL": "postgresql://u:p@db.example.com/app"}) == "local"
    assert settings.resolve_storage_mode({"SCHEDULE_MAXING_STORAGE": "Postgres"}) == "postgres"
    with pytest.raises(ValueError):
        settings.resolve_storage_mode({"SCHEDULE_MAXING_STORAGE": "cloud"})


def test_the_entry_point_refuses_an_env_file_for_local_storage(tmp_path: Path) -> None:
    env = {key: value for key, value in os.environ.items() if key not in ("SCHEDULE_MAXING_STORAGE",)}
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run([sys.executable, "-m", "app.app", "--env-file", str(tmp_path / "x.env")], cwd=ROOT,
                            env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 2 and "only used with --storage postgres" in result.stderr
