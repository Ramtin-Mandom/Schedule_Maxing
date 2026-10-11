#!/usr/bin/env bash
# Usage: pr_tests_passed.sh <commit>
#
# The tests run once, for pull requests (.github/workflows/ci.yml). Everything after a merge -- the backend
# deployment and a release -- reuses that result instead of testing the same code again: this script finds the
# last pull request merged into main that is part of <commit> and succeeds only if CI passed for the commit
# that pull request was merged from.
#
# Needs: a full checkout (fetch-depth: 0), GH_TOKEN, GITHUB_REPOSITORY, and read access to actions and pull
# requests. Commits pushed straight to main after that pull request are NOT tested by anything: they ride on
# its result. Change code through pull requests.
set -euo pipefail

sha="${1:?usage: pr_tests_passed.sh <commit>}"

number="" head=""
while IFS=$'\t' read -r candidate candidate_head merge; do
  [ -n "$merge" ] || continue
  if git merge-base --is-ancestor "$merge" "$sha" 2>/dev/null; then
    number="$candidate" head="$candidate_head"
    break
  fi
done < <(gh pr list --repo "$GITHUB_REPOSITORY" --state merged --base main --limit 50 \
  --json number,headRefOid,mergeCommit,mergedAt \
  --jq 'sort_by(.mergedAt) | reverse | .[] | [.number, .headRefOid, (.mergeCommit.oid // "")] | @tsv')

if [ -z "$number" ]; then
  echo "No pull request merged into main is part of $sha: there is no test result to rely on." >&2
  exit 1
fi

passed="$(gh run list --repo "$GITHUB_REPOSITORY" --workflow ci.yml --commit "$head" \
  --status success --json databaseId --jq length)"
if [ "$passed" -lt 1 ]; then
  echo "CI did not pass for pull request #$number (commit $head): $sha is not accepted." >&2
  echo "Run CI for that commit (Actions > CI > Run workflow on its branch) and get it green first." >&2
  exit 1
fi
echo "Tested by pull request #$number (CI passed for $head)."
