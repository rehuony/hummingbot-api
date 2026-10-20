#!/usr/bin/env bash
# Inspect a local tag; the API's /system/info describes the running image instead.
set -euo pipefail
# shellcheck source=scripts/shell/lib/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

template=$(cat <<'EOF'
Image ID: {{.Id}}
Registry digests: {{json .RepoDigests}}
Image created: {{.Created}}
{{if eq (index .Config.Labels "org.opencontainers.image.source") "https://github.com/rehuony/hummingbot-api"}}Source: {{index .Config.Labels "org.opencontainers.image.source"}}
Revision: {{or (index .Config.Labels "org.opencontainers.image.revision") "unknown"}}
Branch: {{or (index .Config.Labels "io.rehuony.hummingbot-api.branch") "unknown"}}
Modified: {{or (index .Config.Labels "io.rehuony.hummingbot-api.dirty") "unknown"}}
Build time: {{or (index .Config.Labels "org.opencontainers.image.created") "unknown"}}
API version: {{or (index .Config.Labels "org.opencontainers.image.version") "unknown"}}
{{else}}API build metadata: unavailable (missing project provenance)
{{end}}
EOF
)

image=${1:-$(config_get API_IMAGE rehuony/hummingbot-api:main)}
printf 'Local image: %s\n' "$image"
exec docker image inspect "$image" --format "$template"
