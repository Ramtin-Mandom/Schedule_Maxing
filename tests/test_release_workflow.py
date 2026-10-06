"""
.github/workflows/release.yml: the properties a release pipeline must keep, checked by reading the
file (the workflow itself only runs on GitHub; docs/windows-distribution.md, "Releasing").
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

from app import version

ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
WORKFLOW = yaml.safe_load(TEXT)
TRIGGERS = WORKFLOW[True]  # YAML reads the key `on` as the boolean true
JOBS = WORKFLOW["jobs"]


def steps_text(job: str) -> str:
    return "\n".join(str(step.get("run", "")) + str(step.get("uses", "")) for step in JOBS[job]["steps"])


def test_only_version_tags_and_manual_runs_start_it():
    assert set(TRIGGERS) == {"push", "workflow_dispatch"}
    assert TRIGGERS["push"] == {"tags": ["v[0-9]+.[0-9]+.[0-9]+"]}  # no branch, no pull request


def test_publishing_needs_every_earlier_job_to_pass():
    assert set(JOBS) == {"verify", "test", "build", "publish"}
    assert set(JOBS["build"]["needs"]) == {"verify", "test"}
    assert "build" in JOBS["publish"]["needs"]
    assert "continue-on-error" not in TEXT


def test_only_the_publishing_job_may_write_and_only_for_tags():
    assert WORKFLOW["permissions"] == {"contents": "read"}
    assert JOBS["publish"]["permissions"] == {"contents": "write"}
    for name in ("verify", "test", "build"):
        assert "permissions" not in JOBS[name]
    assert "github.event_name == 'push'" in JOBS["publish"]["if"] and "refs/tags/v" in JOBS["publish"]["if"]


def test_the_tag_must_equal_the_application_version():
    assert "version_info.py --check-tag" in steps_text("verify")
    spec = importlib.util.spec_from_file_location("version_info", ROOT / "packaging" / "windows" / "version_info.py")
    version_info = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(version_info)
    assert version_info.check_tag(f"v{version.__version__}") is None
    for wrong in (version.__version__, "v0.0.0", f"v{version.__version__}-rc1", f"V{version.__version__}"):
        assert version_info.check_tag(wrong) is not None


def test_the_build_is_checked_and_tested_before_it_is_uploaded():
    build = steps_text("build")
    order = [build.index(marker) for marker in ("build_windows.ps1", "test_installer.py", "checksums.py --verify",
                                                "actions/upload-artifact")]
    assert order == sorted(order)
    assert "xvfb-run -a python -m pytest" in steps_text("test") and "ruff check" in steps_text("test")


def test_a_stable_release_with_exactly_the_installer_and_its_checksums():
    publish = steps_text("publish")
    assert "gh release create" in publish and "--verify-tag" in publish
    assert "ScheduleMaxing-Setup-$VERSION.exe" in publish and "SHA256SUMS.txt" in publish
    assert "--draft" not in publish and "--prerelease" not in publish


def test_no_secret_value_or_environment_file_is_in_the_workflow():
    assert ".env" not in TEXT.replace("$env:", "").replace("env:", "")
    for line in TEXT.splitlines():
        if "secrets." in line:
            assert "${{ secrets." in line  # referenced by name only
    assert "DATABASE_URL" not in TEXT and "JWT_SECRET" not in TEXT


def test_actions_are_pinned_to_a_version():
    used = [step["uses"] for job in JOBS.values() for step in job["steps"] if "uses" in step]
    assert used and all("@v" in action and action.startswith("actions/") for action in used)
