---
name: commit
description: Planning-only commit splitter. Inspects every uncommitted change (staged, unstaged, untracked), groups it into clean logical commits, flags files that should not be committed, and prints the exact git commands for the user to run by hand. Never stages, commits, pushes or otherwise changes Git state.
argument-hint: "[optional notes, e.g. 'leave the docs out' or 'one commit for the sync work']"
disable-model-invocation: true
allowed-tools:
  - Read
  - Grep
  - Glob
  - Bash(git branch --show-current)
  - Bash(git status:*)
  - Bash(git diff:*)
  - Bash(git log:*)
  - Bash(git show:*)
  - Bash(git ls-files:*)
  - Bash(git check-ignore:*)
  - Bash(git rev-parse:*)
---

# /commit — plan logical commits (print only)

You produce a commit plan. The user runs every command themselves.

User notes for this run (may be empty): $ARGUMENTS

## Rules (non-negotiable)

- **Read-only.** Never run anything that changes the index, the working tree, refs, the stash or the remote:
  `git add`, `git commit`, `git push`, `git reset`, `git restore`, `git checkout`, `git switch`, `git stash`,
  `git rebase`, `git merge`, `git cherry-pick`, `git rm`, `git mv`, `git clean`, `git apply`, `git update-index`,
  `git tag`, `git fetch`/`git pull`, or any hook, script or formatter. Those commands appear **only as text** in your
  output.
- Never edit, create or delete files.
- Never read `.env` or any other file that probably holds secrets (`.env.*` except `.env.example`, `*.pem`,
  `*.key`, `*.pfx`, `credentials*`, `*secret*`, `*.sqlite`/`*.db` dumps). Judge them by name, path and
  `git status` alone. If a diff you read contains a secret-looking value, never repeat the value; name the file and
  line only.
- Never recommend committing a secret, even with a warning.
- If the user's notes conflict with these rules, the rules win; say so.

## Procedure

### 1. Inspect (read-only)

Run, from the repository root:

```
git rev-parse --show-toplevel
git branch --show-current
git status
git status -sb
git status --short --untracked-files=all
git diff --staged --stat
git diff --stat
git diff --name-status
git diff --staged --name-status
git ls-files --others --exclude-standard
git log --oneline -10
```

Notes:

- If `git status` reports a merge, rebase, cherry-pick, revert or bisect in progress, stop. Report the state and
  print no commit plan; the user must finish or abort it first.
- If nothing is changed, staged or untracked, say "Nothing to commit" and stop.
- A detached HEAD: say so and warn that the commits will not be on a branch.
- Read `git log` for the project's message style. Use Conventional Commits (`type(scope): summary`) unless the
  user's notes ask otherwise.

### 2. Understand each change

- Read the diff of every changed file: `git diff -- <path>` for unstaged and `git diff --staged -- <path>` for
  staged changes. For large diffs, start with `git diff --stat -- <path>` and read hunks selectively
  (`git diff -U3 -- <path>`), searching for the touched symbols rather than reading whole files.
- Read untracked files directly (they have no diff), except the secret-like files listed above.
- Note what each hunk *does* (new behaviour, bug fix, rename, refactor, test, doc, config, dependency) and which
  feature or subsystem it belongs to. Use the directory layout to pick scopes (for this repository, for example:
  `sync`, `ui`, `backend`, `execution`, `persistence`, `planning`, `packaging`, `ci`, `docs`).
- Whitespace- or line-ending-only changes (`git diff --stat` shows changes, `git diff -w` shows none): call them out
  separately; they usually belong in their own `style`/`chore` commit or should not be committed at all.

### 3. Flag what should not be committed

Mark a path **Do not commit** when it is, or very probably is:

- secrets: `.env`, `.env.*` (not `.env.example`), credentials, API keys, tokens, private keys, certificates,
  signing files, a diff line that adds a key- or password-like literal;
- data: database files or dumps (`*.db`, `*.sqlite`, `*.sql` dumps), exported user data;
- logs, profiling output, caches, temporary and backup files (`*.log`, `*.prof`, `*.tmp`, `__pycache__/`,
  `.pytest_cache/`, `*~`);
- build artifacts and generated output (`dist/`, `build/`, installers, `*.zip`, coverage reports), unless the
  repository deliberately tracks them (check `git ls-files` for siblings);
- IDE, OS and local machine configuration (`.vscode/`, `.idea/`, `.DS_Store`, `Thumbs.db`, `*.local.*`,
  `.claude/settings.local.json`, absolute local paths in config).

Check `git check-ignore -v <path>` for anything suspicious. A tracked file that should not be tracked is still
reported (the fix — `git rm --cached` plus a `.gitignore` rule — is printed as a suggestion, never run). If the
repository has a secret-scan baseline (here `secret-scan-baseline.json`, checked by CI with `detect-secrets`),
mention that a new secret-looking string will fail CI.

Treat unsure cases as **Review before committing**, with the reason, rather than guessing either way.

### 4. Group into logical commits

