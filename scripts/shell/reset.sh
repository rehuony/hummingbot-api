#!/usr/bin/env bash
# Explicit destructive maintenance, scoped to this verified checkout and project.
set -euo pipefail
# shellcheck source=scripts/shell/lib/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

if dev_session_active; then
    fail 'Stop this checkout development session before resetting; no process was killed.'
    exit 1
fi
# Check local deletion boundaries before changing Docker resources as well.
[ ! -L bots ] && [ ! -L bots/credentials ] && [ ! -L bots/credentials/master_account ] || {
    fail 'Credential paths contain a symbolic link; refusing local deletion.'; exit 1;
}
load_compose_settings
COMPOSE_WITH_TAILSCALE=true
if ! docker info >/dev/null 2>&1; then
    [ "${ALLOW_PARTIAL_RESET:-0}" = 1 ] || {
        fail 'Docker is unavailable; refusing to delete configuration while broker state survives.'; exit 1;
    }
    echo 'Warning: ALLOW_PARTIAL_RESET=1 removes local files only; Docker volumes retain their old credentials.' >&2
else
    require_docker
    verify_project_ownership
    compose down -v --remove-orphans
    # Query failures and retained project volumes both stop local deletion.
    leftovers=$(docker volume ls -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT")
    [ -z "$leftovers" ] || { fail 'Project volumes survived teardown; local configuration was preserved.'; exit 1; }
fi
rm -f .env .setup-complete
if [ -d bots/credentials ]; then
    find bots/credentials -mindepth 1 -maxdepth 1 -type d ! -name master_account -exec rm -rf {} +
fi
if [ -d bots/credentials/master_account ]; then
    find bots/credentials/master_account -name '*.yml' -delete
    rm -f bots/credentials/master_account/.password_verification
fi
if [ -d .dev-session ]; then rm -rf .dev-session; fi
echo 'Reset complete.'
