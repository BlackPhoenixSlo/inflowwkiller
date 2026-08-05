#!/usr/bin/env bash
# stage-mine.sh — stage a file as "HEAD plus exactly my edits", never "whatever the
# working tree holds right now".
#
# WHY THIS EXISTS. This repo is routinely open in two live sessions at once. A plain
# `git add <path>` stages the file's CURRENT contents, so it silently swallows the
# other session's in-flight work — and their matching half stays uncommitted. That
# shipped a caller without its callee twice (`_SellSurface` in scripts_api) and a UI
# reading config keys the committed engine does not have twice (`GhostCycleEditor`),
# once with a red suite whose commit message claimed green because the tests were run
# before the add.
#
#   ./scripts/stage-mine.sh <path> <blob-file>
#
# `blob-file` is the exact content to commit, which you build from `git show HEAD:<path>`
# plus your own edits. The working tree is never touched, so their work survives and
# `git status` keeps showing it as theirs to land.
set -euo pipefail
[ $# -eq 2 ] || { echo "usage: $0 <repo-path> <blob-file>" >&2; exit 2; }
path="$1"; blob="$2"
[ -f "$blob" ] || { echo "no such blob file: $blob" >&2; exit 2; }
# Two statements, not one: inside `$( )` a failing hash-object still lets
# update-index run with an empty sha under `set -e`, which is a silent no-op stage.
sha="$(git hash-object -w "$blob")"
[ -n "$sha" ] || { echo "hash-object produced nothing for $blob" >&2; exit 1; }
git update-index --cacheinfo "100644,$sha,$path"
echo "staged $path from $blob (working tree untouched)"
