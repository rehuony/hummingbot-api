#!/usr/bin/env bash
# Shared operational helpers. Sourcing this file never installs or starts services.
REPO_ROOT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT" || return 1

fail() { printf 'Error: %s\n' "$*" >&2; return 1; }
has_cmd() { command -v "$1" >/dev/null 2>&1; }

env_get() {
    local value code
    if [ ! -f "$REPO_ROOT/.env" ]; then
        printf '%s' "${2:-}"
        return
    fi
    if value=$(awk -v requested="$1" -f "$REPO_ROOT/scripts/shell/lib/read-env.awk" "$REPO_ROOT/.env"); then
        printf '%s' "$value"
    else
        code=$?
        [ "$code" -eq 2 ] || return "$code"
        printf '%s' "${2:-}"
    fi
}

# Compose interpolation uses the process environment before the project .env.
config_get() {
    local value
    if value=$(printenv "$1"); then
        printf '%s' "${value:-${2:-}}"
    else
        value=$(env_get "$1" "${2:-}") || return
        printf '%s' "${value:-${2:-}}"
    fi
}

load_compose_settings() {
    local key value default spec
    for spec in \
        API_IMAGE=rehuony/hummingbot-api:main API_BIND=127.0.0.1 \
        DB_BIND=127.0.0.1 DB_PORT=5432 \
        BROKER_BIND=127.0.0.1 BROKER_PUBLISHED_PORT=1883 \
        BROKER_DASHBOARD_BIND=127.0.0.1 BROKER_DASHBOARD_PORT=18083 \
        TAILSCALE_ENABLED=false TAILSCALE_MODE= TAILSCALE_AUTH_KEY= \
        TAILSCALE_HOSTNAME=hummingbot-api TS_USERSPACE=false \
        COMPOSE_PROJECT_NAME= COMPOSE_FILE=; do
        key=${spec%%=*}; default=${spec#*=}
        value=$(config_get "$key" "$default") || return
        export "$key=$value"
    done
    [ -z "$COMPOSE_FILE" ] || { fail 'COMPOSE_FILE is unsupported by Make commands; use docker-compose.override.yml.'; return 1; }
    # An empty exported project name is not a valid explicit Compose project.
    [ -n "$COMPOSE_PROJECT_NAME" ] || unset COMPOSE_PROJECT_NAME
    for key in DB_PORT BROKER_PUBLISHED_PORT BROKER_DASHBOARD_PORT; do
        value=${!key}
        case "$value" in ''|*[!0-9]*) fail "$key must be an integer from 1 to 65535."; return 1 ;; esac
        [ "${#value}" -le 5 ] && [ "$((10#$value))" -ge 1 ] && [ "$((10#$value))" -le 65535 ] || {
            fail "$key must be an integer from 1 to 65535."; return 1;
        }
    done
}

require_configuration() {
    local key value
    [ -f "$REPO_ROOT/.env" ] || { fail '.env is missing; run make setup.'; return 1; }
    for key in USERNAME PASSWORD CONFIG_PASSWORD BROKER_USERNAME BROKER_PASSWORD BROKER_DASHBOARD_PASSWORD; do
        value=$(env_get "$key") || return
        [ -n "$value" ] || { fail "$key must be set in .env; run make setup or repair the existing configuration."; return 1; }
    done
}

# The optional local override is retained. Explicit files make calls independent
# of cwd, and the full stack is selected for teardown even after a mode change.
compose() {
    local files=(-f "$REPO_ROOT/docker-compose.yml")
    if [ "${COMPOSE_WITH_TAILSCALE:-false}" = true ]; then
        files+=(-f "$REPO_ROOT/docker-compose.tailscale.yml")
    fi
    if [ -f "$REPO_ROOT/docker-compose.override.yml" ]; then
        files+=(-f "$REPO_ROOT/docker-compose.override.yml")
    fi
    docker compose --project-directory "$REPO_ROOT" --env-file "$REPO_ROOT/.env" "${files[@]}" "$@"
}

require_docker() {
    docker compose version >/dev/null 2>&1 || { fail 'Docker Compose plugin is required.'; return 1; }
    docker info >/dev/null 2>&1 || { fail 'Docker is unavailable; no services or local configuration were changed.'; return 1; }
}

resolve_project() {
    local configuration
    configuration=$(compose config) || return
    COMPOSE_PROJECT=$(printf '%s\n' "$configuration" | sed -n 's/^name: //p')
    case "$COMPOSE_PROJECT" in ''|*[!a-z0-9_-]*) fail 'Cannot resolve the Compose project safely.'; return 1 ;; esac
    COMPOSE_RESOURCES=$(printf '%s\n' "$configuration" |
        awk -f "$REPO_ROOT/scripts/shell/lib/compose-resources.awk") || return
}

