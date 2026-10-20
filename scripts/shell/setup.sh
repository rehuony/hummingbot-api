#!/bin/bash
# Initialize local API configuration and seed a missing default client profile.

set -euo pipefail

echo "Hummingbot API Setup"
echo ""

prompt_tty() {
  local message="$1"
  local default_value="${2:-}"
  local value=""
  local fd
  if [[ -t 0 ]]; then
    read -r -p "$message" value
  elif { exec {fd}<>/dev/tty; } 2>/dev/null; then
    printf '%s' "$message" >&${fd}
    read -r value <&${fd}
    exec {fd}>&-
  elif IFS= read -r value; then
    :
  else
    value=""
  fi
  echo "${value:-$default_value}"
}

prompt_yes_no() {
  local message="$1"
  local default_value="${2:-n}"
  local value
  value="$(prompt_tty "$message" "$default_value")"
  [[ "$value" =~ ^[Yy]$ ]]
}

prompt_required_tty() {
  local message="$1"
  local value=""
  while true; do
    value="$(prompt_tty "$message" "")"
    if [[ -n "$value" ]]; then
      echo "$value"
      return 0
    fi
    echo "[WARN] This value cannot be empty"
  done
}

# Same as prompt_tty, but with echo off. These values end up in .env and guard
# an API that can place orders and read balances -- typing them in cleartext
# leaves them in the terminal, in scrollback, and in any recorded install log.
prompt_secret_tty() {
  local message="$1"
  local value=""
  local fd
  if [[ -t 0 ]]; then
    read -rs -p "$message" value
    echo "" >&2
  elif { exec {fd}<>/dev/tty; } 2>/dev/null; then
    printf '%s' "$message" >&${fd}
    read -rs value <&${fd}
    printf '\n' >&${fd}
    exec {fd}>&-
  elif IFS= read -r value; then
    :
  else
    value=""
  fi
  echo "$value"
}

# Masked, non-empty, typed twice -- a mistyped password that is never echoed
# is otherwise only discovered when the API refuses to authenticate.
prompt_required_secret_tty() {
  local message="$1"
  local value="" confirm=""
  while true; do
    value="$(prompt_secret_tty "$message")"
    if [[ -z "$value" ]]; then
      echo "[WARN] This value cannot be empty" >&2
      continue
    fi
    if env_value_unsafe "$value"; then
      echo "[WARN] Not supported here: spaces and  \` \$ \" ' \\ ; & | < > ( ) ~" >&2
      echo "[WARN] Use a single-line value without interpolation or quoting so all configuration readers agree." >&2
      continue
    fi
    confirm="$(prompt_secret_tty "Confirm: ")"
    if [[ "$value" != "$confirm" ]]; then
      echo "[WARN] Values did not match, try again" >&2
      continue
    fi
    echo "$value"
    return 0
  done
}

# Keep generated, unquoted values consistent across Compose and Python dotenv.
# Deployment scripts parse configuration; they never execute .env as shell code.
env_value_unsafe() {
  case "$1" in
    *[[:space:]]*|*'`'*|*'$'*|*'"'*|*"'"*|*'\'*|*';'*|*'&'*|*'|'*|*'<'*|*'>'*|*'('*|*')'*|*'~'*) return 0 ;;
    *) return 1 ;;
  esac
}

