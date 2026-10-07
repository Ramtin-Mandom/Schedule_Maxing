"""
.github/workflows/deploy-backend.yml: the properties the backend deployment must keep, checked by
reading the file (the workflow itself only runs on GitHub; docs/ci-cd.md, "Backend deployment").
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / ".github" / "workflows" / "deploy-backend.yml").read_text(encoding="utf-8")
WORKFLOW = yaml.safe_load(TEXT)
TRIGGERS = WORKFLOW[True]  # YAML reads the key `on` as the boolean true
JOBS = WORKFLOW["jobs"]


def steps_text(job: str) -> str:
    return "\n".join(str(step.get("run", "")) + str(step.get("uses", "")) for step in JOBS[job]["steps"])


def test_only_a_finished_ci_run_on_main_or_a_manual_run_starts_it():
    assert set(TRIGGERS) == {"workflow_run", "workflow_dispatch"}
    assert TRIGGERS["workflow_run"] == {"workflows": ["CI"], "types": ["completed"], "branches": ["main"]}
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    assert ci["name"] == "CI"  # the name the trigger above refers to


def test_nothing_runs_until_it_is_switched_on_and_ci_succeeded_for_a_push_of_this_repository():
    condition = JOBS["resolve"]["if"]
    assert "vars.BACKEND_DEPLOY_ENABLED == 'true'" in condition
    assert "workflow_run.conclusion == 'success'" in condition and "workflow_run.event == 'push'" in condition
    assert "head_repository.full_name == github.repository" in condition


def test_only_a_commit_of_main_with_a_successful_ci_run_is_deployed():
    assert set(JOBS) == {"resolve", "deploy"}
    assert JOBS["deploy"]["needs"] == "resolve"
    resolve = steps_text("resolve")
    assert "git merge-base --is-ancestor" in resolve and "origin/main" in resolve
    assert "gh run list" in resolve and "--workflow ci.yml" in resolve and "--status success" in resolve
    assert "continue-on-error" not in TEXT


def test_deploys_use_the_protected_environment_one_at_a_time():
    assert JOBS["deploy"]["environment"]["name"] == "production"
    assert WORKFLOW["concurrency"] == {"group": "deploy-backend", "cancel-in-progress": False}
    assert "timeout-minutes" in JOBS["deploy"]


def test_the_exact_commit_is_deployed_then_awaited_then_verified():
    deploy = steps_text("deploy")
    order = [deploy.index(marker) for marker in ('{\\"commitId\\": \\"$SHA\\"}', "live) exit 0", "/health", "/ready",
                                                "backend.smoke_check")]
    assert order == sorted(order)
    for failed in ("build_failed", "update_failed", "pre_deploy_failed", "canceled"):
        assert failed in deploy
    assert '.migrations == "current"' in deploy


def test_the_smoke_check_which_writes_accounts_is_opt_in():
    assert TRIGGERS["workflow_dispatch"]["inputs"]["smoke_check"]["default"] is False
    for step in JOBS["deploy"]["steps"]:
        if "smoke_check" in str(step.get("run", "")):
            assert "inputs.smoke_check" in step["if"]


def test_permissions_are_read_only():
    assert WORKFLOW["permissions"] == {"contents": "read"}
    assert JOBS["resolve"]["permissions"] == {"contents": "read", "actions": "read"}
    assert "permissions" not in JOBS["deploy"]
    assert "write" not in TEXT.replace("(writes", "").replace("WRITES", "")


def test_no_secret_value_database_address_or_environment_file_is_in_the_workflow():
    assert ".env" not in TEXT
    for line in TEXT.splitlines():
        if "secrets." in line:
            assert "${{ secrets." in line  # referenced by name only
    assert "DATABASE_URL" not in TEXT and "JWT_SECRET" not in TEXT


def test_no_expression_is_expanded_inside_a_script():
    # Inputs and event data reach the shell only as environment variables, never as script text.
    for job in JOBS.values():
        for step in job["steps"]:
            assert "${{" not in str(step.get("run", ""))


def test_actions_are_pinned_to_a_version():
    used = [step["uses"] for job in JOBS.values() for step in job["steps"] if "uses" in step]
    assert used and all("@v" in action and action.startswith("actions/") for action in used)
