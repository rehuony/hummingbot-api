#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=scripts/shell/lib/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
# shellcheck source=scripts/shell/lib/tailnet-state.sh
. "$REPO_ROOT/scripts/shell/lib/tailnet-state.sh"

case "${1:-}" in
    deploy)
        load_compose_settings
        require_configuration
        require_docker
        if dev_session_active; then
            fail 'Stop this checkout development session before deploying.'; exit 1
        fi
        COMPOSE_WITH_TAILSCALE=true verify_project_ownership
        verify_api_image
        mode=$(tailnet_mode)
        if [ "$mode" = host ]; then
            has_cmd tailscale || { fail 'Host Tailscale mode requires an installed, connected tailscale client.'; exit 1; }
            tailscale status >/dev/null
        fi
        # One serial path: a failed build must not generate bootstrap or touch services.
        bash "$REPO_ROOT/scripts/shell/build-image.sh"
        bash "$REPO_ROOT/scripts/shell/emqx.sh" auth
        if [ "$mode" = sidecar ]; then
            if tailnet_needs_userspace; then export TS_USERSPACE=true; fi
            COMPOSE_WITH_TAILSCALE=true compose up -d
        else
            # Include the sidecar even if .env was changed from sidecar to none/host.
            COMPOSE_WITH_TAILSCALE=true compose stop tailscale
            compose up -d
            if [ "$mode" = host ]; then
                configure_host_serve
            fi
        fi
        ;;
    stop)
        load_compose_settings
        require_docker
        COMPOSE_WITH_TAILSCALE=true
        verify_project_ownership
        compose down
        ;;
    pull-bot-image)
        image=$(config_get BOT_IMAGE hummingbot/hummingbot:latest)
        exec docker pull "$image"
        ;;
    tailscale-status)
        load_compose_settings
        if tailnet_sidecar_running; then
            require_docker
            COMPOSE_WITH_TAILSCALE=true verify_project_ownership
            docker exec hummingbot-tailscale tailscale status
            docker exec hummingbot-tailscale tailscale serve status
        elif has_cmd tailscale; then
            tailscale status
            tailscale serve status
        else
            fail 'Tailscale is not available; check the configured mode with make doctor.'
        fi
        ;;
    *) fail 'Usage: bash scripts/shell/stack.sh {deploy|stop|pull-bot-image|tailscale-status}'; exit 1 ;;
esac
