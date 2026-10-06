"""
The Windows packaging configuration (packaging/windows): checked without building anything, so a
mistake in the spec, the installer script or the build helpers is caught by the development suite.
A real build is verified by packaging/windows/smoke_test.py (docs/windows-distribution.md).
"""

from __future__ import annotations

import importlib.util
import re
import struct
import sys
from pathlib import Path

import pytest

from app import desktop, selftest, version

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging" / "windows"
SPEC = (PACKAGING / "ScheduleMaxing.spec").read_text(encoding="utf-8")
ISS = (PACKAGING / "ScheduleMaxing.iss").read_text(encoding="utf-8")
BUILD = (PACKAGING / "build_windows.ps1").read_text(encoding="utf-8")


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"packaging_{name}", PACKAGING / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -----------------------------------------------------------------------------
# PyInstaller spec
# -----------------------------------------------------------------------------


def test_spec_builds_the_windowed_production_entry_point():
    assert 'HERE / "launcher.py"' in SPEC and "console=False" in SPEC and "exclude_binaries=True" in SPEC
    assert "upx=False" in SPEC and "icon=str(ICON)" in SPEC and "version=str(VERSION_FILE)" in SPEC
    assert "from app.desktop import main" in (PACKAGING / "launcher.py").read_text(encoding="utf-8")
    assert "SPECPATH" in SPEC and "getcwd" not in SPEC  # paths come from the spec's own location


@pytest.mark.parametrize("module", [
    "backend", "app.web", "app.persistence.direct", "app.persistence.executions", "app.persistence.planning",
    "sqlalchemy", "psycopg", "alembic", "fastapi", "starlette", "uvicorn", "httpx", "jwt", "argon2", "dotenv",
    "pytest", "tests", "benchmarks",
])
def test_spec_excludes_server_and_development_code(module):
    assert f'"{module}",' in SPEC.split("excludes = [", 1)[1].split("]", 1)[0]


def test_spec_bundles_only_the_named_resources():
    for resource in (ROOT / "config" / "task_preference.yaml", ROOT / "assets" / "ScheduleMaxing.ico"):
        assert resource.is_file()
    assert '"config" / "task_preference.yaml"' in SPEC and 'collect_data_files("customtkinter")' in SPEC
    assert 'collect_data_files("tzdata")' in SPEC
    bundled = SPEC.split("datas = [", 1)[1].split("hiddenimports = [", 1)[0]
    assert ".env" not in bundled and "samples" not in bundled and "(str(ROOT)" not in bundled  # never the repository root


def test_build_environment_is_the_desktop_runtime_plus_the_packager():
    lines = [line.strip() for line in (ROOT / "requirements-build.txt").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.startswith("#")]
    assert lines[0] == "-r requirements-desktop.txt" and [line.split(">")[0] for line in lines[1:]] == ["pyinstaller"]
    desktop_requirements = (ROOT / "requirements-desktop.txt").read_text(encoding="utf-8").lower()
    assert "pyinstaller" not in desktop_requirements


def test_icon_holds_every_size_windows_asks_for():
    data = (ROOT / "assets" / "ScheduleMaxing.ico").read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    sizes = sorted((struct.unpack("<B", data[6 + 16 * i:7 + 16 * i])[0] or 256) for i in range(count))
    assert (reserved, kind) == (0, 1) and sizes == [16, 24, 32, 48, 64, 128, 256]


# -----------------------------------------------------------------------------
# Version resource and checksums
# -----------------------------------------------------------------------------


def test_version_resource_comes_from_the_version_module():
    version_info = load("version_info")
    metadata = version_info.read_metadata()
    assert metadata["__version__"] == version.__version__ and metadata["APP_NAME"] == version.APP_NAME
    text = version_info.render(metadata)
    major, minor, patch = (int(part) for part in version.__version__.split("."))
    assert f"filevers=({major}, {minor}, {patch}, 0)" in text and f"'ProductVersion', '{version.__version__}'" in text
    assert "'OriginalFilename', 'ScheduleMaxing.exe'" in text and f"'CompanyName', '{version.APP_PUBLISHER}'" in text
    compile(text, "version_info.txt", "exec")  # PyInstaller evaluates this file


