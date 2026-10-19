---
name: wrap
description: End-of-session documentation sync. Detects what changed since the last /wrap (commits, staged, unstaged and untracked files), verifies it against the code, updates only the affected parts of ARCHITECTURE.md, ROADMAP.md, CURRENT_STATE.md, DECISIONS.md and CODEBASE_MAP.md, advances a local checkpoint, and prints a handoff summary. Never edits application code, commits or pushes.
argument-hint: "[optional focus or notes, e.g. 'only the sync work' or 'baseline']"
disable-model-invocation: true
allowed-tools:
  - Read
  - Grep
  - Glob
  - Bash(git status:*)
  - Bash(git diff:*)
  - Bash(git log:*)
  - Bash(git show:*)
  - Bash(git rev-parse:*)
  - Bash(git ls-files:*)
  - Bash(git merge-base:*)
---

# /wrap — synchronize the project knowledge base

Run at the end of a coding session. You keep the five root documents true to the repository, as cheaply
as possible, and hand off to the next session.

User notes for this run (may be empty): $ARGUMENTS

## Rules (non-negotiable)

- **Write only** to `ARCHITECTURE.md`, `ROADMAP.md`, `CURRENT_STATE.md`, `DECISIONS.md` and `CODEBASE_MAP.md`.
  - Also `AGENTS.md`/`CLAUDE.md`, under the conditions in step 6.
  - Also the checkpoint, and only through the script.
- **Never:**
  - edit application code, tests, configs or dependencies;
  - refactor, install anything, or run migrations;
  - touch user data, deployments or secrets (`.env` is never read);
  - commit, push or stash.
- If verification finds a bug, **document it**; don't fix it here.
- Leave unrelated user changes untouched. Don't assume every uncommitted change belongs to this session: when
  ownership is unclear, describe what the change does in the code, not who made it.
- Don't mark anything complete because of a commit message, a comment or an earlier agent's claim. Read the
  code and its tests.
- **Be idempotent:**
  - no duplicate ADRs or roadmap items;
  - no date- or commit-only edits;
  - no reformatting or reordering of untouched sections;
  - a document that needs no change stays byte-for-byte unchanged.

## Tools

The script: `${CLAUDE_SKILL_DIR}/scripts/wrap_checkpoint.py` (from the repository root it is
`.claude/skills/wrap/scripts/wrap_checkpoint.py`). It needs only the standard library and runs on Python 3.8+.
Use the project's `.venv` Python if there is one (`.venv/Scripts/python.exe` on Windows, `.venv/bin/python`
elsewhere), otherwise `python3` or `python`.

| Command | Effect |
|---|---|
| `status` | Read-only. Prints the verdict, checkpoint health, baseline, commits since the baseline, every path needing review with its category, and the documents usually affected. `--json` gives machine-readable output |
| `check-paths` | Read-only. Lists repository paths named in the five documents that do not exist |
| `advance [--uncovered PATH=REASON ...] [--note TEXT]` | Records HEAD plus fingerprints of the current uncommitted files as documented. Paths passed with `--uncovered` stay pending for the next run. Refuses during a merge or rebase |
| `show` | Prints the raw checkpoint |

The checkpoint lives in the git directory (`git rev-parse --git-path wrap-checkpoint.json`). It is private to
the clone or worktree, is never committed, and needs no ignore rule.

## Procedure

### 1. Detect changes (always first)

Run `status`. Then act on its verdict:

- **SYNCHRONIZED** — Make no edits and do not advance. Report "Documentation already synchronized" with the
  checkpoint, and stop. Use step 7's report format, kept short.
- **DOCUMENTATION-ONLY CHANGES** — Only docs or tooling changed. Check the changed documents against each other
  for contradictions (step 5), fix only real inconsistencies, then advance. Do not treat these changes as
  feature work.
- **REVIEW NEEDED** — Continue with step 2.
- **BASELINE NEEDED** — There is no checkpoint and no usable commit in `CURRENT_STATE.md`.
  - Read each document's header and its "Verified against commit" line.
  - Use the recent commits listed to find the subsystems that changed.
  - Check those subsystems against the documents.
  - Do a full rewrite only if the documents are missing or plainly wrong throughout, and say so.

Also handle these cases:

- **Notes.** "Checkpoint … is not an ancestor of HEAD", "no longer exists" and "Branch changed" mean history
  was rewritten or switched. Treat the listed dropped commits as possibly undocumented now. Re-check whether
  features the documents claim still exist.
- **A merge, rebase, cherry-pick or bisect in progress.** Do not advance. Report it.
- **`~` entries.** A change documented at the last wrap was reverted or edited since. Check the documents
  don't still describe it.
- **`!` entries.** These were deliberately left undocumented last time. Review them now.

If the user's notes narrow the scope, honor them. Every path you don't document must still be passed to
`--uncovered` in step 6.

### 2. Classify, then inspect only what changed

Group the review list by subsystem; the `[category]` labels help. Then work out what each change actually is,
using `git diff <base>..HEAD -- <path>` and `git diff -- <path>` for the specific paths. Don't read whole large
files; search them by symbol.