resolve_script_dir() {
  local src="${BASH_SOURCE[0]}"
  while [ -h "$src" ]; do
    local dir
    dir="$(cd -P "$(dirname "$src")" >/dev/null 2>&1 && pwd)"
    src="$(readlink "$src")"
    [[ "$src" != /* ]] && src="$dir/$src"
  done
  cd -P "$(dirname "$src")" >/dev/null 2>&1 && pwd
}

SCRIPT_DIR="$(resolve_script_dir)"

cd "$SCRIPT_DIR/../.."
# shellcheck source=scripts/shell/lib/tailnet-state.sh
. "$SCRIPT_DIR/lib/tailnet-state.sh"

# Runtime profiles are local state. Seed the shipped privacy defaults only when
# missing, including installations with an existing .env; never replace a profile.
client_profile="bots/credentials/master_account/conf_client.yml"
if [ ! -e "$client_profile" ] && [ ! -L "$client_profile" ]; then
  mkdir -p "$(dirname "$client_profile")"
  install -m 600 templates/master_account/conf_client.yml "$client_profile"
fi

if [ -f ".env" ]; then
  echo ".env file already exists. Skipping setup."
  echo ""
  echo "  To change credentials or Tailscale settings: edit .env, then 'make deploy'."
  echo "  To check the install:                        make doctor"
  echo ""

  exit 0
fi

# --------------------------
# Non-interactive mode
# --------------------------
# Lets another installer (Condor's setup-environment.sh, CI, a provisioning
# script) drive this setup without a human. It exists because the alternative
# -- a caller hand-writing .env itself -- is how the schema drifted before:
# a second author shipped BROKER_PASSWORD=password and omitted
# BROKER_DASHBOARD_PASSWORD entirely, leaving the broker with credentials that
# did not match its own bootstrap file.
#
# Only the values a human would TYPE are accepted here. Everything this script
# GENERATES -- both broker passwords above all -- stays generated in both
# modes, so a caller cannot supply a weak one. That is the whole point: .env
# has exactly one author regardless of who started the run.
#
# The prompts below read from /dev/tty, so they cannot be fed by a pipe; this
# is the supported way to answer them programmatically.
HBAPI_NONINTERACTIVE="${HBAPI_NONINTERACTIVE:-0}"

# Captured before the defaults below reset them -- these three are the only
# Tailscale settings a caller may supply, and the reset would otherwise
# silently discard them.
_env_ts_enabled="${TAILSCALE_ENABLED:-}"
_env_ts_key="${TAILSCALE_AUTH_KEY:-}"
_env_ts_host="${TAILSCALE_HOSTNAME:-}"
# Optional caller tuning. Emitted only when supplied, so a standalone install's
# .env is unchanged and this repo's own defaults keep applying.
_env_bt_concurrent="${HBAPI_BACKTESTING_MAX_CONCURRENT:-}"
_env_bt_timeout="${HBAPI_BACKTESTING_TIMEOUT_SECONDS:-}"

# Clear screen before prompting user (only if running interactively)
if [ "$HBAPI_NONINTERACTIVE" != "1" ] && [[ -t 0 ]] && [[ -c /dev/tty ]]; then
  # `clear` exits non-zero when TERM is unset (a tty without a termcap entry --
  # CI runners, `script` wrappers, some IDE terminals). Under `set -e` that
  # aborted the whole setup before a single prompt, so fall back to the escape
  # sequence rather than trusting the exit status.
  clear 2>/dev/null || printf "\033c"
fi

TAILSCALE_ENABLED=false
TAILSCALE_AUTH_KEY=""
TAILSCALE_HOSTNAME="hummingbot-api"

if [ "$HBAPI_NONINTERACTIVE" = "1" ]; then
  echo "Hummingbot API Setup (non-interactive)"
  echo ""
  # Fail loudly rather than writing a half-configured .env: these three guard
  # an API that can place orders and read balances, and there is no safe
  # default for any of them.
  : "${HBAPI_USERNAME:?HBAPI_NONINTERACTIVE=1 requires HBAPI_USERNAME}"
  : "${HBAPI_PASSWORD:?HBAPI_NONINTERACTIVE=1 requires HBAPI_PASSWORD}"
  : "${HBAPI_CONFIG_PASSWORD:?HBAPI_NONINTERACTIVE=1 requires HBAPI_CONFIG_PASSWORD}"
  USERNAME="$HBAPI_USERNAME"
  PASSWORD="$HBAPI_PASSWORD"
  CONFIG_PASSWORD="$HBAPI_CONFIG_PASSWORD"
  for _v in USERNAME PASSWORD CONFIG_PASSWORD; do
    _val="${!_v}"
    if env_value_unsafe "$_val"; then
      echo "[ERROR] $_v contains a character that is unsafe in .env: spaces or  \` \$ \" ' \\ ; & | < > ( ) ~" >&2
      echo "[ERROR] Use a single-line value without interpolation or quoting." >&2
      exit 1
    fi
  done

  # An explicit mode takes precedence over the legacy enabled switch.
  case "${TAILSCALE_MODE:-}" in
    none)           TAILSCALE_ENABLED=false ;;
    host|sidecar)   TAILSCALE_ENABLED=true ;;
    "")             case "$_env_ts_enabled" in
                      [Tt]rue|[Yy]es|1) TAILSCALE_ENABLED=true ;;
                      *)                TAILSCALE_ENABLED=false ;;
                    esac ;;
    *) echo "[ERROR] TAILSCALE_MODE must be none, host or sidecar." >&2; exit 1 ;;
  esac

  if [ "$TAILSCALE_ENABLED" = true ]; then
    TAILSCALE_AUTH_KEY="$_env_ts_key"
    TAILSCALE_HOSTNAME="${_env_ts_host:-hummingbot-api}"
    # An auth key registers a NEW node, so only the sidecar needs one. In host
    # mode we serve on a node this machine already has, and demanding a
    # credential for that would fail installs that are entirely correct --
    # which is exactly the co-located Condor case.
    if [ "${TAILSCALE_MODE:-}" != "host" ]; then
      if [ -z "$TAILSCALE_AUTH_KEY" ]; then
        echo "[ERROR] Tailscale requested but TAILSCALE_AUTH_KEY is empty" >&2
        echo "        (not needed when TAILSCALE_MODE=host — this host would reuse its own node)" >&2
        exit 1
      fi
      if [[ ! "$TAILSCALE_AUTH_KEY" =~ ^tskey-auth- ]]; then
        echo "[ERROR] TAILSCALE_AUTH_KEY must start with 'tskey-auth-'" >&2
        exit 1
      fi
    fi
  fi
  echo "[INFO] Credentials taken from the environment; broker passwords generated locally."
else

echo "Hummingbot API Setup"
echo ""
echo "Set API credentials (use a strong username, password, and config password):"
echo ""

USERNAME="$(prompt_required_tty "API username: ")"
PASSWORD="$(prompt_required_secret_tty "API password: ")"
CONFIG_PASSWORD="$(prompt_required_secret_tty "Config password: ")"

# --------------------------
# Tailscale Configuration
# --------------------------

if prompt_yes_no "Use Tailscale for secure private networking? [y/N]: " "n"; then
  TAILSCALE_ENABLED=true
  # Ask what the answer depends on before asking for an auth key. A machine
  # that already runs tailscaled needs no key -- it is on the tailnet, and
  # joining a second time from the same host would only contend for the same
  # device. Prompting anyway would demand a credential to do something we are
  # about to decide not to do.
  if [ "$(tailnet_state)" != none ]; then
    TAILSCALE_MODE=host
    echo ""
    echo "[OK] This machine is already on a tailnet — no auth key needed."
    echo "     Port 8000 will be served on the node it already has."
    echo "     Want the API to have its own tailnet identity instead?"
    echo "     Set TAILSCALE_MODE=sidecar and TAILSCALE_AUTH_KEY in .env, then 'make deploy'."
  else
    TAILSCALE_MODE=sidecar
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  How to get a Tailscale auth key:"
    echo "    1. Create a free account at https://tailscale.com"
    echo "    2. Go to: https://tailscale.com/admin/settings/keys"
    echo "    3. Click 'Generate auth key'"
    echo "    4. Check 'Reusable' for multiple server deployments"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    while true; do
      TAILSCALE_AUTH_KEY="$(prompt_tty "Tailscale auth key (tskey-auth-...): " "")"
      if [[ -z "$TAILSCALE_AUTH_KEY" ]]; then
        echo "[WARN] Auth key cannot be empty"
        continue
      fi
      if [[ ! "$TAILSCALE_AUTH_KEY" =~ ^tskey-auth- ]]; then
        echo "[WARN] Auth key must start with 'tskey-auth-'"
        continue
      fi
      break
    done
    # Hostname defaults to "hummingbot-api" — override via TAILSCALE_HOSTNAME in .env if needed
  fi
fi

fi  # end interactive / non-interactive split

# --------------------------
# Tailnet ownership
# --------------------------
# Recorded in .env as a preference; `make deploy` re-resolves it against the
# live host on every run, so this is a starting point, not a guarantee.
if [ "$TAILSCALE_ENABLED" != true ]; then
  TAILSCALE_MODE=none
elif [ -n "${TAILSCALE_MODE:-}" ]; then
  : # caller was explicit (validated earlier); respect it
else
  case "$(tailnet_state)" in
    native)
      TAILSCALE_MODE=host
      echo ""
      echo "[INFO] This machine is already on a tailnet."
      echo "[INFO] Port 8000 will be served on the node it already has, rather than"
      echo "[INFO] joining a second time — two daemons cannot share one tailnet device."
      echo "[INFO] Want a separate identity anyway? Set TAILSCALE_MODE=sidecar in .env."
      ;;
    sidecar)
      TAILSCALE_MODE=sidecar
      echo "[INFO] Reusing the existing hummingbot-tailscale sidecar."
      ;;
    *)
      TAILSCALE_MODE=sidecar
      ;;
  esac
fi

# Validate all caller-provided values before writing any configuration. Never
# echo their contents: a malformed value can contain credentials.
for _v in USERNAME PASSWORD CONFIG_PASSWORD TAILSCALE_AUTH_KEY TAILSCALE_HOSTNAME; do
  if env_value_unsafe "${!_v}"; then
    echo "[ERROR] $_v contains unsupported configuration characters." >&2
    exit 1
  fi
done
if [[ -n "$_env_bt_concurrent" && ! "$_env_bt_concurrent" =~ ^[1-9][0-9]*$ ]]; then
  echo "[ERROR] HBAPI_BACKTESTING_MAX_CONCURRENT must be a positive integer." >&2
  exit 1
fi
if [[ -n "$_env_bt_timeout" && ! "$_env_bt_timeout" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "[ERROR] HBAPI_BACKTESTING_TIMEOUT_SECONDS must be a non-negative number." >&2
  exit 1
fi

# Broker credentials are never typed by the user — the API and the bots are the only clients —
# so generate strong ones instead of shipping well-known defaults. Rotating BROKER_PASSWORD
# later requires `make emqx-auth-reset`, since EMQX only imports the bootstrap file for users
# it does not have.
#
# Two separate passwords, deliberately: BROKER_PASSWORD is the MQTT client credential handed
# to every bot instance's conf_client.yml, while BROKER_DASHBOARD_PASSWORD only unlocks the
# EMQX web dashboard (full broker admin — rules, connectors, the lot). Sharing one password
# between them would mean a single leaked bot config grants dashboard admin, not just the
# scoped MQTT access emqx/acl.conf intends.
#
# EMQX rejects single-character-class passwords for the dashboard, so compose the value from
# letters and digits explicitly rather than trusting a random alphanumeric draw to contain both
# — applied to both passwords here for one code path instead of two.
# `head -c N` exits as soon as it has its N bytes, so `tr` gets SIGPIPE on its next write —
# under `set -euo pipefail` that would otherwise abort setup before .env is written.
gen_password() {
    printf '%s%s' \
        "$(LC_ALL=C tr -dc 'A-Za-z' < /dev/urandom | head -c 24 || true)" \
        "$(LC_ALL=C tr -dc '0-9' < /dev/urandom | head -c 8 || true)"
}
BROKER_PASSWORD="$(gen_password)"
BROKER_DASHBOARD_PASSWORD="$(gen_password)"

umask 077
cat > .env << EOF
# Hummingbot API Configuration
USERNAME=$USERNAME
PASSWORD=$PASSWORD
CONFIG_PASSWORD=$CONFIG_PASSWORD
DEBUG_MODE=false

# MQTT Broker
BROKER_HOST=localhost
BROKER_PORT=1883
BROKER_USERNAME=admin
BROKER_PASSWORD=$BROKER_PASSWORD
BROKER_DASHBOARD_PASSWORD=$BROKER_DASHBOARD_PASSWORD

# Host-run API connection; native development needs an explicit database mapping.
# See docs/user-guide.md. Compose uses postgres:5432 internally.
DATABASE_URL=postgresql+asyncpg://hbot:hummingbot-api@localhost:5432/hummingbot_api

# Published ports (Compose). Bind to a specific interface for remote access.
# Port values must be 1-65535. Changing DB_PORT or BROKER_PUBLISHED_PORT also
# requires the corresponding DATABASE_URL or BROKER_PORT change for make run.
API_BIND=127.0.0.1
# Used only by the optional database override and native-development validation.
DB_BIND=127.0.0.1
DB_PORT=5432
BROKER_BIND=127.0.0.1
BROKER_PUBLISHED_PORT=1883
BROKER_DASHBOARD_BIND=127.0.0.1
BROKER_DASHBOARD_PORT=18083

# Gateway (optional). Reuses CONFIG_PASSWORD rather than a hardcoded default:
# this passphrase unlocks Gateway's DEX wallet keys, and "admin" is the first
# thing anyone who reaches the port will try.
GATEWAY_URL=https://localhost:15888
GATEWAY_PASSPHRASE=$CONFIG_PASSWORD

# Paths
BOTS_PATH=$(pwd)

# Tailscale
# TAILSCALE_ENABLED is the switch and keeps its historical meaning.
# TAILSCALE_MODE says HOW: host = share this machine's existing tailnet node,
# sidecar = give the API a node of its own. Deploy re-resolves this against
# the live host every time, so an .env that was right in June cannot put a
# second kernel-mode daemon on a box that grew one in July.
#
# NOTE: this heredoc is unquoted so plain variables expand. Command
# substitution in these comments would EXECUTE while .env is written -- a
# backtick here once ran a make target mid-setup and seeded the broker with
# the wrong password. Keep substitution out of the comments.
TAILSCALE_ENABLED=$TAILSCALE_ENABLED
TAILSCALE_MODE=$TAILSCALE_MODE
TAILSCALE_AUTH_KEY=$TAILSCALE_AUTH_KEY
TAILSCALE_HOSTNAME=$TAILSCALE_HOSTNAME

# ---------------------------------------------------------------------------
# Optional settings. Every line below is commented out and shows the default
# that config.py already applies -- config.py stays the source of truth, so
# uncomment a line only to override it.
# ---------------------------------------------------------------------------

# Performance snapshots
# How often a live executor's performance is snapshotted, in seconds.
#PERFORMANCE_EXECUTOR_SNAPSHOT_INTERVAL=60
# Delete performance snapshots (executor AND controller) older than this many
# days. 0 keeps everything forever -- raise it to cap database growth.
#PERFORMANCE_RETENTION_DAYS=0

# Backtesting
# How many backtests may run at once; further submissions queue. Runs are
# isolated in separate processes, so this can be raised up to the cores you are
# willing to give them.
#BACKTESTING_MAX_CONCURRENT=1
# Wall-clock budget for one backtest; the worker is killed and the task fails past it.
#BACKTESTING_TIMEOUT_SECONDS=1800.0
# How many finished backtests to retain before the oldest are reaped.
#BACKTESTING_MAX_RESULTS=100
# Directory holding archived backtest payloads (inside the bots volume, so it
# survives redeploys).
#BACKTESTING_RESULTS_PATH=bots/data/backtests
# Directory holding downloaded candle history shared by backtest workers, so a
# sweep of configs over one market downloads that market once.
#BACKTESTING_CANDLES_CACHE_PATH=bots/data/backtests/candles
# How many downloaded candle ranges to keep; the least recently used are dropped
# past it. 0 disables the cache and makes every run download its own history.
#BACKTESTING_CANDLES_CACHE_ENTRIES=32
# How long a downloaded candle range may be reused. A window ending near now is
# fetched with its last candle still forming, so an entry is refetched once it is
# older than this.
#BACKTESTING_CANDLES_CACHE_TTL_SECONDS=3600.0

# Market data feeds
# How often to run feed cleanup, in seconds.
#MARKET_DATA_CLEANUP_INTERVAL=300
# How long to keep unused feeds alive, in seconds.
#MARKET_DATA_FEED_TIMEOUT=600
# How long to wait for a candle feed to become ready, in seconds.
#MARKET_DATA_CANDLES_READY_TIMEOUT=30
# REST historical ranges: shared cache with a strict freshness and entry bound
#MARKET_DATA_HISTORICAL_CACHE_PATH=data/market_candles
#MARKET_DATA_HISTORICAL_CACHE_ENTRIES=128
#MARKET_DATA_HISTORICAL_CACHE_TTL_SECONDS=15.0
# WebSocket heartbeat interval, in seconds.
#MARKET_DATA_WS_HEARTBEAT_INTERVAL=30
# Allowed range for a market-data WebSocket subscription update interval, in seconds.
#MARKET_DATA_WS_MIN_UPDATE_INTERVAL=0.25
#MARKET_DATA_WS_MAX_UPDATE_INTERVAL=60.0
# Allowed range, and the applied default, for a /ws/executors subscription
# update interval, in seconds. The floor is stricter than the market-data one
# because executor push loops hit the database instead of reading in-memory
# candles and order books.
#MARKET_DATA_WS_EXECUTOR_MIN_UPDATE_INTERVAL=0.5
#MARKET_DATA_WS_EXECUTOR_MAX_UPDATE_INTERVAL=60.0
#MARKET_DATA_WS_EXECUTOR_DEFAULT_UPDATE_INTERVAL=2.0
# How often to refresh tickers from connected exchanges, in seconds.
#MARKET_DATA_TICKER_UPDATE_INTERVAL=30
# Max age of cached tickers before an on-demand request refetches them, in seconds.
#MARKET_DATA_TICKER_MAX_AGE=60
# How long a ticker-only connector stays in the background refresh cycle after
# its last request, in seconds.
#MARKET_DATA_TICKER_SUBSCRIPTION_TTL=600
# How often to refresh Hyperliquid HIP-3 (builder-deployed perp dex) tickers, in
# seconds. Set to 0 to exclude HIP-3 markets.
#MARKET_DATA_HYPERLIQUID_HIP3_INTERVAL=120

# CORS (SEC-019). A wildcard origin must never be combined with credentials, so
# trusted origins are restricted to localhost by default.
# Explicit list of trusted origins, as JSON, e.g. ["https://dashboard.example.com"]
#CORS_ALLOW_ORIGINS=[]
# Regex matching trusted origins; an empty string disables regex matching.
#CORS_ALLOW_ORIGIN_REGEX=https?://(localhost|127\.0\.0\.1)(:\d+)?
# Allow credentialed (cookies/auth) cross-origin requests.
#CORS_ALLOW_CREDENTIALS=true
# HTTP methods and headers allowed for cross-origin requests.
#CORS_ALLOW_METHODS=["*"]
#CORS_ALLOW_HEADERS=["*"]

# AWS (only needed for S3 archiving)
#AWS_API_KEY=
#AWS_SECRET_KEY=
#AWS_S3_DEFAULT_BUCKET_NAME=
EOF

# Appended rather than templated: these are the caller's tuning, not this
# repo's defaults, and an install that did not ask for them should not carry
# them at all.
if [ -n "$_env_bt_concurrent" ] || [ -n "$_env_bt_timeout" ]; then
  {
    echo ""
    echo "# Backtesting (set by the installer that drove this setup)"
    if [ -n "$_env_bt_concurrent" ]; then echo "BACKTESTING_MAX_CONCURRENT=$_env_bt_concurrent"; fi
    if [ -n "$_env_bt_timeout" ]; then echo "BACKTESTING_TIMEOUT_SECONDS=$_env_bt_timeout"; fi
  } >> .env
fi

# Holds the API password, the config/Gateway passphrase and (when set) a
# Tailscale auth key. The usual 644 umask makes all of that readable by every
# other account on the box.
chmod 600 .env 2>/dev/null || true

echo ""
echo ".env created successfully!"
echo ""
echo "Next steps:"
echo "  make deploy          # Build the current API source, then start services"
echo "  make pull-bot-image  # Prepare the Hummingbot image before creating bots"
echo "  make doctor          # Check configuration, services and connectivity"
echo ""
echo "For source development (Conda must already be installed):"
echo "  make install         # Create the Conda environment and install hooks"
echo "  make run             # Start dependencies and run the API in the foreground"
if [ "$TAILSCALE_ENABLED" = true ]; then
  echo ""
  echo "Tailscale mode: $TAILSCALE_MODE"
  echo "Run make doctor after deployment to check the actual node name and forwarding."
fi