def test_version_resource_refuses_a_malformed_version(tmp_path):
    version_info = load("version_info")
    bad = tmp_path / "version.py"
    bad.write_text('__version__ = "1.0"\nAPP_NAME = "x"\nAPP_PUBLISHER = "x"\nAPP_DESCRIPTION = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        version_info.read_metadata(bad)


def test_publisher_is_a_name_and_not_an_address():
    assert version.APP_PUBLISHER == "Ramtin Rezaei" and "@" not in version.APP_PUBLISHER
    assert "/DAppPublisher=$Publisher" in BUILD  # the installer gets it from app/version.py, like the executable


def test_checksum_file_round_trips(tmp_path):
    checksums = load("checksums")
    installer = tmp_path / "ScheduleMaxing-Setup-9.9.9.exe"
    installer.write_bytes(b"not really an installer" * 1000)
    target = checksums.write_checksums([installer])
    assert target.name == "SHA256SUMS.txt"
    line = target.read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"[0-9a-f]{64}  ScheduleMaxing-Setup-9\.9\.9\.exe", line)
    assert checksums.parse_checksums(target.read_text(encoding="utf-8")) == {installer.name: checksums.sha256_of(installer)}
    assert checksums.main(["--verify", str(installer)]) == 0
    installer.write_bytes(b"tampered")
    assert checksums.main(["--verify", str(installer)]) == 1


# -----------------------------------------------------------------------------
# Bundle checks
# -----------------------------------------------------------------------------


def make_bundle(root: Path) -> Path:
    check_bundle = load("check_bundle")
    for name in check_bundle.REQUIRED:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    return root


def test_bundle_check_accepts_a_complete_folder_and_names_what_is_wrong(tmp_path):
    check_bundle = load("check_bundle")
    bundle = make_bundle(tmp_path / "ScheduleMaxing")
    assert check_bundle.check_folder(bundle) == []

    (bundle / "_internal" / ".env").write_text("DATABASE_URL=postgresql://user:pw@host/db\n", encoding="utf-8")
    (bundle / "_internal" / "sqlalchemy").mkdir()
    (bundle / "_internal" / "sklearn" / "tests").mkdir(parents=True)  # a library's own folder: not ours to judge
    (bundle / "_internal" / "config" / "task_preference.yaml").unlink()
    problems = "\n".join(check_bundle.check_folder(bundle))
    assert "environment file packaged: _internal/.env" in problems
    assert "secret-looking text (DATABASE_URL=)" in problems
    assert "forbidden package packaged: _internal/sqlalchemy" in problems
    assert "missing: _internal/config/task_preference.yaml" in problems
    assert "sklearn/tests" not in problems


# -----------------------------------------------------------------------------
# Self-test (the logic the packaged executable runs)
# -----------------------------------------------------------------------------


def test_self_test_refuses_the_real_data_directory(tmp_path):
    report = tmp_path / "report.json"
    assert selftest.run(report, checks=()) == selftest.EXIT_REFUSED
    assert "refused" in report.read_text(encoding="utf-8")


def test_self_test_reports_each_check_and_fails_when_one_does(tmp_path, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "DATA_DIR_OVERRIDDEN", True)

    def broken():
        raise RuntimeError("missing resource")

    report = tmp_path / "report.json"
    assert selftest.run(report, checks=(("fine", lambda: {"detail": 1}), ("broken", broken))) == selftest.EXIT_FAILED
    text = report.read_text(encoding="utf-8")
    assert '"detail": 1' in text and "RuntimeError: missing resource" in text
    assert selftest.run(report, checks=(("fine", lambda: {}),)) == selftest.EXIT_OK


