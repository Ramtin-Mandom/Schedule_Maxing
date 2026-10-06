"""
packaging/windows/check_bundle.py

Checks a built application folder (dist/ScheduleMaxing) before it is tested
or put in an installer: nothing that must stay private or server-side is in
it, and everything the application needs at run time is.

    python packaging/windows/check_bundle.py dist/ScheduleMaxing [--pyz build/ScheduleMaxing/PYZ-00.pyz]

With --pyz (the archive of pure-Python modules PyInstaller builds) the module
list is checked too; that needs PyInstaller, so use the build environment.
Exit code 0 when everything holds; otherwise 1 with one line per problem.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

EXECUTABLE = "ScheduleMaxing.exe"

#: Top-level packages/modules that must not be distributed (the server, its drivers, the test tools).
FORBIDDEN_MODULES = ("backend", "sqlalchemy", "psycopg", "psycopg_binary", "alembic", "fastapi", "starlette", "uvicorn",
                     "httpx", "jwt", "argon2", "dotenv", "pytest", "_pytest", "tests", "benchmarks")
#: Application modules that must not be distributed.
FORBIDDEN_APP_MODULES = ("app.web", "app.persistence.direct", "app.persistence.executions", "app.persistence.planning",
                         "app.persistence.verify_render")
#: Text that would mean a secret or a private configuration file was packaged.
SECRET_MARKERS = (b"DATABASE_URL=", b"JWT_SECRET=", b"BEGIN PRIVATE KEY", b"BEGIN RSA PRIVATE KEY")
REQUIRED = (
    EXECUTABLE,
    "_internal/config/task_preference.yaml",
    "_internal/assets/ScheduleMaxing.ico",
    "_internal/customtkinter/assets/themes/blue.json",
    "_internal/tzdata/zoneinfo/America/Toronto",
)
#: Files larger than this are compiled binaries, not configuration; they are not scanned for secret text.
SCAN_LIMIT = 4_000_000


def check_folder(bundle: Path) -> list[str]:
    problems = [f"missing: {name}" for name in REQUIRED if not (bundle / name).is_file()]
    for path in sorted(bundle.rglob("*")):
        relative = path.relative_to(bundle).as_posix()
        name = path.name.lower()
        if name == ".env" or name.startswith(".env.") or name.endswith(".env"):
            problems.append(f"environment file packaged: {relative}")
        parts = path.relative_to(bundle).parts
        # A package or module directly in the import folder (deeper folders, e.g. a library's own tests/, are its own).
        if len(parts) == 2 and parts[0] == "_internal" and parts[1].lower().split(".")[0] in FORBIDDEN_MODULES:
            problems.append(f"forbidden package packaged: {relative}")
        if path.is_file() and path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".log", ".pem", ".pfx", ".key"}:
            problems.append(f"data, log or key file packaged: {relative}")
        if path.is_file() and path.stat().st_size <= SCAN_LIMIT:
            data = path.read_bytes()
            problems.extend(f"secret-looking text ({marker.decode()}) in {relative}" for marker in SECRET_MARKERS
                            if marker in data)
    return problems


def check_modules(pyz: Path) -> list[str]:
    from PyInstaller.archive.readers import ZlibArchiveReader

    names = set(ZlibArchiveReader(str(pyz)).toc)
    problems = []
    for name in sorted(names):
        root = name.split(".")[0]
        if root in FORBIDDEN_MODULES or any(name == bad or name.startswith(bad + ".") for bad in FORBIDDEN_APP_MODULES):
            problems.append(f"forbidden module packaged: {name}")
    for needed in ("app.desktop", "app.app", "app.selftest", "app.execution.db", "customtkinter", "sklearn", "pandas",
                   "keyring.backends.Windows"):
        if needed not in names:
            problems.append(f"missing module: {needed}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a built Schedule Maxing application folder.")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--pyz", type=Path, default=None, help="PyInstaller's PYZ archive, to check the module list too")
    args = parser.parse_args(argv)
    if not args.bundle.is_dir():
        print(f"not a folder: {args.bundle}")
        return 1
    problems = check_folder(args.bundle)
    if args.pyz is not None:
        problems += check_modules(args.pyz)
    for problem in problems:
        print(f"PROBLEM  {problem}")
    files = [path for path in args.bundle.rglob("*") if path.is_file()]
    size = sum(path.stat().st_size for path in files) / 1_000_000
    print(f"{args.bundle}: {len(files)} files, {size:.0f} MB, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
