# User guide

See the [README](../README.md) for installation and routine commands, and
[maintenance](maintenance.md) for publication, deployment, migration, and recovery.

## Services

After hummingbot-api is running, these services are available:

| Service | Local URL | Tailnet URL (when Tailscale enabled) | Description |
| --- | --- | --- | --- |
| API | [localhost:8000](http://localhost:8000) | `http://hummingbot-api:8000` | REST API |
| API documentation | [localhost:8000/docs](http://localhost:8000/docs) | `http://hummingbot-api:8000/docs` | Interactive API schema |
| PostgreSQL | — | — | Internal database at `postgres:5432` on the Compose network |
| EMQX | `127.0.0.1:1883` | — | MQTT broker (authentication required) |
| EMQX dashboard | [127.0.0.1:18083](http://127.0.0.1:18083) | — | Broker admin (`admin` / `BROKER_DASHBOARD_PASSWORD`) |

Use the actual MagicDNS hostname reported by `make doctor` if it differs from the example.

### Broker credentials

The broker requires a username and password and is published on loopback by default.
Bot containers use `network_mode: host` and reach the published address, normally
`127.0.0.1:1883`; the containerized API reaches it in-network as `emqx:1883`. Credentials come from `BROKER_USERNAME` /
`BROKER_PASSWORD` in `.env`; `make deploy` seeds them into the broker via `make emqx-auth`.
EMQX imports the bootstrap file only for new accounts, so editing `.env` alone does not
rotate an existing account. For an intentional credential rotation, edit `.env` and run
`make emqx-auth-reset`. This deletes `emqx-data`, losing retained messages and broker
state before recreating the broker with the current credentials.

The dashboard login is a **separate** credential, `BROKER_DASHBOARD_PASSWORD` — deliberately
not the same value as `BROKER_PASSWORD`, since that one is written into every bot instance's
`conf_client.yml` and the dashboard grants full broker admin (rules, connectors), not just the
scoped MQTT access `emqx/acl.conf` gives `BROKER_PASSWORD`. Like the MQTT bootstrap account,
EMQX only applies it on first init, so changing it in `.env` also needs `make emqx-auth-reset`
(it wipes the same `emqx-data` volume, rotating both passwords together).

The broker also denies every topic outside `hbot/#` and `hummingbot-api/response/#`
([`emqx/acl.conf`](../emqx/acl.conf)), so a leaked broker credential cannot be used to read the whole bus or to
drive the rule engine. Run **`make emqx-audit`** to print the broker's listeners, auth,
authorization and any rules, actions, connectors or bridges — a rule nobody added can make
the broker issue authenticated HTTP requests into internal services, and it survives
restarts. Use `make emqx-audit EMQX_CONTAINER=<name>` to check another deployment.

### Network bindings

The main Compose file publishes the API and broker ports on localhost by default.
PostgreSQL has no host port mapping, so it can coexist with a host PostgreSQL service.
Configure bindings and host ports in `.env` when another interface or port is needed:

| Setting | Default | Purpose |
| --- | --- | --- |
| `API_BIND` | `127.0.0.1` | API binding; port remains 8000 |
| `DB_BIND`, `DB_PORT` | `127.0.0.1`, `5432` | Optional native-development database mapping (requires the override below) |
| `BROKER_BIND`, `BROKER_PUBLISHED_PORT` | `127.0.0.1`, `1883` | MQTT published endpoint |
| `BROKER_DASHBOARD_BIND`, `BROKER_DASHBOARD_PORT` | `127.0.0.1`, `18083` | EMQX dashboard published endpoint |

Port values must be in `1–65535`; an empty value or `0` does not disable publishing.
Prefer a specific interface address over `0.0.0.0`. For tailnet access, keep the normal
loopback bindings and use Tailscale forwarding, or deliberately bind the API to the
host's Tailscale IP. Publishing PostgreSQL or the broker externally is an explicit
configuration choice, separate from enabling remote API access.

Container-to-container connections remain `postgres:5432` and `emqx:1883` regardless
of published ports. New bots with the default localhost MQTT endpoint use the configured
published endpoint; an explicitly customized profile endpoint is preserved. Source
profiles and already running bots are not rewritten.

`make run` runs the API on the host and requires a host-reachable database. To use the
Compose database for native development, explicitly add this mapping to a local
`docker-compose.override.yml`, which the Make commands already load:

```yaml
services:
  postgres:
    ports:
      - "${DB_BIND:-127.0.0.1}:${DB_PORT:-5432}:5432"
```

If the host already uses port 5432, set `DB_PORT=15432` (or another unused port) and
update `DATABASE_URL` to use the same host port. Setting `DB_BIND` / `DB_PORT` alone
does not publish PostgreSQL. This override also applies to deployment while present;
omit it for container-only database access.

When changing the published MQTT endpoint, also set the local API's `BROKER_HOST` /
`BROKER_PORT` to that endpoint. `make run` validates endpoint settings before starting
containers; it does not add port mappings or edit `.env`.

## Configuration

Setup writes deployment settings to `.env`. Keep it, bot credentials, certificates,
logs, and database backups private. Key settings:

| Setting | Purpose |
| --- | --- |
| `USERNAME`, `PASSWORD` | API HTTP Basic authentication |
| `CONFIG_PASSWORD` | Credential and Gateway wallet encryption |
| `DATABASE_URL` | PostgreSQL connection for the local API process |
| `GATEWAY_URL` | Gateway endpoint |
| `DEBUG_MODE` | Development logging and reload |
| `API_BIND` | API host binding; defaults to `127.0.0.1` |
| `DB_BIND`, `DB_PORT` | Optional database mapping and native-development endpoint validation |
| `BROKER_BIND`, `BROKER_PUBLISHED_PORT` | Host-network bot endpoint; Compose API uses `emqx:1883` internally |
| `PERFORMANCE_EXECUTOR_SNAPSHOT_INTERVAL` | Live executor snapshot interval in seconds; default `60` |
| `PERFORMANCE_RETENTION_DAYS` | Snapshot retention; `0` keeps all snapshots |
| `BACKTESTING_MAX_CONCURRENT` | Concurrent backtests; default `1`, with one core per backtest |

[`config.py`](../config.py) defines the application settings and defaults. The generated
`.env` also includes commented `PERFORMANCE_`, `BACKTESTING_`, `MARKET_DATA_`, `CORS_`,
and `AWS_` settings.

`make setup` initializes `.env` and a missing default client profile without installing
packages or pulling images. Existing files are preserved. Run it explicitly before a first
deployment or development session; `.setup-complete` is no longer used as a readiness check.

Operational scripts parse only the configuration keys they need; they do not execute `.env`.
These keys support single-line values, surrounding quotes, and comments, but not variable
interpolation, escape sequences, or multiline values. Unsupported syntax reports the key and
line without its value. Generated credentials use a conservative character set shared by
Compose and Python dotenv. Application settings continue to use the existing Python dotenv
loader. Exported Compose control variables take precedence over `.env`; keep overrides
intentional, especially the project name and published ports.

`API_IMAGE` selects the same local tag for build, inspection, and deployment. For example,
`make build API_IMAGE=rehuony/hummingbot-api:verify` builds an independent validation image.
The default is `rehuony/hummingbot-api:main`.

Apply intentional configuration changes with `make deploy`. Each deployment builds current
source before recreating containers; a failed build stops the deployment.

## Historical market data

Use `/market-data/historical-candles` for scans and research. It downloads REST pages
without starting a live candle feed. Concurrent requests for the same market and
interval-aligned window share one download; each response is trimmed to its requested
start/end timestamps. At most four historical downloads run concurrently. The disk cache
defaults to 128 ranges with a 15-second freshness bound, including forming candles:

```bash
#MARKET_DATA_HISTORICAL_CACHE_PATH=data/market_candles
#MARKET_DATA_HISTORICAL_CACHE_ENTRIES=128
#MARKET_DATA_HISTORICAL_CACHE_TTL_SECONDS=15.0
```

Set entries to zero to disable the cache. Failed or empty downloads are not cached.
Live `/market-data/candles` requests continue to use managed WebSocket feeds.

API-owned Binance connectors and candle feeds share a request-weight budget across
accounts, separately for spot, USD-M futures and test/US domains. The budget uses 80%
of the connector's configured IP allowance and retains its existing order throttles.
Futures candle pages use at most 499 rows (weight 2); requests below 100 rows use weight 1.
Bulk 24-hour ticker requests use weight 80 for spot and 40 for futures. The adapter preserves
the historical endpoint's inclusive end timestamp across pages, including UTC calendar weeks
and months.
See Binance's [USD-M market data documentation](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data).

HTTP 418/429 pauses the shared budget using `Retry-After` or the reported ban expiry.
Background REST requests wait; historical callers receive HTTP 429 with `Retry-After`.
High `X-MBX-USED-WEIGHT-1M` usage also pauses new requests, except on futures endpoints
whose headers Binance documents as inaccurate. Do not immediately retry
against the live-candles endpoint: it uses the same IP allowance.

This coordination is scoped to one API process. Independent bots, backtest workers and
scripts using Binance directly still consume the same egress IP's allowance; they must
budget their own traffic or request historical data through this API. Successful response
headers provide a brake for external usage, not a distributed reservation system.

## Tailscale

[Tailscale](https://tailscale.com) provides private access from other devices without
publicly exposing the API. Keep strong API credentials and public firewall restrictions
in place. A client running on the same host can use localhost without a separate API
tailnet node.

### Prerequisites

1. Create a free account at [tailscale.com](https://tailscale.com)
2. Go to **Settings → Keys**: [Settings → Keys](https://login.tailscale.com/admin/settings/keys)
3. Click **Generate auth key** — check **Reusable** for multiple deployments
4. Copy the key (starts with `tskey-auth-`)
5. Enable **[MagicDNS](https://login.tailscale.com/admin/dns)** in the Tailscale admin console

### Setup

Run `make setup` and enable Tailscale when prompted.

This adds the following to `.env`:

```bash
TAILSCALE_ENABLED=true
TAILSCALE_AUTH_KEY=tskey-auth-...
TAILSCALE_HOSTNAME=hummingbot-api   # MagicDNS hostname on your tailnet
```

### Deploy

```bash
make deploy
```

`make deploy` checks the current host's tailnet state and configured preference. It can
reuse an existing host node through `tailscale serve`, or start the sidecar defined in
[`docker-compose.tailscale.yml`](../docker-compose.tailscale.yml). If a native daemon
already owns the host's tailnet device, deployment uses userspace mode for the sidecar.

In sidecar mode, a Tailscale container joins the tailnet using `network_mode: host`.
The API's port 8000 remains bound to loopback by default (`API_BIND` in `.env`).
`tailscale-serve.json` forwards the tailnet's port 8000 to `127.0.0.1:8000`.
In host mode, the same forwarding uses the existing node's hostname. Run `make doctor`
to verify the actual node name and forwarding; an occupied requested hostname may gain a suffix.

### Dev mode

`make run` uses the configured Tailscale mode and `API_BIND`, which defaults to loopback.
Install and connect the host Tailscale client before selecting host mode; the command
does not install system software. Sidecar mode uses the existing Tailscale overlay.
Use `make tailscale-status` to verify forwarding after startup.

### Check status

```bash
make tailscale-status
```

This shows both tailnet peer status and `tailscale serve status` — confirm
port 8000 shows up as forwarded, not just that the node joined the tailnet.

From another authorized device on your tailnet, let curl prompt for the password:

```bash
curl --user YOUR_USERNAME http://hummingbot-api:8000/
```

## MCP

MCP clients use the separate `hummingbot/hummingbot-mcp:latest` image. When the client
runs on another device, use the API's actual MagicDNS hostname and ensure both devices
have access to the same tailnet.

### Claude Code (CLI)

```bash
claude mcp add --transport stdio hummingbot -- \
  docker run --rm -i \
  -e HUMMINGBOT_API_URL=http://hummingbot-api:8000 \
  -v hummingbot_mcp:/root/.hummingbot_mcp \
  hummingbot/hummingbot-mcp:latest
```

On Docker Desktop, an MCP container connecting to an API on the same host can use
`http://host.docker.internal:8000`. Host access from Linux containers depends on their
network configuration; use the reachable tailnet address or configure host access explicitly.

Requests such as “Show my portfolio balances” use the connected API.

### Claude Desktop

Add to your config file:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "hummingbot": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-e", "HUMMINGBOT_API_URL=http://hummingbot-api:8000",
        "-v", "hummingbot_mcp:/root/.hummingbot_mcp",
        "hummingbot/hummingbot-mcp:latest"
      ]
    }
  }
}
```

Restart Claude Desktop after adding.

## Bot image

Setup prepares configuration only. Pull the Hummingbot image explicitly before creating
bots, and again when intentionally updating their runtime:

```bash
make pull-bot-image
# Select a specific supported image when needed:
make pull-bot-image BOT_IMAGE=hummingbot/hummingbot:latest
```

This downloads a bot image and does not build or deploy the API. Select the matching
image when creating a bot through the API; the pull command does not rewrite profiles
or change running instances.

## Gateway

Gateway enables decentralized exchange trading. Start it through MCP with “Start
Gateway,” or use `POST /gateway/start` with an empty body in the
[API documentation](http://localhost:8000/docs).

Gateway uses TLS and mutual certificate authentication. Certificates are generated on
first start under `bots/gateway-files/certs/`; `CONFIG_PASSWORD` protects Gateway and
wallet encryption. There is no plain-HTTP or insecure mode. The local endpoint is
`https://localhost:15888`, and clients need the generated certificates.

## Development

```bash
make setup
make install
conda activate hummingbot-api
make run
```

Install Conda first. `make install` creates the Conda environment if missing and installs
commit hooks; it does not run setup or change an existing environment. After an intentional
dependency change, update the environment explicitly:

```bash
conda env update -n hummingbot-api -f environment.yml
```

`make run` starts database and broker services and may configure Tailscale. It runs the
API in the foreground; stop that session with Ctrl-C before resetting this checkout.
`make stop` stops the project's Compose services, including a sidecar, and retains data.
`make uninstall` removes the development Conda environment, not runtime configuration.
These are operational commands. See [build and validation](maintenance.md#build-and-validation)
for offline checks. Routine tests exclude the `integration` marker because those tests
require live services and can create trading executors.

## Troubleshooting

Start with `make doctor`. It checks dependencies, configuration, default credentials,
containers, exposed ports, Tailscale forwarding, authenticated API access, and MQTT
connectivity without changing runtime state.

| Symptom | Initial check |
| --- | --- |
| API fails to start | `docker compose logs hummingbot-api` |
| Database errors | `docker compose logs postgres hummingbot-api` |
| Missing bot status, controllers, logs, or performance | `make doctor`, then `docker compose logs emqx` |
| Tailnet connection fails | `make tailscale-status`; confirm MagicDNS and forwarding |
| Build details differ from the running service | `make image-info` and authenticated `GET /system/info` |
| Update comparison is unavailable | Check `GET /system/update-check`'s reason and the running build's metadata |

Bot status arrives over MQTT. A healthy API or broker container does not prove the
API authenticated to the broker. A `Permission denied` error for
`/opt/emqx/etc/auth-bootstrap.csv` means EMQX could not import the credentials.
Inspect this error before considering credential rotation.

Before changing a database, back it up and inspect the error. Startup applies the schema
steps in [`database/connection.py`](../database/connection.py); a Git revert does not
undo them. Follow [recovery](maintenance.md#recovery) instead of deleting volumes.

### Build and update details

`make image-info` describes the image currently assigned to the local tag.
Authenticated `GET /system/info` describes the immutable image used by the running
container and stays offline. Rebuilding a tag does not change an existing container;
the running image remains unchanged until deployment.

`container.build` contains `source`, `revision`, `branch`, `created`, and `dirty`.
Its `created` value is the UTC build start; `container.image_created` is Docker's
separate image timestamp and may be retained by the build cache. Native development runs and
containers whose identity cannot be verified return `container: null`.

A locally built image usually has no registry digest; its image ID identifies the local
image. Missing or unrecognized build metadata is reported as unknown. Authenticated
`GET /system/update-check` compares the running build with this repository's `main`
and reports reasons for unavailable or uncertain results. Condor needs separate support
for this endpoint and metadata; its existing Upgrade message does not indicate that
the API can upgrade itself.

The update response contains `status`, `current_revision`, `latest_revision`,
`checked_at`, `reason`, and `compare_url`. Revisions and the comparison link can be
null when the check cannot establish them.

| Status | Meaning |
| --- | --- |
| `up_to_date` | The clean running build matches remote `main` |
| `update_available` | Remote `main` descends from the clean running build |
| `ahead` | The running source is ahead of remote `main` |
| `diverged` | The two source histories have diverged |
| `unknown` | Missing or untrusted metadata, a modified build, or an unavailable source commit |
| `error` | Timeout, network failure, GitHub rate limit, or an invalid response |

Checks run only on request. Neither missing registry digests nor the absence of the
removed `/system/upgrade` routes means that a newer API can upgrade itself.

## Privacy and local operation

The API does not initialize trade-volume telemetry or Logfire exporters. Bot configurations
created by this API disable remote error reporting and anonymized trade-volume metrics, including
when an old credential profile enables them. Existing running bots and their configurations are
not changed by a Git update; redeploy them deliberately after reviewing their configuration.
Local logs, authenticated health/resource endpoints, error messages, trading records, and
performance analysis remain available. Optional S3 archives use the owner's configured bucket.

There is no feedback UI, conversation-sharing service, or replacement feedback collector in this
API. Version reporting stays offline. The explicit `/system/update-check` request contacts
GitHub to compare source commits; it does not poll in the background, pull a registry
image, or upgrade the API. Updates require an explicit local build and deployment.
