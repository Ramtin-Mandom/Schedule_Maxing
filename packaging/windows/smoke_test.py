"""
packaging/windows/smoke_test.py

Runs the packaged application's own self-test (app/selftest.py) and fails
loudly if anything in it does:

    python packaging/windows/smoke_test.py dist/ScheduleMaxing/ScheduleMaxing.exe [--skip-copy]

1. a first run in an empty scratch data folder (first launch: the database is
   created there, the scheduler runs in every mode, the window opens);
2. a second run against the same folder (restart: the saved data is still there);
3. unless --skip-copy: a run of a copy placed in a folder whose name contains
   spaces (as under "C:\\Program Files"), with its own empty data folder;
4. the application folder itself is unchanged by all of this (nothing is ever
   written beside the executable).

The executable runs with no network access configured and with its data
folder set explicitly, so the real per-user data is never touched. Needs
only the standard library.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TIMEOUT_SECONDS = 300


def snapshot(folder: Path) -> dict[str, tuple[int, str]]:
    """Every file under `folder` with its size and digest."""
    result = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            result[path.relative_to(folder).as_posix()] = (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
    return result


def run_self_test(executable: Path, data_dir: Path, report: Path) -> tuple[dict, float]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("SCHEDULE_MAXING_") and key not in ("DATABASE_URL", "JWT_SECRET", "PYTHONPATH", "PYTHONHOME")}
    env.update(SCHEDULE_MAXING_DATA_DIR=str(data_dir), SCHEDULE_MAXING_BACKEND_URL="off", SCHEDULE_MAXING_TIMEZONE="UTC",
               SCHEDULE_MAXING_UPDATE_CHECK="off")
    started = time.monotonic()
    completed = subprocess.run([str(executable), "--self-test", str(report)], env=env, cwd=str(data_dir.parent),
                               timeout=TIMEOUT_SECONDS)
    elapsed = time.monotonic() - started
    if not report.is_file():
        raise SystemExit(f"FAILED: {executable} exited with {completed.returncode} and wrote no report "
                         f"(log, if any: {data_dir / 'logs'})")
    result = json.loads(report.read_text(encoding="utf-8"))
    result["exit_code"] = completed.returncode
    return result, elapsed


def describe(label: str, result: dict, elapsed: float) -> list[str]:
    failures = []
    print(f"-- {label}: {'ok' if result.get('ok') else 'FAILED'} in {elapsed:.1f} s "
          f"(version {result.get('version')}, packaged={result.get('frozen')})")
    if result.get("error"):
        failures.append(f"{label}: {result['error']}")
    for name, check in result.get("checks", {}).items():
        print(f"   {name:<24} {'ok' if check['ok'] else 'FAILED'}")
        if not check["ok"]:
            failures.append(f"{label}/{name}: {check.get('error')}\n{check.get('traceback', '')}")
    if result.get("exit_code") != 0:
        failures.append(f"{label}: exit code {result.get('exit_code')}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a packaged Schedule Maxing executable.")
    parser.add_argument("executable", type=Path)
    parser.add_argument("--skip-copy", action="store_true", help="skip the run from a folder with spaces in its name")
    parser.add_argument("--expect-source", action="store_true",
                        help="the executable is a development launcher, not a packaged build (tests only)")
    args = parser.parse_args(argv)
    executable = args.executable.resolve()
    if not executable.is_file():
        print(f"not found: {executable}")
        return 1
    bundle = executable.parent
    failures: list[str] = []

    with tempfile.TemporaryDirectory(prefix="schedule-maxing-smoke-") as scratch:
        scratch_dir = Path(scratch)
        before = snapshot(bundle)
        data = scratch_dir / "first" / "data"
        data.mkdir(parents=True)

        first, elapsed = run_self_test(executable, data, scratch_dir / "first.json")
        failures += describe("first launch", first, elapsed)
        second, elapsed = run_self_test(executable, data, scratch_dir / "second.json")
        failures += describe("restart", second, elapsed)

        storage = second.get("checks", {}).get("storage_and_scheduling", {})
        if storage.get("ok") and not storage.get("restart"):
            failures.append("restart: the data saved by the first run was not found by the second")
        if first.get("checks", {}).get("storage_and_scheduling", {}).get("restart"):
            failures.append("first launch: the scratch data folder was not empty")
        if not (data / "executions.db").is_file():
            failures.append(f"the database was not created in the data folder {data}")
        if not (data / "logs" / "schedule-maxing.log").is_file():
            failures.append("no log file was written to the data folder")
        if not args.expect_source:
            for result in (first, second):
                if not result.get("frozen"):
                    failures.append("the executable does not report itself as packaged")
            resources = first.get("checks", {}).get("resources", {}).get("resource_root", "")
            if resources and bundle not in Path(resources).parents and Path(resources) != bundle:
                failures.append(f"resources are read from outside the application folder: {resources}")

        if snapshot(bundle) != before:
            failures.append("the application folder was modified while running (it must be read-only at run time)")

        if not args.skip_copy:
            spaced = scratch_dir / "Program Files copy" / "Schedule Maxing"
            shutil.copytree(bundle, spaced)
            spaced_data = scratch_dir / "User Data With Spaces" / "data"
            spaced_data.mkdir(parents=True)
            third, elapsed = run_self_test(spaced / executable.name, spaced_data, scratch_dir / "third.json")
            failures += describe("path with spaces", third, elapsed)

    if failures:
        print("\nSMOKE TEST FAILED")
        for failure in failures:
            print(f" - {failure}")
        return 1
    print("\nSmoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
