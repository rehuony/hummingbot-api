#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=scripts/shell/lib/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

has_cmd conda || { fail 'Conda must be installed and available on PATH.'; exit 1; }
install_hooks() {
    conda run -n hummingbot-api pip install pre-commit
    conda run -n hummingbot-api pre-commit install
}

case "${1:-}" in
    install)
        environments=$(conda env list)
        if ! printf '%s\n' "$environments" | grep -q '^hummingbot-api '; then
            conda env create -f "$REPO_ROOT/environment.yml"
        fi
        install_hooks
        ;;
    install-pre-commit) install_hooks ;;
    uninstall) conda env remove -n hummingbot-api -y ;;
    run)
        load_compose_settings
        require_configuration
        for key in USERNAME PASSWORD CONFIG_PASSWORD BROKER_USERNAME BROKER_PASSWORD BROKER_DASHBOARD_PASSWORD; do
            value=$(env_get "$key")
            export "$key=$value"
        done
        require_docker
        COMPOSE_WITH_TAILSCALE=true verify_project_ownership
        conda run --no-capture-output -n hummingbot-api python "$REPO_ROOT/scripts/python/validate_dev_config.py"
        development_python=$(conda run --no-capture-output -n hummingbot-api python -c 'import sys; print(sys.executable)')
        [ -x "$development_python" ] || { fail 'Cannot locate the development environment Python.'; exit 1; }
        # mkdir acts as the session lock; a live/unknown session must not be replaced.
        if [ -d .dev-session ]; then
            if dev_session_active; then fail 'A development session is already active in this checkout.'; exit 1; fi
            rm -f .dev-session/pid .dev-session/pgid
            rmdir .dev-session
        fi
        mkdir -m 700 .dev-session
        printf '%s\n' "$$" > .dev-session/pid
        cleanup_session() {
            # Until exec hands ownership to the supervisor, no development child exists.
            rm -f "$REPO_ROOT/.dev-session/pid"
            rmdir "$REPO_ROOT/.dev-session"
        }
        trap cleanup_session EXIT
        trap 'exit 130' INT
        trap 'exit 143' TERM
        # shellcheck source=scripts/shell/lib/tailnet-state.sh
        . "$REPO_ROOT/scripts/shell/lib/tailnet-state.sh"
        mode=$(tailnet_mode)
        if [ "$mode" = host ]; then
            has_cmd tailscale || { fail 'Host Tailscale mode requires an installed, connected client.'; exit 1; }
            tailscale status >/dev/null
        fi
        bash "$REPO_ROOT/scripts/shell/emqx.sh" auth
        if [ "$mode" = sidecar ]; then
            if tailnet_needs_userspace; then export TS_USERSPACE=true; fi
            COMPOSE_WITH_TAILSCALE=true compose up -d emqx postgres tailscale
        else
            COMPOSE_WITH_TAILSCALE=true compose stop tailscale
            compose up -d emqx postgres
            if [ "$mode" = host ]; then
                configure_host_serve
            fi
        fi
        exec "$development_python" "$REPO_ROOT/scripts/python/dev_session.py" \
            conda run --no-capture-output -n hummingbot-api uvicorn main:app --reload \
            --env-file "$REPO_ROOT/.env" --host "$(normalize_bind "$API_BIND")" --port 8000
        ;;
    *) fail 'Usage: bash scripts/shell/dev.sh {install|install-pre-commit|uninstall|run}'; exit 1 ;;
esac
