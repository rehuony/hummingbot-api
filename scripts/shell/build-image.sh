#!/usr/bin/env bash
# Collect provenance once for both local Docker builds and the CI build action.
set -euo pipefail
# shellcheck source=scripts/shell/lib/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

revision=$(git rev-parse --verify HEAD)
branch=$(git symbolic-ref --quiet --short HEAD || printf '%s' detached)
changes=$(git status --porcelain --untracked-files=normal)
dirty=false
if [ -n "$changes" ]; then
    dirty=true
fi
created=$(date -u +%Y-%m-%dT%H:%M:%SZ)
version=$(sed -n 's/^VERSION = "\([^"]*\)"$/\1/p' version.py)
if [ -z "$version" ]; then
    echo "Error: version.py must define a literal VERSION." >&2
    exit 1
fi

if [ "${1:-}" = --metadata ]; then
    printf 'revision=%s\nbranch=%s\ndirty=%s\ncreated=%s\nversion=%s\n' \
        "$revision" "$branch" "$dirty" "$created" "$version"
    exit 0
fi

image=${1:-$(config_get API_IMAGE rehuony/hummingbot-api:main)}
exec docker build \
    --build-arg "VCS_REF=$revision" \
    --build-arg "VCS_BRANCH=$branch" \
    --build-arg "VCS_DIRTY=$dirty" \
    --build-arg "BUILD_DATE=$created" \
    --build-arg "API_VERSION=$version" \
    -t "$image" .
