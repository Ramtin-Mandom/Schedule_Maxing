"""
packaging/windows/test_installer.py

An end-to-end check of the installer itself, with no manual steps:

    python packaging/windows/test_installer.py dist/installer/ScheduleMaxing-Setup-X.Y.Z.exe [--upgrade NEWER.exe]

1. installs silently, for the current user only (no administrator prompt),
   into a scratch folder whose path contains spaces;
2. runs the installed program's self-test twice with a scratch data folder
   (first launch, then restart with the data still there);
3. installs again over it -- the same installer, or the newer one given with
   --upgrade -- and checks the data folder is byte-for-byte what it was and
   the program still passes with that data;
4. uninstalls silently and checks that the program folder is gone while the
   data folder is untouched.

It never uses the real install location or the real per-user data. It does
add and remove an Add/Remove Programs entry for the current user, so run it
on a build machine or CI runner, not on a computer where Schedule Maxing is
installed for that user (it refuses if it finds one). Standard library only.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from smoke_test import describe, run_self_test, snapshot  # noqa: E402 - this folder was just put on the path

APP_ID_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{14235D14-9AA2-440C-A227-D8AC414886C7}_is1"
SILENT = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"]
TIMEOUT_SECONDS = 900


def already_installed() -> bool:
    import winreg

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            winreg.CloseKey(winreg.OpenKey(hive, APP_ID_KEY))
            return True
        except OSError:
            continue
    return False


def uninstall_finished(log: Path) -> bool:
    """Whether the uninstaller has written its last line and let go of its log file."""
    try:
        with log.open("r+", encoding="utf-8", errors="replace") as file:  # fails while the uninstaller holds it
            return "Log closed." in file.read()
    except OSError:
        return False


def install(installer: Path, target: Path, log: Path) -> None:
    command = [str(installer), *SILENT, "/CURRENTUSER", "/NOICONS", f"/DIR={target}", f"/LOG={log}"]
    completed = subprocess.run(command, timeout=TIMEOUT_SECONDS)
    if completed.returncode != 0:
        raise SystemExit(f"FAILED: {installer.name} exited with {completed.returncode} (log: {log})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install, upgrade and uninstall the installer in a scratch folder.")
    parser.add_argument("installer", type=Path)
    parser.add_argument("--upgrade", type=Path, default=None, help="a newer installer to upgrade with (default: the same one)")
    parser.add_argument("--keep", type=Path, default=None, help="keep the installer logs in this folder")
    args = parser.parse_args(argv)
    installer = args.installer.resolve()
    upgrade = (args.upgrade or args.installer).resolve()
    for path in (installer, upgrade):
        if not path.is_file():
            print(f"not found: {path}")
            return 1
    if not sys.platform.startswith("win"):
        print("this check runs on Windows only")
        return 1
    if already_installed():
        print("Schedule Maxing is already installed on this computer; this check would replace and remove that "
              "installation. Run it on a build machine or uninstall first.")
        return 1

    failures: list[str] = []
    # A scratch file that is still briefly held open must not turn a finished run into a crash.
    with tempfile.TemporaryDirectory(prefix="schedule-maxing-installer-", ignore_cleanup_errors=True) as scratch:
        scratch_dir = Path(scratch)
        logs = args.keep or scratch_dir
        logs.mkdir(parents=True, exist_ok=True)
        target = scratch_dir / "Program Files test" / "Schedule Maxing"
        data = scratch_dir / "user data" / "ScheduleMaxing"
        data.mkdir(parents=True)
        executable = target / "ScheduleMaxing.exe"
        uninstaller = target / "unins000.exe"

        print(f"== install {installer.name}")
        install(installer, target, logs / "install.log")
        for needed in (executable, uninstaller, target / "_internal" / "config" / "task_preference.yaml"):
            if not needed.is_file():
                failures.append(f"not installed: {needed}")
        if failures:
            print("\n".join(failures))
            return 1
        (target / "_internal" / "left-over-from-an-older-build.txt").write_text("stale", encoding="utf-8")

        result, elapsed = run_self_test(executable, data, scratch_dir / "first.json")
        failures += describe("installed: first launch", result, elapsed)
        result, elapsed = run_self_test(executable, data, scratch_dir / "second.json")
        failures += describe("installed: restart", result, elapsed)
        if not result.get("checks", {}).get("storage_and_scheduling", {}).get("restart"):
            failures.append("restart: the saved data was not found")

        database = data / "executions.db"
        before = snapshot(data)
        print(f"== upgrade with {upgrade.name}")
        install(upgrade, target, logs / "upgrade.log")
        if snapshot(data) != before:
            failures.append("the upgrade changed the user's data folder")
        if (target / "_internal" / "left-over-from-an-older-build.txt").exists():
            failures.append("the upgrade left a file of the previous build behind")
        result, elapsed = run_self_test(executable, data, scratch_dir / "third.json")
        failures += describe("upgraded: launch with existing data", result, elapsed)
        if not result.get("checks", {}).get("storage_and_scheduling", {}).get("restart"):
            failures.append("after the upgrade the saved data was not found")

        before = snapshot(data)
        print("== uninstall")
        completed = subprocess.run([str(uninstaller), *SILENT, f"/LOG={logs / 'uninstall.log'}"], timeout=TIMEOUT_SECONDS)
        if completed.returncode != 0:
            failures.append(f"the uninstaller exited with {completed.returncode}")
        # The uninstaller hands over to a temporary copy of itself and returns at once; that copy removes the
        # folder and then closes its log. Wait for both before judging the result (and before the scratch
        # folder, which holds that log, is deleted).
        uninstall_log = logs / "uninstall.log"
        for _ in range(120):
            if not executable.exists() and not uninstaller.exists() and uninstall_finished(uninstall_log):
                break
            time.sleep(0.5)
        if executable.exists():
            failures.append("the program is still there after uninstalling")
        if not database.is_file() or snapshot(data) != before:
            failures.append("uninstalling changed or removed the user's data folder")
        if already_installed():
            failures.append("the Add/Remove Programs entry is still there after uninstalling")

    if failures:
        print("\nINSTALLER TEST FAILED")
        for failure in failures:
            print(f" - {failure}")
        return 1
    print("\nInstaller test passed: install, restart, upgrade and uninstall kept the user's data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