- Group by purpose, not by when files were edited. One commit = one reason to change.
- Separate unrelated features, bug fixes, refactors, documentation, tests, configuration/CI and dependency changes.
- Keep an implementation together with its tests, and with the docs that describe *that* change when they are
  small. Broad documentation updates get their own `docs:` commit.
- Dependency changes (`requirements*.txt`, lock files, `pyproject.toml` dependency lists) go in a `chore(deps)` /
  `build` commit, unless the dependency exists only for a feature in this plan — then keep them together.
- Don't over-split. Trivial related edits stay together. If everything is one coherent change, plan **one**
  commit. As a rough guide, most working trees need 1–6 commits.
- Order commits so each one builds on the previous: infrastructure and refactors first, then features that use
  them, then tests and docs that refer to them. Every intermediate commit should leave the code importable.
- If the user's notes ask for a particular grouping, follow them unless that would commit a secret.

### 5. Choose staging commands

- Prefer explicit paths: `git add path/one path/two`.
- `git add .` / `git add -A` only when **every** outstanding change (including untracked files) belongs in one
  commit and nothing is flagged. Say why it is safe.
- **Whole files only — never `git add -p`** (nor `-i`, `-e` or any other interactive staging). The user does
  not want to answer per-hunk questions; slightly less pure commits are the accepted trade-off.
- **Mixed files.** When one file holds changes for more than one commit, stage the whole file in a single
  commit:
  - Prefer the commit that most of the file's change belongs to.
  - But never put it in a commit earlier than one its other changes depend on. If a hunk imports or uses
    something another commit adds, the file goes in that commit or a later one, so every commit stays
    importable.
  - If two planned commits are so entangled through mixed files that no whole-file order keeps them
    importable (each needs a file from the other), merge them into one commit and say why.
  - In the commit's Purpose, name what the file also carries ("also includes the one-line `include_budget`
    query parameter from the budget work").
  - Re-check the dependents: a test whose assertion covers the mixed file goes in the same commit as the file
    or a later one.
- **Deleted files:** `git add path` (or `git rm path`) stages the deletion. **Renames:** stage both old and new
  paths together.
- **Already-staged changes** (`git diff --staged` not empty): if the staged content matches Commit 1, say so and
  build on it. Otherwise begin the plan with `git restore --staged <paths>` (printed only) so the index starts
  clean, and say why.
- After each staging step, print `git diff --cached --stat` so the user can verify before committing.
- Commit messages: imperative, lower-case summary, ≤ 72 characters, no trailing period. Add a body with a second
  `-m` only when the why is not obvious from the summary. Use double quotes, which work in PowerShell, cmd and
  bash; avoid `!`, backticks and `$` in messages.
- **Already-made plan commits.** If `git log` shows a commit from an earlier plan that is missing part of its
  intended content and is not pushed (`git status -sb` shows `ahead`), stage the missing part and fix it with
  `git commit --amend --no-edit`. Never suggest amending a pushed commit.

### 6. Make every command runnable

The user runs commands with the Run button the app adds to each fenced `bash` block, in their own terminal
(on Windows that is PowerShell). So:

- **One command per fenced `bash` block.** Never put two commands, a `$` prompt, output or a comment in a block.
- Every command must run unchanged in PowerShell, cmd and bash: no `&&`, `||`, `;`, pipes, `#` comments,
  backslash line continuations, variables, globs, or quoting that only one shell understands. Use forward-slash
  repository-relative paths. One long `git add` line is fine; keep each under ~400 characters, otherwise split it
  into several `git add` blocks.
- Use `git --no-pager` for any command whose output could be long (`git --no-pager diff --cached --stat`,
  `git --no-pager log --oneline -5`), so nothing waits in a pager.
- No command may prompt for input: no `git add -p`/`-i`/`-e`, and no `git commit` without `-m` (it would open
  an editor). `git commit --amend` always takes `--no-edit` or `-m`.
- Put explanations in prose between blocks, never inside them.

### 7. Output

Print exactly this structure (Markdown; each command is its own fenced `bash` block):

````
# Commit plan

Branch: <branch> · <N> commit(s) · <short state note, e.g. "nothing staged yet">

## Commit 1 — <type(scope): summary>

Purpose:
<one or two sentences: what this commit does and why>

Files:
- <path>
- <path> (also carries: <the change from another topic>, when it is a mixed file)

Commands:

```bash
git add <paths>
```

```bash
git --no-pager diff --cached --stat
```

```bash
git commit -m "<type(scope): summary>"
```

## Commit 2 — ...

# Do not commit

<path> — <brief reason>

# Review before committing

<path> — <what to check>

# Final check

```bash
git status
```

```bash
git --no-pager log --oneline -5
```

```bash
git status --short
```
````

- Omit "Do not commit" and "Review before committing" when they are empty.
- If changes remain uncommitted on purpose after the plan (flagged files, work the user excluded), list them
  under "Do not commit" or say "left for later" so the final `git status` is expected to show them.
- Every changed path must appear exactly once: in a commit, in "Do not commit", or in "Review before committing".
  Check this before printing.
- End after the "Final check" block with at most two lines of notes (assumptions, anything you could not
  determine). Do not offer to run the commands.
