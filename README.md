# Hummingbot API

A REST API for managing a personal Hummingbot trading workflow across multiple exchanges, with MCP integration for AI assistants.

- Track portfolio balances, positions, and performance.
- Place orders and manage trading bots and strategies.
- Retrieve prices, order books, candles, and funding rates.
- Connect to decentralized exchanges through Gateway.

## Installation

Install Git, Make, and Docker with the Compose v2 plugin, then start Docker.

```bash
git clone --branch main https://github.com/rehuony/hummingbot-api.git
cd hummingbot-api
make setup
make deploy
```

Setup creates local configuration and prompts for API credentials, the credential-encryption
password, and optional Tailscale access. It preserves existing configuration and installs
no software. Deployment builds `rehuony/hummingbot-api:main` from the current checkout,
then starts the API, PostgreSQL, and EMQX. Compose uses the local image with
`pull_policy: never`. See [bot image preparation](docs/user-guide.md#bot-image) before creating bots.

For an existing installation, preserve `.env`, bot credentials, database volumes, and the
Compose project name. See [existing installations](docs/maintenance.md#existing-installations)
before switching an older checkout to `main`.

## Access

| Service | Local address |
| --- | --- |
| API | [http://localhost:8000](http://localhost:8000) |
| Interactive API documentation | [http://localhost:8000/docs](http://localhost:8000/docs) |
| EMQX dashboard | [http://127.0.0.1:18083](http://127.0.0.1:18083) |

API requests use the username and password configured during setup. The EMQX dashboard
uses `admin` and the separate `BROKER_DASHBOARD_PASSWORD`.

The API binds to loopback by default. For another device, configure
[Tailscale access](docs/user-guide.md#tailscale) and use the actual node's MagicDNS
hostname, usually `http://hummingbot-api:8000`. Keep port 8000 closed on public interfaces.

## Common commands

| Command | Purpose |
| --- | --- |
| `make help` | List commands and their effects; also the default target |
| `make setup` | Initialize local configuration |
| `make check-update` | Compare this checkout with `origin/main` |
| `make update` | Fast-forward a clean `main` checkout |
| `make build` | Build the local API image from the current checkout |
| `make image-info` | Inspect the image currently assigned to the local tag |
| `make deploy` | Build the current checkout, then start or recreate services |
| `make stop` | Stop this Compose project, including its Tailscale sidecar; preserve data |
| `make doctor` | Check configuration, services, API access, and broker connectivity |
| `make tailscale-status` | Show tailnet connection and forwarding status |
| `make install` | Create the Conda development environment and install hooks |
| `make run` | Validate local connection settings, start dependencies, and run the API with reload |
| `make pull-bot-image` | Pull the Hummingbot bot image explicitly |

## Updates

```bash
make check-update
make update
# Review changes and CI; back up persistent data before deployment.
make deploy
make image-info
make doctor
```

`check-update` fetches Git metadata and reports whether the checkout is current, behind,
ahead, or diverged; it also reports local modifications. `update` requires a clean `main`
branch and the correct `origin`, and only accepts a fast-forward.

Checking or updating source leaves running services unchanged. Use `make build` to
build without deploying; `make deploy` always builds first and stops if the build fails.
The image name is a local tag, not a published registry release.

The authenticated `GET /system/info` reports the running image and its build metadata
without network requests. `GET /system/update-check` checks that running build against
this repository's `main` on GitHub. It reports unavailable or uncertain comparisons
explicitly and does not upgrade the service. A rebuilt local tag can differ from the
image used by a running container until deployment.

See [update and deployment](docs/maintenance.md#update-and-deployment) for validation,
startup schema changes, and recovery.

## Documentation

- [User guide](docs/user-guide.md): configuration, MCP, Gateway, Tailscale, development, and troubleshooting.
- [Maintenance](docs/maintenance.md): validation, publication, deployment, migration, and recovery.
- [API schema](http://localhost:8000/docs): endpoints and request models for the running server.
- [Agent instructions](AGENTS.md): repository engineering and operational conventions.

## Source and license

Independently maintained at [rehuony/hummingbot-api](https://github.com/rehuony/hummingbot-api)
on `main` for personal use. External Issues, pull requests, and community support are
not accepted. Maintenance uses focused commits and ordinary pushes to `origin/main`.

Original project: [hummingbot/hummingbot-api](https://github.com/hummingbot/hummingbot-api).
The verified development baseline is
[`7ae108f11bf20651682dbdff639a8717068c5883`](https://github.com/hummingbot/hummingbot-api/commit/7ae108f11bf20651682dbdff639a8717068c5883),
committed on **2026-10-15 at 17:01:44 UTC**:
“Merge pull request #246 from hummingbot/fix/210-position-mode-no-pair”.
Baseline verification and the one-time consolidation are recorded in
[maintenance](docs/maintenance.md#provenance-and-independence).

Hummingbot core, Gateway, and MCP remain external dependencies. The
[original API documentation](https://hummingbot.org/hummingbot-api/) provides additional
reference material; installation and updates for this repository follow the steps above.

Licensed under the [MIT License](LICENSE). Copyright © 2023 Hummingbot Foundation;
original attribution is retained.