| Change type | Documents usually affected |
|---|---|
| New feature implemented | CURRENT_STATE, ROADMAP, CODEBASE_MAP |
| Existing feature modified | CURRENT_STATE, CODEBASE_MAP |
| Architectural change | ARCHITECTURE, DECISIONS, CODEBASE_MAP |
| Database or schema change | ARCHITECTURE, CODEBASE_MAP, CURRENT_STATE |
| File moved, renamed or deleted | CODEBASE_MAP (and every other document naming the old path) |
| Bug fixed | CURRENT_STATE; ROADMAP if it is tracked there |
| New known bug | CURRENT_STATE; ROADMAP if action is needed |
| Milestone completed | ROADMAP, CURRENT_STATE |
| Important design decision | DECISIONS; ARCHITECTURE if affected |
| API or interface change | ARCHITECTURE, CODEBASE_MAP |
| Tests added or changed | CURRENT_STATE; CODEBASE_MAP if test locations changed |

This is default guidance. Update any document a change genuinely affects, and nothing else. Use
`CODEBASE_MAP.md` section D to find related files and tests. Follow dependencies only as far as the change
reaches.

### 3. Verify before documenting

For each change that will affect a status claim:

1. Read the implementation, and how it is wired in. Is it reachable from the UI, an entry point or a route?
   Code that exists but is unreachable is *Partially Implemented*.
2. Read the related tests.
3. Run focused tests when that is practical: `python -m pytest -m dev <files>`.
   - `tests/sync`, `tests/web` and `tests/direct` are system tier, so run those files without `-m dev`.
   - Run real-window or full suites only if the user asks or the change needs them (see `CLAUDE.md`).
   - Never run anything against production, real PostgreSQL or live services, and never run migrations on real
     data.
4. Compare the result against the ROADMAP acceptance criteria and any missing integration.
5. When you cannot verify something, say why, and record it as *Unverified* rather than guessing.

Use the status words already in `CURRENT_STATE.md`: Implemented, Partially Implemented, Not Implemented,
Broken, Unverified. Keep four things distinct: implemented, tested, operational, and externally verified.

### 4. Edit the documents incrementally

Use targeted edits that keep each document's structure and wording.

- **ARCHITECTURE.md:** only for changed components, data flows, boundaries, or auth, sync, concurrency or
  storage design. Keep Mermaid diagrams matching the code. Minor changes don't belong here.
- **ROADMAP.md:**
  - Tick a deliverable `[x]` only when it is verified complete against its acceptance criteria.
  - Keep partial work open, with a status note.
  - Add only *confirmed* new work, in Part 1 or Part 2. Your own ideas go in Part 3, marked as needing approval.
  - Never delete unfinished requirements because related code appeared.
  - Never invent product requirements.
- **CURRENT_STATE.md:**
  - Update the affected rows: feature statuses, bugs, limitations, test results, infrastructure.
  - Change the "Generated" date and "Commit" rows only when you change this file's content.
  - Record tests run this session in section D, with real results.
- **DECISIONS.md:**
  - Append an ADR only for a real, consequential decision made in the changes.
  - Use the next free ID and the existing record template, and add it to the index.
  - Do not invent a rationale: use *Inferred* or "Rationale not recorded" where that is the truth.
  - For a superseded decision, mark the old record *Superseded by ADR-NNN* and link both ways. Never delete a
    record.
- **CODEBASE_MAP.md:**
  - Add new important modules to sections B and C.
  - Remove deleted ones and correct moved paths.
  - Update dependencies (section E), test locations, and the section D change guide.
  - Keep it a coherent map, not an appended list.
- **Commit references.** A "Verified against commit" line in a document: update it only in documents you
  otherwise change this run.

### 5. Validate (targeted)

- Run `check-paths`, which must report all paths existing. Fix stale references in the documents.
- Search the documents for any deleted or renamed path from the status list.
- Check that statuses agree across CURRENT_STATE, ROADMAP and CODEBASE_MAP, and that no ROADMAP item is ticked
  without verified implementation.
- Check each new ADR cites evidence.
- **Mermaid** (only for diagrams you changed): one statement per line; no `;` inside sequence messages; quote
  labels that contain brackets or punctuation. Parse them with a real renderer if a tool is available;
  otherwise say they were inspected but not rendered.

### 6. Agent instructions, then the checkpoint

- **AGENTS.md / CLAUDE.md.** Edit these only when one of these changed: a development or test command, a
  major convention, an architectural constraint agents must know, or the documentation navigation. Make the
  edit minimal, and don't duplicate content from the five documents.
- **Advance the checkpoint.** Run `advance` after the edits and validation succeed.
  - Pass `--uncovered "path=reason"` for every path from the review list that you did *not* document, for
    example unrelated in-progress work, a change you could not verify, or something the user excluded.
  - This keeps uncommitted work from being silently treated as documented.
  - Never advance if validation failed or an operation is in progress.
  - Then run `status` once more. Expect `SYNCHRONIZED`, or only your `--uncovered` paths.

### 7. Report

Print exactly this structure. Keep it concise, and mark which points are *confirmed* (read, run) and which
are *assumed*:

**WRAP SUMMARY**

**Repository Changes**
- Features added or modified.
- Bugs fixed.
- Architectural changes.

**Documentation Updated**
- Each modified document, and what changed in it.
- Documents left unchanged.

**Verification**
- Tests and checks run, with their results.
- What remains unverified, and why.

**Remaining Work**
- Incomplete features.
- Unresolved problems.
- Recommended next steps.

**Next Session Context**
3–6 sentences another agent can resume from: the branch, what is in flight, what to do next, and where to
start (a CODEBASE_MAP section).

**Checkpoint**
- The previous checkpoint and the new one, or "not advanced", with the reason.
- Uncommitted or uncovered changes not covered by the checkpoint.
