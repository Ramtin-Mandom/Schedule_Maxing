"""
.github/workflows/deploy-backend.yml: the properties the backend deployment must keep, checked by
reading the file (the workflow itself only runs on GitHub; docs/ci-cd.md, "Backend deployment").

Jobs are found by what they do and ordered by their `needs` (tests/workflow_files.py); what every
workflow must keep is in tests/test_workflows.py.
"""

from __future__ import annotations

import re

from tests.workflow_files import (
    ENV_FILE,
    SCRIPTS,
    WORKFLOWS,
    earlier_jobs,
    grants,
    job_running,
    load,
    squash,
    steps_text,
    triggers,
    writing_jobs,
)

TEXT, WORKFLOW = load("deploy-backend.yml")
TRIGGERS = triggers(WORKFLOW)
JOBS = WORKFLOW["jobs"]

#: The job that starts the Render deploy, and the jobs that decide whether it may.
DEPLOY = job_running(JOBS, r"api\.render\.com")
GATES = earlier_jobs(JOBS, DEPLOY)

CHECK_NAME = "pr_tests_passed.sh"
CHECK = (SCRIPTS / CHECK_NAME).read_text(encoding="utf-8")


def test_only_a_push_to_main_or_a_manual_run_starts_it():
    assert "workflow_dispatch" in TRIGGERS
    assert TRIGGERS["push"] == {"branches": ["main"]}  # no other branch, no tag
    assert not {"pull_request", "pull_request_target", "schedule", "workflow_run"} & set(TRIGGERS)


def test_the_tests_run_once_for_pull_requests_and_never_for_a_push_or_a_tag():
    # The workflow whose result the shared check reads is the one that runs for pull requests.
    tested_by = re.search(r"--workflow (\S+)", CHECK).group(1)
    assert (WORKFLOWS / tested_by).is_file()
    _, ci = load(tested_by)
    assert "pull_request" in triggers(ci) and "push" not in triggers(ci)
    assert ci["concurrency"]["cancel-in-progress"] is True  # a newer push replaces the older run
    assert "pytest" in steps_text(ci["jobs"], *ci["jobs"])


def test_nothing_runs_until_it_is_switched_on():
    first = [name for name in JOBS if not earlier_jobs(JOBS, name)]
    assert first and DEPLOY not in first  # the deploy always waits for a job that carries the switch
    for name in first:
        condition = squash(JOBS[name]["if"])
        assert "vars.BACKEND_DEPLOY_ENABLED == 'true'" in condition
        for event in TRIGGERS:
            assert f"github.event_name == '{event}'" in condition


def test_only_a_commit_of_main_whose_pull_request_passed_ci_is_deployed():
    assert GATES
    gate = steps_text(JOBS, *GATES)
    assert "git merge-base --is-ancestor" in gate and "origin/main" in gate
    assert CHECK_NAME in gate
    # The shared check: the last pull request merged into main that the commit contains, and its CI result.
    assert "gh pr list" in CHECK and "--state merged" in CHECK and "--base main" in CHECK
    assert "git merge-base --is-ancestor" in CHECK
    assert "gh run list" in CHECK and "--status success" in CHECK
    assert "set -euo pipefail" in CHECK
    # Nothing lets the deploy run after a failed gate.
    assert "always()" not in squash(JOBS[DEPLOY].get("if")) and "continue-on-error" not in TEXT


def test_deploys_use_the_protected_environment_one_at_a_time():
    environment = JOBS[DEPLOY]["environment"]
    assert (environment["name"] if isinstance(environment, dict) else environment) == "production"
    assert WORKFLOW["concurrency"]["group"] and WORKFLOW["concurrency"]["cancel-in-progress"] is False
    assert "${{" not in str(WORKFLOW["concurrency"]["group"])  # one queue for every deploy
    assert "timeout-minutes" in JOBS[DEPLOY]


def test_the_exact_commit_is_deployed_then_awaited_then_verified():
    deploy = steps_text(JOBS, DEPLOY)
    markers = (r"commitId.*\$SHA", r"live\)\s*exit 0", r"/health\b", r"/ready\b", r"backend\.smoke_check")
    order = [re.search(marker, deploy).start() for marker in markers]
    assert order == sorted(order)
    for failed in ("build_failed", "update_failed", "pre_deploy_failed", "canceled"):
        assert failed in deploy
    assert re.search(r"""\.migrations\s*==\s*["']current["']""", deploy)


def test_the_smoke_check_which_writes_accounts_is_opt_in():
    assert TRIGGERS["workflow_dispatch"]["inputs"]["smoke_check"]["default"] is False
    smoke = [step for step in JOBS[DEPLOY]["steps"] if "smoke_check" in str(step.get("run", ""))]
    assert smoke
    for step in smoke:
        assert "inputs.smoke_check" in step["if"]


def test_permissions_are_read_only():
    assert grants(WORKFLOW["permissions"]) == {"read"}
    assert not writing_jobs(JOBS)
    # The gate reads the pull request and its CI run; the deploy job gets nothing beyond the default.
    for name in GATES:
        if CHECK_NAME in steps_text(JOBS, name):
            assert {"actions", "pull-requests"} <= set(JOBS[name]["permissions"])
    assert "permissions" not in JOBS[DEPLOY]


def test_no_database_address_or_environment_file_is_in_the_workflow():
    assert not ENV_FILE.search(TEXT)
    assert "DATABASE_URL" not in TEXT and "JWT_SECRET" not in TEXT