verify_resource_ownership() {
    local kind=$1 names resource key name external labels
    names=$(docker "$kind" ls --format '{{.Name}}') || return
    while IFS=$'\t' read -r resource key name external; do
        [ "$resource" = "$kind" ] && [ "$external" != true ] || continue
        if printf '%s\n' "$names" | grep -Fxq -- "$name"; then
            labels=$(docker "$kind" inspect --format \
                "{{index .Labels \"com.docker.compose.project\"}}|{{index .Labels \"com.docker.compose.$kind\"}}" "$name") || return
            if [ "$labels" != "$COMPOSE_PROJECT|$key" ]; then
                fail "The resolved $kind $name is not owned by this Compose project and resource; refusing to change it."
                return 1
            fi
        fi
    done <<< "$COMPOSE_RESOURCES"
}

managed_resource_name() {
    local resource key name external
    while IFS=$'\t' read -r resource key name external; do
        [ "$resource" = "$1" ] && [ "$key" = "$2" ] || continue
        if [ "$external" = true ]; then
            fail "The $1 $2 is externally managed; refusing to reset it."
            return 1
        fi
        printf '%s' "$name"
        return
    done <<< "$COMPOSE_RESOURCES"
    fail "The $1 $2 is missing from the resolved Compose configuration."
}

verify_project_ownership() {
    local ids names container project directory
    resolve_project || return
    ids=$(docker ps -aq --filter "label=com.docker.compose.project=$COMPOSE_PROJECT") || return
    names=$(docker ps -a --format '{{.Names}}') || return
    for container in hummingbot-api hummingbot-broker hummingbot-postgres hummingbot-tailscale; do
        if printf '%s\n' "$names" | grep -qx "$container"; then
            ids="$ids $container"
        fi
    done
    for container in $ids; do
        project=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$container") || return
        directory=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$container") || return
        if [ "$project" != "$COMPOSE_PROJECT" ] || [ "$directory" != "$REPO_ROOT" ]; then
            fail "Container $container belongs to another project or checkout; refusing to change it."
            return 1
        fi
    done
    # Compose down -v removes declared non-external volumes by name, including
    # custom override names. Container labels alone do not establish their owner.
    verify_resource_ownership volume || return
    verify_resource_ownership network
}

normalize_bind() {
    local value=$1
    value=${value#[}; value=${value%]}
    printf '%s' "$value"
}

connect_host() {
    local value
    value=$(normalize_bind "$1")
    case "$value" in 0.0.0.0) printf 127.0.0.1 ;; ::|'[::]') printf ::1 ;; *) printf '%s' "$value" ;; esac
}

# Only sessions launched by this checkout are tracked. Never search or kill all
# uvicorn processes on a shared host. An unproven active PID blocks reset safely.
dev_session_active() {
    local pid pgid
    [ -d "$REPO_ROOT/.dev-session" ] || return 1
    [ -r "$REPO_ROOT/.dev-session/pid" ] || return 0
    read -r pid < "$REPO_ROOT/.dev-session/pid" || return 0
    case "$pid" in ''|0|1|*[!0-9]*) return 0 ;; esac
    kill -0 "$pid" 2>/dev/null && return 0
    # SIGKILL may interrupt publication of the child group; absence is not proof
    # that no child was started. Normal startup failures remove the whole guard.
    [ -e "$REPO_ROOT/.dev-session/pgid" ] || return 0
    read -r pgid < "$REPO_ROOT/.dev-session/pgid" || return 0
    case "$pgid" in ''|0|1|*[!0-9]*) return 0 ;; esac
    # A killed supervisor may leave its reload workers in the recorded group.
    kill -0 -- "-$pgid" 2>/dev/null && return 0
    return 1
}

configure_host_serve() {
    # A specific interface (including a tailnet IP) is served directly. A loopback
    # proxy is only useful when the API actually listens on loopback.
    case "$API_BIND" in
        127.0.0.1|0.0.0.0)
            tailscale serve status | grep -q ':8000' ||
                sudo tailscale serve --bg --tcp=8000 tcp://127.0.0.1:8000
            ;;
        *) echo 'API_BIND selects a specific interface; host Tailscale forwarding is unchanged.' ;;
    esac
}

verify_api_image() {
    local configuration image
    # Compose includes dependency images even when --images selects one service.
    # Read the named service from its canonical YAML, without resolving env_file.
    configuration=$(compose config --no-env-resolution hummingbot-api) || return
    image=$(printf '%s\n' "$configuration" | awk '
        /^  hummingbot-api:$/ {service=1; next}
        service && /^  [^ ]/ {exit}
        service && /^    image: / {sub(/^    image: /, ""); print; exit}
    ')
    [ "$image" = "$API_IMAGE" ] || {
        fail 'Resolved Compose API image differs from API_IMAGE; remove the conflicting local override.'
        return 1
    }
}

local_probe_host() {
    local addresses
    case "$1" in 127.0.0.1|::1) return 0 ;; esac
    if has_cmd ip; then
        addresses=$(ip -o addr show 2>/dev/null | awk '{sub(/\/.*/, "", $4); print $4}') || return 1
    elif has_cmd ifconfig; then
        addresses=$(ifconfig 2>/dev/null | awk '/inet6? / {sub(/%.*/, "", $2); print $2}') || return 1
    else
        return 1
    fi
    printf '%s\n' "$addresses" | grep -Fxq -- "$1"
}
