"""
Reading the GitHub workflows (.github/workflows/*.yml) for the tests that check them.

The tests name a job by what it does (the job that runs `gh release create`), never by what it is called,
and read the jobs' order from their `needs`: renaming a job, adding one or rewording a step does not break
them. Only losing a property they protect does.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
SCRIPTS = ROOT / ".github" / "scripts"


def workflow_paths() -> list[Path]:
    return sorted(path for path in WORKFLOWS.iterdir() if path.suffix in {".yml", ".yaml"})


def load(name: str) -> tuple[str, dict]:
    """(the text, the parsed workflow) of one workflow file."""
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    return text, yaml.safe_load(text)


def triggers(workflow: dict) -> dict:
    """The events that start it, as {event: its settings or None} whichever way `on` is written."""
    on = workflow.get(True, workflow.get("on"))  # YAML reads the key `on` as the boolean true
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return {event: None for event in on}
    return dict(on or {})


def squash(expression: object) -> str:
    """A condition on one line, whatever its line breaks and indentation."""
    return " ".join(str(expression or "").split())


def needs(job: dict) -> set[str]:
    wanted = job.get("needs", [])
    return {wanted} if isinstance(wanted, str) else set(wanted)


def earlier_jobs(jobs: dict, name: str) -> set[str]:
    """Every job that must have run before this one (its `needs`, and theirs)."""
    found: set[str] = set()
    pending = list(needs(jobs[name]))
    while pending:
        job = pending.pop()
        if job not in found:
            found.add(job)
            pending.extend(needs(jobs[job]))
    return found


def steps_text(jobs: dict, *names: str) -> str:
    """What the jobs' steps run and use, in order."""
    return "\n".join(str(step.get("run", "")) + str(step.get("uses", ""))
                     for name in names for step in jobs[name].get("steps", []))


def job_running(jobs: dict, pattern: str) -> str:
    """The one job with a step that matches the pattern."""
    found = [name for name in jobs if re.search(pattern, steps_text(jobs, name))]
    assert len(found) == 1, f"expected one job running {pattern!r}, found {found}"
    return found[0]


def grants(permissions: object) -> set[str]:
    """The access levels a `permissions` block gives (a bare `read-all` / `write-all` included)."""
    if permissions is None:
        return set()
    if isinstance(permissions, str):
        return {"write" if "write" in permissions else "read"}
    return {str(level) for level in permissions.values()}


def writing_jobs(jobs: dict) -> set[str]:
    return {name for name, job in jobs.items() if "write" in grants(job.get("permissions"))}


def named_secrets_only(text: str) -> bool:
    """Every secret is referenced by name in an expression; no value is written in the file."""
    return all("${{ secrets." in line for line in text.splitlines() if "secrets." in line)


#: An environment file (.env, .env.production), not PowerShell's $env: or a step's env: block.
ENV_FILE = re.compile(r"(?<![\w$])\.env\b(?!:)")
