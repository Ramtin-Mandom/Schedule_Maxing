"""
Every workflow in .github/workflows: the properties all of them must keep, checked by reading the files.
A new workflow is checked without editing this file (tests/workflow_files.py).
"""

from __future__ import annotations

import re

import pytest

from tests.workflow_files import ROOT, grants, load, named_secrets_only, needs, workflow_paths

NAMES = [path.name for path in workflow_paths()]

#: Files of this repository that a step may name: they must exist, or the step fails only on GitHub.
REPOSITORY_PATH = re.compile(r"(?:(?:tests|packaging|\.github[/\\]scripts)[/\\][\w./\\-]+|requirements[\w-]*\.txt)")
#: A release tag (v4, v4.1.2) or a full commit.
PINNED = re.compile(r"@(v\d+(\.\d+)*|[0-9a-f]{40})$")


def steps(workflow: dict) -> list[dict]:
    return [step for job in workflow["jobs"].values() for step in job.get("steps", [])]


def test_there_are_workflows_to_check():
    assert NAMES


@pytest.mark.parametrize("name", NAMES)
def test_it_is_a_workflow_whose_jobs_only_need_jobs_that_exist(name):
    _, workflow = load(name)
    jobs = workflow["jobs"]
    assert jobs
    for job in jobs.values():
        assert job.get("steps") or job.get("uses")
        assert needs(job) <= set(jobs)


@pytest.mark.parametrize("name", NAMES)
def test_no_step_may_fail_quietly(name):
    text, _ = load(name)
    assert "continue-on-error" not in text


@pytest.mark.parametrize("name", NAMES)
def test_no_expression_is_expanded_inside_a_script(name):
    # Inputs and event data reach the shell only as environment variables, never as script text.
    _, workflow = load(name)
    for step in steps(workflow):
        assert "${{" not in str(step.get("run", "")), step.get("name")


@pytest.mark.parametrize("name", NAMES)
def test_actions_are_pinned_to_a_version(name):
    _, workflow = load(name)
    used = [step["uses"] for step in steps(workflow) if "uses" in step]
    for action in used:
        assert PINNED.search(action), action
        # Another publisher's action only at an exact commit: its tags can be moved.
        assert action.startswith(("actions/", "github/")) or re.search(r"@[0-9a-f]{40}$", action), action


@pytest.mark.parametrize("name", NAMES)
def test_secrets_are_named_never_written(name):
    text, _ = load(name)
    assert named_secrets_only(text)


@pytest.mark.parametrize("name", NAMES)
def test_the_default_permissions_never_write(name):
    _, workflow = load(name)
    assert "write" not in grants(workflow.get("permissions"))


@pytest.mark.parametrize("name", NAMES)
def test_every_file_a_step_names_exists(name):
    _, workflow = load(name)
    for step in steps(workflow):
        for path in REPOSITORY_PATH.findall(str(step.get("run", ""))):
            assert (ROOT / path.replace("\\", "/").rstrip("./")).exists(), f"{step.get('name')}: {path}"
