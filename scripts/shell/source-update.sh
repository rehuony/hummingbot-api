#!/usr/bin/env bash
# Source maintenance only: never build images, change runtime config, or deploy.
set -euo pipefail
# shellcheck source=scripts/shell/lib/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

action=${1:-}
case "$action" in
    check|update) ;;
    *) echo "Usage: bash scripts/shell/source-update.sh {check|update}" >&2; exit 1 ;;
esac

branch=$(git branch --show-current)
changes=$(git status --porcelain --untracked-files=normal)
if [ "$action" = update ]; then
    [ "$branch" = main ] || { echo "Error: update requires the main branch."; exit 1; }
    [ -z "$changes" ] || { echo "Error: commit or stash local changes before updating."; exit 1; }
fi
case "$(git remote get-url origin)" in
    https://github.com/rehuony/hummingbot-api|https://github.com/rehuony/hummingbot-api.git|git@github.com:rehuony/hummingbot-api.git|ssh://git@github.com/rehuony/hummingbot-api.git) ;;
    *) echo "Error: origin must point to rehuony/hummingbot-api."; exit 1 ;;
esac

if [ "$action" = update ]; then
    exec git pull --ff-only origin main
fi

if [ "$(git rev-parse --is-shallow-repository)" = true ]; then
    echo "Error: source comparison requires full Git history; deepen the checkout first." >&2
    exit 1
fi
current=$(git rev-parse --verify HEAD)
# Read this fetch's commit rather than a potentially stale origin/main ref.
git fetch --no-tags origin main
latest=$(git rev-parse --verify FETCH_HEAD)
if [ "$current" = "$latest" ]; then
    state=up_to_date
elif git merge-base --is-ancestor "$current" "$latest"; then
    state=update_available
elif git merge-base --is-ancestor "$latest" "$current"; then
    state=ahead
else
    state=diverged
fi
printf 'Source status: %s\nBranch: %s\nCurrent revision: %s\nRemote main: %s\n' \
    "$state" "${branch:-detached}" "$current" "$latest"
if [ -n "$changes" ]; then
    echo "Working tree: modified; this comparison describes commits only."
else
    echo "Working tree: clean"
fi
echo "This checks the checkout, not the running API. Use make deploy to build and apply source explicitly."
