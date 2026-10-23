#!/usr/bin/env python3
"""
.claude/skills/wrap/scripts/wrap_checkpoint.py

Change detection and the local checkpoint for the /wrap skill (.claude/skills/wrap/SKILL.md).
Standard library and the git command line only; works on Python 3.8+.

    python wrap_checkpoint.py status [--json]
    python wrap_checkpoint.py advance [--uncovered PATH ...] [--note TEXT]
    python wrap_checkpoint.py show

The checkpoint is private, per-clone (per-worktree) metadata stored inside the git directory at
`git rev-parse --git-path wrap-checkpoint.json`, so it is never committed and needs no ignore rule.
It records:

    commit      HEAD when /wrap last finished: every commit up to it has been documented
    branch      the branch HEAD was on (informational)
    covered     {path: fingerprint} of the uncommitted files whose state was documented then
    uncovered   {path: reason} of changes seen but deliberately left undocumented

A fingerprint is the git blob id of the file's content ("deleted" for a missing file), so a
documented uncommitted change that is later committed unchanged is still recognised as documented,
while one that is edited, reverted or deleted afterwards is reported again.

`status` never writes anything. `advance` only writes the checkpoint file, and refuses while a
merge, rebase, cherry-pick, revert or bisect is in progress.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCHEMA = 1
CHECKPOINT_NAME = "wrap-checkpoint.json"
KB_DOCS = ("ARCHITECTURE.md", "ROADMAP.md", "CURRENT_STATE.md", "DECISIONS.md", "CODEBASE_MAP.md")
AGENT_DOCS = ("AGENTS.md", "CLAUDE.md")
DELETED = "deleted"
LIST_LIMIT = 60

# (category, path prefixes or exact paths) -- first match wins. Exact paths have no trailing slash.
RULES = (
    ("knowledge-base", (*KB_DOCS, "ci_check_notes.md")),
    ("agent-instructions", AGENT_DOCS),
    ("agent-tooling", (".claude/",)),
    ("schema", ("backend/migrations/", "backend/models.py", "app/execution/db.py")),
    ("tests", ("tests/",)),
    ("documentation", ("docs/", "README.md", "LICENSE")),
    ("sync", ("app/sync/", "backend/sync.py", "backend/mutations.py", "backend/snapshots.py",
              "backend/record_mapping.py")),
    ("auth", ("backend/accounts.py", "backend/passwords.py", "backend/password_policy.py", "backend/security.py",
              "backend/security_api.py", "backend/native_sessions.py", "backend/browser_sessions.py",
              "backend/login_guard.py", "backend/rate_limit.py", "backend/mfa.py", "backend/account_security.py",
              "backend/verification.py", "backend/verification_api.py", "backend/recovery.py",
              "backend/recovery_api.py", "backend/auth_schemas.py")),
    ("backend-api", ("backend/",)),
    ("scheduling-planning", ("app/planning/", "app/optimizer.py", "app/mode_objectives.py", "app/reward.py",
                             "app/pert.py", "app/constraints.py", "app/models.py", "app/data_processor.py")),
    ("execution", ("app/execution/",)),
    ("analytics", ("app/productivity/",)),
    ("direct-mode", ("app/persistence/",)),
    ("local-web", ("app/web/",)),
    ("updater", ("app/update/",)),
    ("desktop-ui", ("app/ui/", "app/app.py")),
    ("packaging-release", ("packaging/", "app/version.py", "app/selftest.py", "app/desktop.py")),
    ("configuration", ("config/", "app/runtime.py", "app/logging_setup.py", ".env.example", "pyproject.toml",
                       ".editorconfig", ".gitattributes", ".gitignore", "docker-compose.postgres-test.yml")),
    ("dependencies", ("requirements",)),
    ("ci", (".github/", "secret-scan-baseline.json")),
    ("benchmarks", ("benchmarks/",)),
    ("assets", ("samples/", "assets/")),
    ("app-other", ("app/", "main.py")),
)
NON_APPLICATION = {"knowledge-base", "agent-instructions", "agent-tooling", "documentation"}

# Default guidance (the /wrap table); the agent decides what actually needs updating.
DOC_HINTS = {
    "schema": "ARCHITECTURE, CODEBASE_MAP, CURRENT_STATE",
    "sync": "CURRENT_STATE, CODEBASE_MAP; ARCHITECTURE if protocol or data flow changed",
    "auth": "CURRENT_STATE, CODEBASE_MAP; ARCHITECTURE/DECISIONS if the auth design changed",
    "backend-api": "CODEBASE_MAP, CURRENT_STATE; ARCHITECTURE if an API/interface changed",
    "tests": "CURRENT_STATE (test status); CODEBASE_MAP if test locations changed",
    "configuration": "CURRENT_STATE (infrastructure); ARCHITECTURE if configuration/environment design changed",
    "dependencies": "ARCHITECTURE (technologies) if a dependency was added/removed; CURRENT_STATE",
    "ci": "CURRENT_STATE (infrastructure); ARCHITECTURE section on deployment if the pipeline changed",
    "packaging-release": "CURRENT_STATE; ARCHITECTURE if delivery changed; CODEBASE_MAP if paths changed",
    "benchmarks": "usually none; CURRENT_STATE if a baseline result changed",
    "assets": "usually none",
}
SOURCE_HINT = "CURRENT_STATE, CODEBASE_MAP; ROADMAP if a tracked item progressed; ARCHITECTURE/DECISIONS if design changed"


class WrapError(Exception):
    pass


def git(root: Path | None, *args: str, check: bool = True, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(root) if root else None, input=stdin.encode("utf-8") if stdin is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        raise WrapError(f"git {' '.join(args)} failed: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout.decode("utf-8", "replace") if result.returncode == 0 else ""


def repo_root() -> Path:
    out = git(None, "rev-parse", "--show-toplevel", check=False).strip()
    if not out:
        raise WrapError("Not inside a git repository.")
    return Path(out)


def checkpoint_path(root: Path) -> Path:
    path = Path(git(root, "rev-parse", "--git-path", CHECKPOINT_NAME).strip())
    return path if path.is_absolute() else root / path


def category_of(path: str) -> str:
    for category, patterns in RULES:
        for pattern in patterns:
            if path == pattern or (pattern.endswith("/") and path.startswith(pattern)) \
                    or (pattern == "requirements" and path.startswith("requirements")):
                return category
    return "other"


def is_commit(root: Path, sha: str) -> bool:
    return bool(sha) and bool(git(root, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", check=False).strip())


def is_ancestor(root: Path, older: str, newer: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", older, newer], cwd=str(root),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def operation_in_progress(root: Path) -> str | None:
    for marker, name in (("MERGE_HEAD", "merge"), ("rebase-merge", "rebase"), ("rebase-apply", "rebase/am"),
                         ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert"), ("BISECT_LOG", "bisect")):
        path = Path(git(root, "rev-parse", "--git-path", marker).strip())
        if (path if path.is_absolute() else root / path).exists():
            return name
    return None


def parse_name_status(output: str) -> list[dict]:
    """Parse `git diff --name-status -z -M` output into {status, path, old_path}."""
    fields = output.split("\0")
    changes, i = [], 0
    while i < len(fields) and fields[i]:
        status = fields[i]
        if status[0] in "RC":
            changes.append({"status": status[0], "score": status[1:], "old_path": fields[i + 1], "path": fields[i + 2]})
            i += 3
        else:
            changes.append({"status": status[0], "path": fields[i + 1]})
            i += 2
    return changes


def fingerprints(root: Path, paths: list[str]) -> dict[str, str]:
    existing = [p for p in paths if (root / p).is_file()]
    result = {p: DELETED for p in paths if p not in existing}
    if existing:
        hashes = git(root, "hash-object", "--stdin-paths", stdin="\n".join(existing) + "\n").split()
        result.update(dict(zip(existing, hashes)))
    return result


def documented_commit_in_current_state(root: Path) -> str | None:
    """The commit CURRENT_STATE.md says it was verified against (a fallback baseline)."""
    path = root / "CURRENT_STATE.md"
    if not path.is_file():
        return None
    match = re.search(r"^\|\s*Commit\s*\|\s*`([0-9a-f]{7,40})`", path.read_text(encoding="utf-8", errors="replace"),
                      re.MULTILINE)
    return match.group(1) if match else None


def load_checkpoint(root: Path) -> tuple[dict | None, str | None]:
    path = checkpoint_path(root)
    if not path.exists():
        return None, "missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"unreadable ({exc.__class__.__name__})"
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("commit"), str):
        return None, "malformed"
    data.setdefault("covered", {})
    data.setdefault("uncovered", {})
    return data, None


def working_tree_changes(root: Path) -> list[dict]:
    """Staged, unstaged and untracked changes relative to HEAD (or everything, before the first commit)."""
    changes = []
    has_head = is_commit(root, "HEAD")
    if has_head:
        for change in parse_name_status(git(root, "diff", "--cached", "--name-status", "-z", "-M")):
            changes.append({**change, "source": "staged"})
        for change in parse_name_status(git(root, "diff", "--name-status", "-z", "-M")):
            changes.append({**change, "source": "unstaged"})
    for path in filter(None, git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")):
        changes.append({"status": "?", "path": path, "source": "untracked"})
    return changes


def build_status(root: Path) -> dict:
    head = git(root, "rev-parse", "HEAD", check=False).strip() or None
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip() or None
    checkpoint, problem = load_checkpoint(root)
    notes: list[str] = []
    dropped: list[str] = []
    base = None
    base_kind = None
    state = "ok"

    if checkpoint is not None:
        cp = checkpoint["commit"]
        if not is_commit(root, cp):
            state = "invalid"
            notes.append(f"Checkpoint commit {cp[:12]} no longer exists (history rewritten or garbage-collected).")
        elif head and is_ancestor(root, cp, head):
            base, base_kind = cp, "checkpoint"
        elif head:
            state = "diverged"
            merge_base = git(root, "merge-base", cp, head, check=False).strip()
            dropped = [line for line in git(root, "log", "--oneline", f"{head}..{cp}").splitlines() if line]
            notes.append(f"Checkpoint commit {cp[:12]} is not an ancestor of HEAD (rebase, force push, reset or "
                         f"branch switch). {len(dropped)} documented commit(s) are no longer in HEAD's history.")
            if merge_base:
                base, base_kind = merge_base, "merge-base of checkpoint and HEAD"
        if checkpoint.get("branch") and branch and checkpoint["branch"] != branch:
            notes.append(f"Branch changed since the checkpoint: {checkpoint['branch']} -> {branch}.")
    else:
        state = problem or "missing"

    if base is None and head:
        documented = documented_commit_in_current_state(root)
        if documented and is_commit(root, documented) and is_ancestor(root, documented, head):
            base, base_kind = git(root, "rev-parse", documented).strip(), "commit recorded in CURRENT_STATE.md"
        else:
            notes.append("No usable baseline: inspect the subsystems named by the recent commits below and the "
                         "working tree, rather than assuming the documents are current.")

    committed: list[dict] = []
    commits: list[str] = []
    if base and head and base != head:
        committed = [{**c, "source": "committed"} for c in
                     parse_name_status(git(root, "diff", "--name-status", "-z", "-M", f"{base}..{head}"))]
        commits = [line for line in git(root, "log", "--oneline", "--no-decorate", f"{base}..{head}").splitlines()
                   if line]
    elif base is None and head:
        commits = [line for line in git(root, "log", "--oneline", "--no-decorate", "-20").splitlines() if line]

    tree = working_tree_changes(root)
    by_path: dict[str, dict] = {}
    for change in committed + tree:
        entry = by_path.setdefault(change["path"], {"path": change["path"], "statuses": [], "sources": []})
        entry["statuses"].append(change["status"])
        entry["sources"].append(change["source"])
        if change.get("old_path"):
            entry["old_path"] = change["old_path"]

    covered = checkpoint["covered"] if checkpoint else {}
    uncovered = checkpoint["uncovered"] if checkpoint else {}
    # A documented uncommitted change that has since been reverted no longer shows as a change: report it.
    for path in covered:
        if path not in by_path:
            by_path[path] = {"path": path, "statuses": ["~"], "sources": ["reverted-or-changed-since-wrap"]}
    for path in uncovered:
        by_path.setdefault(path, {"path": path, "statuses": ["!"], "sources": ["left-undocumented-at-last-wrap"]})

    prints = fingerprints(root, sorted(by_path))
    review, documented = [], []
    for path, entry in sorted(by_path.items()):
        entry["category"] = category_of(path)
        entry["fingerprint"] = prints[path]
        if path in uncovered:
            entry["reason"] = f"left undocumented at last wrap: {uncovered[path]}"
            review.append(entry)
        elif path in covered and covered[path] == prints[path]:
            documented.append(entry)
        elif path in covered and "reverted-or-changed-since-wrap" in entry["sources"] \
                and prints[path] == _head_blob(root, path):
            entry["reason"] = "documented while uncommitted, since reverted to HEAD"
            review.append(entry)
        else:
            review.append(entry)

    application = [e for e in review if e["category"] not in NON_APPLICATION]
    hints = sorted({DOC_HINTS.get(e["category"], SOURCE_HINT) for e in application})
    if state == "ok" and not review and not dropped:
        verdict = "SYNCHRONIZED"
    elif base is None:
        verdict = "BASELINE NEEDED"
    elif not application and not dropped:
        verdict = "DOCUMENTATION-ONLY CHANGES"
    else:
        verdict = "REVIEW NEEDED"

    return {
        "verdict": verdict,
        "checkpoint_state": state,
        "checkpoint": checkpoint and {k: checkpoint.get(k) for k in ("commit", "branch", "recorded_at", "note")},
        "checkpoint_file": str(checkpoint_path(root)),
        "head": head,
        "branch": branch,
        "base": base,
        "base_kind": base_kind,
        "operation_in_progress": operation_in_progress(root),
        "commits": commits,
        "dropped_commits": dropped,
        "notes": notes,
        "review": review,
        "already_documented": [e["path"] for e in documented],
        "application_paths": len(application),
        "document_hints": hints,
    }


def _head_blob(root: Path, path: str) -> str:
    out = git(root, "rev-parse", "--verify", "--quiet", f"HEAD:{path}", check=False).strip()
    return out or DELETED


def describe(entry: dict) -> str:
    status = "/".join(dict.fromkeys(entry["statuses"]))
    sources = "+".join(dict.fromkeys(entry["sources"]))
    moved = f" (from {entry['old_path']})" if entry.get("old_path") else ""
    reason = f" -- {entry['reason']}" if entry.get("reason") else ""
    return f"  {status:<5} {entry['path']}{moved}  [{sources}]{reason}"


def print_status(status: dict) -> None:
    short = lambda sha: sha[:12] if sha else "none"  # noqa: E731
    cp = status["checkpoint"]
    print("WRAP STATUS")
    print(f"verdict:     {status['verdict']}")
    print(f"head:        {short(status['head'])} on {status['branch']}")
    if cp:
        print(f"checkpoint:  {short(cp['commit'])} on {cp.get('branch')} recorded {cp.get('recorded_at')} "
              f"[{status['checkpoint_state']}]")
    else:
        print(f"checkpoint:  {status['checkpoint_state']} ({status['checkpoint_file']})")
    print(f"baseline:    {short(status['base'])} ({status['base_kind'] or 'none'})")
    if status["operation_in_progress"]:
        print(f"WARNING:     a {status['operation_in_progress']} is in progress; finish it before advancing.")
    for note in status["notes"]:
        print(f"note:        {note}")
    if status["dropped_commits"]:
        print("documented commits no longer in history:")
        for line in status["dropped_commits"][:LIST_LIMIT]:
            print(f"  {line}")
    if status["commits"]:
        label = "commits since baseline" if status["base"] else "recent commits (no baseline)"
        print(f"{label}: {len(status['commits'])}")
        for line in status["commits"][:LIST_LIMIT]:
            print(f"  {line}")
    groups: dict[str, list[dict]] = {}
    for entry in status["review"]:
        groups.setdefault(entry["category"], []).append(entry)
    if groups:
        print(f"changes needing review: {len(status['review'])} "
              f"({status['application_paths']} application, rest documentation/tooling)")
        for category in sorted(groups, key=lambda c: (c in NON_APPLICATION, c)):
            entries = groups[category]
            print(f" [{category}] {len(entries)}")
            for entry in entries[:LIST_LIMIT]:
                print(describe(entry))
            if len(entries) > LIST_LIMIT:
                print(f"  ... {len(entries) - LIST_LIMIT} more")
    if status["already_documented"]:
        print(f"already documented at last wrap (unchanged since): {len(status['already_documented'])} path(s)")
    if status["document_hints"]:
        print("document hints (default guidance, not a rule):")
        for hint in status["document_hints"]:
            print(f"  - {hint}")
    print("legend: M modified, A added, D deleted, R renamed, ? untracked, ~ documented-then-changed, "
          "! left undocumented")


def advance(root: Path, uncovered_args: list[str], note: str | None) -> dict:
    in_progress = operation_in_progress(root)
    if in_progress:
        raise WrapError(f"A {in_progress} is in progress; finish or abort it before advancing the checkpoint.")
    head = git(root, "rev-parse", "HEAD", check=False).strip()
    if not head:
        raise WrapError("The repository has no commits yet; nothing to checkpoint.")
    uncovered: dict[str, str] = {}
    for item in uncovered_args:
        path, _, reason = item.partition("=")
        uncovered[path.replace("\\", "/").strip()] = reason.strip() or "not documented"
    dirty = sorted({c["path"] for c in working_tree_changes(root)})
    prints = fingerprints(root, dirty)
    data = {
        "schema": SCHEMA,
        "commit": head,
        "branch": git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip(),
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
        "note": note,
        "covered": {p: prints[p] for p in dirty if p not in uncovered},
        "uncovered": uncovered,
    }
    path = checkpoint_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(temporary), str(path))
    return data


PATH_TOKEN = re.compile(r"`([^`\s]+)`|\]\(([^)#\s]+)(?:#[^)]*)?\)")
CHECKED_ROOTS = ("app/", "backend/", "tests/", "docs/", "config/", "packaging/", "benchmarks/", "samples/",
                 "assets/", ".github/", ".claude/")
#: Files the documents name that are produced by builds or at run time, not stored in the repository.
GENERATED_FILES = {"SHA256SUMS.txt"}


def check_doc_paths(root: Path, docs: tuple[str, ...] = KB_DOCS) -> dict[str, list[str]]:
    """Repository paths named in the documents that do not exist (placeholders such as <x>, NN or vN skipped)."""
    files = [p for p in git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0") if p]
    basenames = {p.rsplit("/", 1)[-1] for p in files}
    missing: dict[str, list[str]] = {}
    for doc in docs:
        path = root / doc
        if not path.is_file():
            missing[doc] = ["(document missing)"]
            continue
        for match in PATH_TOKEN.finditer(path.read_text(encoding="utf-8", errors="replace")):
            raw = match.group(1) or match.group(2)
            candidate = raw.split(":")[0].split("#")[0].rstrip(",.;)").rstrip("/")
            if any(mark in candidate for mark in ("<", "NN", "vN", "{")) or candidate.startswith("http") \
                    or candidate in GENERATED_FILES:
                continue
            if candidate.startswith(CHECKED_ROOTS):
                exists = bool(list(root.glob(candidate))) if "*" in candidate else (root / candidate).exists()
            elif "/" not in candidate and re.fullmatch(r"[\w.*-]+\.(md|txt|toml|json|yml|yaml|lock|py)", candidate):
                # A bare file name (often package-relative in a table cell): it must exist somewhere.
                pattern = re.compile(re.escape(candidate).replace(r"\*", ".*") + r"\Z")
                exists = any(pattern.match(name) for name in basenames)
            else:
                continue
            if not exists:
                missing.setdefault(doc, []).append(raw)
    return {doc: sorted(set(items)) for doc, items in missing.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Change detection and checkpoint for the /wrap skill.")
    sub = parser.add_subparsers(dest="command", required=True)
    status_parser = sub.add_parser("status", help="report changes since the last wrap (read-only)")
    status_parser.add_argument("--json", action="store_true")
    advance_parser = sub.add_parser("advance", help="record HEAD and the documented working-tree state")
    advance_parser.add_argument("--uncovered", nargs="*", default=[], metavar="PATH[=REASON]",
                                help="changed paths that were NOT documented and must be reviewed next time")
    advance_parser.add_argument("--note", default=None)
    sub.add_parser("show", help="print the raw checkpoint")
    sub.add_parser("check-paths", help="list repository paths named in the five documents that do not exist")
    args = parser.parse_args(argv)
    try:
        root = repo_root()
        if args.command == "status":
            status = build_status(root)
            if args.json:
                print(json.dumps(status, indent=2))
            else:
                print_status(status)
        elif args.command == "advance":
            data = advance(root, args.uncovered, args.note)
            print(f"checkpoint advanced to {data['commit'][:12]} on {data['branch']}: "
                  f"{len(data['covered'])} uncommitted path(s) recorded as documented, "
                  f"{len(data['uncovered'])} left for review.")
        elif args.command == "check-paths":
            missing = check_doc_paths(root)
            for doc, items in missing.items():
                print(doc)
                for item in items:
                    print(f"  missing: {item}")
            print("all referenced paths exist" if not missing else f"{sum(map(len, missing.values()))} missing")
            return 1 if missing else 0
        else:
            checkpoint, problem = load_checkpoint(root)
            print(json.dumps(checkpoint, indent=2) if checkpoint else f"checkpoint {problem}: {checkpoint_path(root)}")
    except WrapError as exc:
        print(f"wrap_checkpoint: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
