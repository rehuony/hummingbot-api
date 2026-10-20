#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=scripts/shell/lib/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

write_auth() {
    local user pass temporary
    [ -f .env ] || { fail '.env is missing; run make setup.'; return 1; }
    user=$(env_get BROKER_USERNAME)
    pass=$(env_get BROKER_PASSWORD)
    [ -n "$user" ] && [ -n "$pass" ] || { fail 'BROKER_USERNAME and BROKER_PASSWORD must be set in .env.'; return 1; }
    case "$user$pass" in *','*|*'"'*|*$'\n'*|*$'\r'*) fail 'Broker credentials cannot contain CSV separators, quotes or newlines.'; return 1 ;; esac
    [ ! -L .emqx ] && [ ! -L .emqx/auth-bootstrap.csv ] || { fail 'Broker bootstrap must not be a symbolic link.'; return 1; }
    [ ! -e .emqx/auth-bootstrap.csv ] || [ -f .emqx/auth-bootstrap.csv ] || {
        fail 'Broker bootstrap path must be a regular file; repair it before deployment.'
        return 1
    }
    mkdir -p .emqx
    chmod 700 .emqx
    temporary=$(mktemp .emqx/auth-bootstrap.XXXXXX)
    # Docker resolves the 0700 directory as root; EMQX uid 1000 needs a readable file.
    if ! printf 'user_id,password,is_superuser\n%s,%s,false\n' "$user" "$pass" > "$temporary" ||
        ! chmod 644 "$temporary" || ! mv -f "$temporary" .emqx/auth-bootstrap.csv; then
        rm -f "$temporary"
        return 1
    fi
    echo 'Broker bootstrap credentials prepared.'
}

case "${1:-}" in
    auth) write_auth ;;
    auth-reset)
        load_compose_settings
        require_configuration
        require_docker
        verify_project_ownership
        volume=$(managed_resource_name volume emqx-data)
        write_auth
        compose rm -sf emqx
        existing=$(docker volume ls --format '{{.Name}}')
        if printf '%s\n' "$existing" | grep -Fxq -- "$volume"; then
            docker volume rm "$volume"
        fi
        compose up -d emqx
        ;;
    audit)
        container=${EMQX_CONTAINER:-hummingbot-broker}
        printf '\nListeners\n'
        docker exec "$container" /opt/emqx/bin/emqx ctl listeners
        printf '\nPublished ports\n'
        docker port "$container"
        printf '\nAuthentication\n'
        docker exec "$container" /opt/emqx/bin/emqx ctl conf show authentication
        printf '\nAuthorization (expected no_match = deny)\n'
        docker exec "$container" /opt/emqx/bin/emqx ctl conf show authorization
        printf '\nACL rules\n'
        docker exec "$container" sh -c 'grep -E "^\{" /opt/emqx/etc/acl.conf'
        printf '\nPersisted rules, actions, connectors and bridges (review any entries)\n'
        docker exec "$container" sh -c \
            'if [ ! -e /opt/emqx/data/configs/cluster.hocon ]; then echo "No persisted dynamic configuration."; exit 0; fi
             awk '\''/^(actions|connectors|bridges|rule_engine) \{/{p=1} p{print} /^\}/{if(p){p=0;print ""}}'\'' /opt/emqx/data/configs/cluster.hocon'
        ;;
    *) fail 'Usage: bash scripts/shell/emqx.sh {auth|audit|auth-reset}'; exit 1 ;;
esac
