"""app/runtime.py and app/version.py: source vs. packaged locations, and the single version source."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from app import runtime, version
from app.reward import load_reward_settings
from config import settings

ROOT = Path(__file__).resolve().parent.parent


def frozen(bundle: Path) -> SimpleNamespace:
    return SimpleNamespace(frozen=True, _MEIPASS=str(bundle))


def test_source_resources_are_the_repository_root():
    assert not runtime.is_frozen()
    assert runtime.resource_root() == ROOT
    assert runtime.resource_path("config", "task_preference.yaml").is_file()


def test_packaged_resources_are_the_bundle_directory(tmp_path):
    system = frozen(tmp_path)
    assert runtime.is_frozen(system)
    assert runtime.resource_root(system) == tmp_path
    assert runtime.resource_path("assets", "icon.ico", system=system) == tmp_path / "assets" / "icon.ico"


def test_frozen_needs_both_markers():
    assert not runtime.is_frozen(SimpleNamespace(frozen=True))
    assert not runtime.is_frozen(SimpleNamespace(_MEIPASS="x"))


def test_writable_directories_follow_the_data_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "elsewhere")
    assert runtime.logs_dir() == tmp_path / "elsewhere" / "logs"
    assert runtime.backups_dir() == tmp_path / "elsewhere" / "backups"
    assert runtime.updates_dir() == tmp_path / "elsewhere" / "updates"
    assert not (tmp_path / "elsewhere").exists()  # paths only: nothing is created


def test_missing_standard_streams_are_replaced_and_real_ones_kept():
    real = object()
    system = SimpleNamespace(stdout=None, stderr=real)
    runtime.ensure_standard_streams(system)
    assert system.stderr is real
    system.stdout.write("discarded")
    system.stdout.flush()
    system.stdout.close()


def test_importing_runtime_touches_no_directory(tmp_path):
    code = "import app.runtime, app.version, os, sys; sys.exit(1 if os.listdir(sys.argv[1]) else 0)"
    env = {"SCHEDULE_MAXING_DATA_DIR": str(tmp_path), "PYTHONPATH": str(ROOT), "SYSTEMROOT": "C:\\Windows"}
    assert subprocess.run([sys.executable, "-c", code, str(tmp_path)], cwd=str(ROOT), env=env).returncode == 0


def test_reward_template_is_read_from_the_bundle(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "task_preference.yaml").write_text("weights:\n  importance: 42.5\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert load_reward_settings().weight_importance == 42.5


def test_reward_defaults_apply_when_the_bundle_has_no_template(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert load_reward_settings().weight_importance == 5.0


def test_version_is_major_minor_patch():
    assert re.fullmatch(r"\d+\.\d+\.\d+", version.__version__)
    assert version.APP_NAME == "Schedule Maxing" and version.APP_ID == "ScheduleMaxing"


def test_version_is_written_in_one_place():
    """No second hard-coded copy in the desktop application, packaging or workflow sources
    (app/web is the separate local web service: its FastAPI `version` is that API's own)."""
    literal = re.compile(r"""["']?""" + re.escape(version.__version__) + r"""["']?""")
    sources = [path for folder in ("app", "config", "packaging", ".github") for path in (ROOT / folder).rglob("*")
               if path.is_file() and path.suffix in {".py", ".spec", ".iss", ".ps1", ".yml", ".yaml", ".toml"}]
    sources = [path for path in sources if "__pycache__" not in path.parts and "web" not in path.parts]
    copies = [path.relative_to(ROOT).as_posix() for path in sources
              if literal.search(path.read_text(encoding="utf-8", errors="replace"))]
    assert copies == ["app/version.py"]
