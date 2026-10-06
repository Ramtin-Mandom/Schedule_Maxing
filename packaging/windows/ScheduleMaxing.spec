# -*- mode: python ; coding: utf-8 -*-
#
# packaging/windows/ScheduleMaxing.spec -- the Windows build of Schedule Maxing.
#
#     pyinstaller packaging/windows/ScheduleMaxing.spec --noconfirm --distpath dist --workpath build
#     -> dist/ScheduleMaxing/ScheduleMaxing.exe   (a folder: "onedir"; the installer hides it from the user)
#
# Normally run through packaging/windows/build_windows.ps1, from an environment made of
# requirements-build.txt only (docs/windows-distribution.md). Every path below comes from this
# file's own location, never from the working directory.
#
# What goes in:   the desktop application (entry point app/desktop.py), its read-only resources
#                 (config/task_preference.yaml, assets/ScheduleMaxing.ico) and its runtime packages.
# What stays out: the server (backend/), the local web service (app/web), the direct PostgreSQL mode
#                 and every database driver, .env files, tests, samples and benchmarks. A distributed
#                 client must never carry database credentials or the code that uses them.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

HERE = Path(SPECPATH).resolve()
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))

from version_info import write_version_file  # noqa: E402 - this folder was just put on the path

NAME = "ScheduleMaxing"
ICON = ROOT / "assets" / "ScheduleMaxing.ico"
VERSION_FILE = write_version_file(ROOT / "build" / "version_info.txt")  # from app/version.py

# Read-only resources, at the same relative paths app/runtime.py resolves from source.
datas = [
    (str(ROOT / "config" / "task_preference.yaml"), "config"),
    (str(ICON), "assets"),
]
# Package data that import analysis cannot see:
datas += collect_data_files("customtkinter")  # theme JSON files, fonts and the default icons
datas += collect_data_files("tzdata")         # the IANA time zone database (Windows ships none)

hiddenimports = [
    # keyring finds its backends through entry points at run time; "Keep me signed in" needs this one.
    "keyring.backends.Windows",
    "win32ctypes.core.ctypes",
]

excludes = [
    # Server, local web service and direct PostgreSQL mode (lazily imported, so named explicitly).
    "backend",
    "app.web",
    "app.persistence.direct",
    "app.persistence.executions",
    "app.persistence.planning",
    "app.persistence.verify_render",
    "sqlalchemy",
    "psycopg",
    "psycopg_binary",
    "psycopg_pool",
    "alembic",
    "fastapi",
    "starlette",
    "uvicorn",
    "httpx",
    "jwt",
    "argon2",
    "dotenv",
    # Development-only code and tools.
    "tests",
    "benchmarks",
    "pytest",
    "_pytest",
    "ruff",
    "app.main",
    "app.productivity.report_cli",
    "app.productivity.predictor_comparison_cli",
    # Optional extras of the scientific stack that the application never uses.
    "matplotlib",
    "IPython",
    "notebook",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "tkinter.test",
]

analysis = Analysis(
    [str(HERE / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,  # onedir: binaries live beside the executable, in _internal
    name=NAME,
    icon=str(ICON),
    version=str(VERSION_FILE),
    console=False,          # a windowed program: no console window (app/desktop.py logs to a file instead)
    upx=False,              # compressed binaries are slower to start and a frequent antivirus false positive
    disable_windowed_traceback=False,
)

COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    upx=False,
    name=NAME,
)
