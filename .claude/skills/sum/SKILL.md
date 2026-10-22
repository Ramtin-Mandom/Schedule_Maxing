---
name: sum
description: Hand-off summary for a new session. Distills what this conversation already learned about the topics given in [brackets] (state, decisions, exact file/line pointers, dead ends, next steps) into a compact sum_<topic>.md so the next session can start from it instead of re-reading files and re-deriving context. Never edits code, commits or pushes.
argument-hint: "[topic or parts to extract] [+ sum_<name>.md to add to an existing summary; + alone = latest]"
disable-model-invocation: true
allowed-tools:
  - Write
  - Read
  - Glob
  - Bash(git rev-parse:*)
  - Bash(git status:*)
  - Bash(git log:*)
---

# /sum — summarize this session for the next one

Requested scope: $ARGUMENTS

You are writing a briefing for a fresh agent that has **none** of this conversation. Its only job is to let
that agent continue the work while reading as few files and tokens as possible. Every line must either save
the next session a file read, a search, a test run or a wrong turn — or it doesn't belong.

## 1. Parse the scope

- The text inside `[...]` names what to extract. It may be one topic (`[sync fix]`), several
  (`[deferred pages, perf numbers]`) or a slice of a topic (`[only the failing tests in the sync fix]`).
  Extract **only** that; leave out everything else this session touched, even if it was most of the work.
- Several topics go into one file, each under its own `##` section.
- No brackets / empty: summarize the session's main line of work (skip side questions and abandoned tangents).
- If the scope names something this conversation never covered, say so and stop — don't explore the
  repository to fill it in. A summary of what you didn't see is worse than none.
- **Add mode:** a `+` after the brackets means "add to an existing summary" instead of creating a new one:
  - `+ sum_sync_fix.md` (or `+ sum_sync_fix`, `+ sync_fix`) — that file in the project root.
  - `+` alone — the most recently modified `sum_*.md` in the project root (first `Glob` result).
  - Plain words such as "add to sum_sync_fix.md" / "update the previous summary" mean the same.
  - If the target doesn't exist (or there is no `sum_*.md` at all), say so and stop; don't create one.
  - Examples: `/sum [retry logic] + sum_sync_fix.md`, `/sum [test results] +`, `/sum +`.

## 2. Gather — from the conversation, not the repository

- Source material is what is **already in this conversation**: files read, edits made, command and test
  output, decisions the user made, errors hit.
- Don't re-read files or search to "refresh" the picture. The only allowed calls are cheap facts for the
  header: `git rev-parse --abbrev-ref HEAD`, `git rev-parse --short HEAD`, `git status --short`, `Glob`
  for an existing `sum_*.md`, and — in add mode only — `Read` of the one target summary file.
- If you're unsure whether something is still true (e.g. a file was edited after you read it), mark it
  `(unverified)` rather than checking.
- Never include secrets, tokens, `.env` contents or credentials.

## 3. Name the file

- `sum_<slug>.md` in the project root, where `<slug>` is a short lowercase, underscore-separated name of what
  the scope is about (2–4 words): `[the sync conflict fix]` → `sum_sync_conflict_fix.md`. With no brackets,
  name it after the session's main work.
- If that file already exists, don't overwrite it: use `sum_<slug>_2.md` (`_3`, …) — unless the scope says
  to update/replace it, in which case rewrite it in place.
- In add mode, skip naming: the target file keeps its name.

## 3b. Add mode — merge into the existing summary

Merge, don't just append a second summary below the first. The result must still read as one current
briefing for a fresh agent:

- **Status** — rewrite to the current truth: move items from In progress / Not started to Done when this
  session finished them; drop "Uncommitted" entries that are now committed.
- **Key facts / Decisions / Gotchas / Already ruled out** — add new bullets; correct or delete old ones this
  session proved wrong (never leave two contradicting bullets). Don't repeat what's already there.
- **Where to look** — add new rows; remove rows for files that no longer matter.
- **Tests & commands** — replace an old result with the newer one for the same command.
- **Next steps / Open questions** — rewrite: drop what's done or answered, add what's new.
- New topics from the brackets that don't fit an existing section get their own `##` section.
- Update the header line to the new date, branch and sha, and keep a short history line under it:
  `> Updated: <YYYY-MM-DD> (+ <scope>), <earlier date>, …`
- Keep the merged file within ~150 lines: compress or drop the least useful old detail rather than growing.
- Write the whole merged file with `Write`.

## 4. Write it — template

Keep it dense: bullets, not prose; target **≤ 120 lines**. Use `path:line` and symbol names instead of
pasting code. Quote code only when the exact text matters (a signature, a regex, an error message) and keep
each snippet to a few lines. Drop the sections that would be empty.

```markdown
# Summary: <topic>

> Hand-off from a previous session (<YYYY-MM-DD>, branch `<branch>` @ `<short sha>`).
> Read this first; open only the files listed under "Where to look". Treat everything here as a starting
> point, not proof — re-check anything you are about to change.

## Goal
<1–3 lines: what the user wants and why; the acceptance condition if one was stated.>

## Status
- Done: <what is implemented/verified, with how it was verified (test name, command)>
- In progress: <partially done; exactly where it stopped>
- Not started: <remaining pieces>
- Uncommitted: <files changed but not committed that matter to this scope>

## Key facts
- <Non-obvious things learned about the code that the next session would otherwise have to discover:
  how X actually flows, which function owns Y, invariants, config values, numbers measured.>

## Decisions (and why)
- <Choice made> — <reason / what the user said>. Don't re-litigate these.

## Where to look
| File / symbol | Why it matters |
|---|---|
| `app/x.py:120` `Foo.bar()` | <role in this task> |
<Only files that must be opened. This list is the reading budget for the next session.>

## Already ruled out — don't re-read / re-try
- <Files examined and found irrelevant to this scope.>
- <Approaches tried that failed, with the reason (error, test result).>

## Tests & commands
- `<exact command>` — <result: passed / N failed (which) / not run yet>

## Gotchas
- <Traps hit: environment quirks, flaky tests, encoding issues, ordering constraints.>

## Next steps
1. <Concrete, ordered actions with file pointers.>

## Open questions
- <Undecided items that need the user.>
```

## 5. Finish

- Don't edit any other file; don't commit, push or stage.
- Reply with the file path, its line count (in add mode: what was added, changed or removed, in one or two
  lines), and one line telling the user how to use it, e.g.:
  "In the new session start with: read `sum_sync_conflict_fix.md` and continue from Next steps."
