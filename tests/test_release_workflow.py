"""
.github/workflows/release.yml: the properties a release pipeline must keep, checked by reading the
file (the workflow itself only runs on GitHub; docs/windows-distribution.md, "Releasing").

Jobs are found by what they do and ordered by their `needs` (tests/workflow_files.py); what every
workflow must keep is in tests/test_workflows.py.
"""

from __future__ import annotations

import importlib.util
import re

from app import version
from tests.workflow_files import (
    ENV_FILE,
    ROOT,
    earlier_jobs,
    grants,
    job_running,
    load,
    needs,
    squash,
    steps_text,
    triggers,
    writing_jobs,
)

TEXT, WORKFLOW = load("release.yml")
TRIGGERS = triggers(WORKFLOW)
JOBS = WORKFLOW["jobs"]

#: The jobs, by what they do.
VERIFY = job_running(JOBS, r"version_info\.py --check-tag")
TEST = job_running(JOBS, r"pr_tests_passed\.sh")
BUILD = job_running(JOBS, r"build_windows\.ps1")
PUBLISH = job_running(JOBS, r"gh release create")

SWITCH = "vars.SKIP_RELEASE_TESTS"


def condition(job: str) -> str:
    return squash(JOBS[job].get("if"))


def test_only_version_tags_and_manual_runs_start_it():
    assert "workflow_dispatch" in TRIGGERS
    assert set(TRIGGERS["push"]) == {"tags"}  # no branch
    assert not {"pull_request", "pull_request_target", "schedule", "workflow_run"} & set(TRIGGERS)
    # GitHub's tag filter: `.` is itself, `+` repeats what is before it.
    patterns = [re.compile(pattern.replace(".", r"\.")) for pattern in TRIGGERS["push"]["tags"]]
    assert patterns

    def starts(tag: str) -> bool:
        return any(pattern.fullmatch(tag) for pattern in patterns)

    assert starts(f"v{version.__version__}") and starts("v10.20.30")
    for other in (version.__version__, f"v{version.__version__}-rc1", "v1.2", "main", "latest"):
        assert not starts(other), other


def test_publishing_needs_every_earlier_job_to_pass():
    assert earlier_jobs(JOBS, PUBLISH) == set(JOBS) - {PUBLISH}
    assert {VERIFY, TEST} <= earlier_jobs(JOBS, BUILD)
    assert "continue-on-error" not in TEXT
    # Each later job names the results it requires: a failed or cancelled earlier job never lets it run.
    for job in (BUILD, PUBLISH):
        assert "!cancelled()" in condition(job) and "always()" not in condition(job)
        for earlier in needs(JOBS[job]):
            assert f"needs.{earlier}.result == 'success'" in condition(job), (job, earlier)


def test_the_tests_are_skipped_only_by_the_explicit_off_switch():
    assert condition(TEST) == f"{SWITCH} != 'true'"  # unset, empty or any other value: the tests count
    # A skipped test job is accepted by the build only together with the switch itself.
    skipped = f"needs.{TEST}.result == 'skipped'"
    assert f"({skipped} && {SWITCH} == 'true')" in condition(BUILD)
    assert condition(BUILD).count(skipped) == 1
    assert "skipped" not in condition(PUBLISH)
    # And the run says so.
    warnings = [step for step in JOBS[BUILD]["steps"] if squash(step.get("if")) == skipped]
    assert warnings and all("::warning" in step["run"] for step in warnings)
    assert not (TRIGGERS["workflow_dispatch"] or {}).get("inputs")  # no per-run input can skip them


def test_only_the_publishing_job_may_write_and_only_for_tags():
    assert grants(WORKFLOW["permissions"]) == {"read"}
    assert writing_jobs(JOBS) == {PUBLISH}
    assert JOBS[PUBLISH]["permissions"] == {"contents": "write"}
    # The test check only reads: the pull request the commit came from, and its CI run.
    assert {"actions", "pull-requests"} <= set(JOBS[TEST]["permissions"])
    assert "github.event_name == 'push'" in condition(PUBLISH) and "refs/tags/v" in condition(PUBLISH)


def test_the_tag_must_equal_the_application_version():
    assert VERIFY in earlier_jobs(JOBS, PUBLISH)
    spec = importlib.util.spec_from_file_location("version_info", ROOT / "packaging" / "windows" / "version_info.py")
    version_info = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(version_info)
    assert version_info.check_tag(f"v{version.__version__}") is None
    for wrong in (version.__version__, "v0.0.0", f"v{version.__version__}-rc1", f"V{version.__version__}"):
        assert version_info.check_tag(wrong) is not None


def test_the_build_is_checked_and_tested_before_it_is_uploaded():
    build = steps_text(JOBS, BUILD)
    markers = (r"build_windows\.ps1", r"test_installer\.py", r"checksums\.py --verify", r"actions/upload-artifact")
    order = [re.search(marker, build).start() for marker in markers]
    assert order == sorted(order)
    # The suite is not run a second time: a release relies on CI having passed for the pull request the
    # tagged commit came from, and only commits of main are released.
    check = steps_text(JOBS, TEST)
    assert "git merge-base --is-ancestor" in check and "origin/main" in check
    assert "pytest" not in check


def test_a_stable_release_with_exactly_the_installer_and_its_checksums():
    publish = steps_text(JOBS, PUBLISH)
    assert "--verify-tag" in publish
    assert "ScheduleMaxing-Setup-$VERSION.exe" in publish and "SHA256SUMS.txt" in publish
    assert "--draft" not in publish and "--prerelease" not in publish


def test_no_database_address_or_environment_file_is_in_the_workflow():
    assert not ENV_FILE.search(TEXT)
    assert "DATABASE_URL" not in TEXT and "JWT_SECRET" not in TEXT