def test_self_test_storage_and_scheduling_passes_from_source(tmp_path, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(settings, "DATA_DIR_OVERRIDDEN", True)
    selftest._check_resources()
    first = selftest._check_storage_and_scheduling()
    second = selftest._check_storage_and_scheduling()
    assert first["restart"] is False and second["restart"] is True
    assert set(first["modes"]) == {"precise_greedy", "adhd_friendly", "early_finish", "night_owl", "catch_up"}
    assert (tmp_path / "data" / "executions.db").is_file()


def test_self_test_switch_is_wired_to_the_entry_point(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "start_runtime", lambda **options: None)
    called: list = []
    monkeypatch.setattr(selftest, "run", lambda path: called.append(path) or 0)
    assert desktop.main(["--self-test", str(tmp_path / "r.json")], window_factory=lambda: pytest.fail("no window")) == 0
    assert called == [str(tmp_path / "r.json")]
    assert desktop.main(["--self-test"], window_factory=lambda: pytest.fail("no window")) == selftest.EXIT_REFUSED


# -----------------------------------------------------------------------------
# Inno Setup script
# -----------------------------------------------------------------------------


def directive(name: str) -> str:
    match = re.search(rf"^{name}=(.*)$", ISS, flags=re.MULTILINE)
    assert match, f"{name} is not set"
    return match.group(1).strip()


def test_installer_identity_is_fixed_and_the_version_is_passed_in():
    assert directive("AppId") == "{{14235D14-9AA2-440C-A227-D8AC414886C7}"
    assert directive("AppVersion") == "{#AppVersion}" and directive("OutputBaseFilename") == "ScheduleMaxing-Setup-{#AppVersion}"
    assert "#error AppVersion is required" in ISS
    assert not re.search(r"\b\d+\.\d+\.\d+\b", re.sub(r"^;.*$", "", ISS, flags=re.MULTILINE))  # no hard-coded version
    assert "/DAppVersion=$Version" in BUILD and "version_info.py" in BUILD


def test_installer_is_64_bit_for_windows_10_and_later():
    assert directive("ArchitecturesAllowed") == "x64compatible"
    assert directive("ArchitecturesInstallIn64BitMode") == "x64compatible"
    assert directive("MinVersion") == "10.0"
    assert directive("DefaultDirName") == r"{autopf}\{#AppName}"
    assert directive("PrivilegesRequired") == "admin" and directive("PrivilegesRequiredOverridesAllowed") == "dialog commandline"


def test_installer_waits_for_the_running_application_by_its_mutex():
    assert f'#define AppMutexName "{desktop.INSTANCE_MUTEX_NAME}"' in ISS
    assert "CheckForMutexes('{#AppMutexName}')" in ISS and "function PrepareToInstall" in ISS
    assert "function InitializeUninstall" in ISS


def test_installer_creates_shortcuts_and_can_relaunch_after_a_silent_update():
    assert r'Name: "{autoprograms}\{#AppName}"' in ISS
    assert re.search(r'Name: "\{autodesktop\}\\\{#AppName\}".*Tasks: desktopicon', ISS)
    assert re.search(r'Name: "desktopicon".*Flags: unchecked', ISS)
    assert "postinstall skipifsilent" in ISS and "Check: RelaunchRequested" in ISS and "{param:RELAUNCH|0}" in ISS


def test_installer_and_uninstaller_never_touch_user_data():
    code = re.sub(r"^;.*$", "", ISS, flags=re.MULTILINE).lower()
    for constant in ("{localappdata}", "{userappdata}", "{commonappdata}", "{userdocs}", "{%localappdata", "{%appdata"):
        assert constant not in code
    assert "[uninstalldelete]" not in code and "deltree" not in code and "deletefile" not in code
    install_delete = code.split("[installdelete]", 1)[1].split("[", 1)[0].strip().splitlines()
    assert install_delete == [r'type: filesandordirs; name: "{app}\_internal"']


def test_signing_is_optional_and_holds_no_certificate():
    assert "#ifdef SignedBuild" in ISS and "SignTool=smsign" in ISS
    assert "Not signed (no certificate configured)" in BUILD and "$env:SM_SIGN_PFX_PASSWORD" in BUILD
    assert not list(PACKAGING.rglob("*.pfx")) and not list(PACKAGING.rglob("*.pem"))


def test_build_script_stops_on_errors_and_checks_before_packaging():
    assert '$ErrorActionPreference = "Stop"' in BUILD
    order = [BUILD.index(f'Step "{title}') for title in ("Tests", "PyInstaller", "Checking the application folder",
                                                           "Smoke test", "Inno Setup installer", "Checksums")]
    assert order == sorted(order)
    assert sys.platform != "win32" or (PACKAGING / "build_windows.ps1").is_file()
