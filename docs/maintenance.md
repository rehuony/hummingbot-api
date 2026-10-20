# Personal maintenance

This repository serves the owner's daily trading workflow. It is maintained at
[rehuony/hummingbot-api](https://github.com/rehuony/hummingbot-api/tree/main) on `main`.
External Issues, feature requests, PRs, and community support are not accepted.
GitHub Issues, Pull requests, Discussions, and individual commit comments are disabled.
Keep the current public visibility and MIT license, including the Hummingbot Foundation copyright.

## Provenance and independence

The original source is [hummingbot/hummingbot-api](https://github.com/hummingbot/hummingbot-api).
The verified baseline is
[`7ae108f11bf20651682dbdff639a8717068c5883`](https://github.com/hummingbot/hummingbot-api/commit/7ae108f11bf20651682dbdff639a8717068c5883),
committed on 2026-10-15 at 17:01:44 UTC:
“Merge pull request #246 from hummingbot/fix/210-position-mode-no-pair”.

Before migration, `custom` pointed to `f76260a0b0a2b1cc9d6ce4c43bf988953a48ad03`,
whose sole parent was that baseline. Its earlier local variants had the same parent.
The local reflog records the previous rebase onto that commit on 2026-10-16; the
original GitHub commit confirms its identity and timestamp. This evidence establishes
the current code's baseline independently of the latest upstream branch.

The independence migration consolidates the existing customizations and migration
changes into one child of that baseline, preserving all earlier history. This is a
one-time operation, not the future maintenance model. There is no upstream remote,
periodic synchronization, upstream merge, or upstream rebase process. Removing a Git
remote and leaving GitHub's fork network are separate operations.

## Development and publication

Use the existing Conda environment described by `environment.yml`:

```bash
make install
conda run -n hummingbot-api pytest -q
```

`make install` creates the environment if missing and installs the commit hooks. Run
`make setup` separately to initialize local configuration. Installation does not update
an already existing environment. After an intentional
dependency change, update it explicitly with `conda env update -n hummingbot-api -f environment.yml`.
There is no tracked lockfile today. Do not invent a lockfile replacement or a private package
source. Hummingbot core comes from PyPI; other channels and external images remain documented
in `environment.yml`, `Dockerfile`, and the Compose files.

`make run` starts local PostgreSQL and EMQX dependencies and runs development uvicorn,
potentially configuring Tailscale. It is an operational action, not a test command.
Use mocks and temporary data for tests. Do not verify changes using real trades.
The existing `integration` tests require live services and can create trading executors.
`pyproject.toml` excludes that marker by default; do not enable it for routine validation or CI.

For subsequent changes:

```bash
git switch main
make update
# Edit and run the checks relevant to the change.
git add <reviewed-files>
git commit -m "fix: describe the focused change"
git push origin main
```

Ordinary commits accumulate on `main`. Preserve published history. Do not amend the
migration commit for each change or use force-push as the normal workflow. If another
checkout has advanced `main`, inspect the divergence and reconcile your work deliberately.
`make update` must refuse dirty trees, the wrong branch, and the wrong repository.

## Build and validation

```bash
make build
```

This builds `rehuony/hummingbot-api:main` from the current checkout using the existing
multi-stage Conda Dockerfile. It does not start or restart containers, run migrations,
publish a package, or upload an image. Compose uses this local image with
`pull_policy: never`; it cannot replace the personal API with an official image.
The name is a local tag, not a published registry release. Existing Hummingbot bot,
Gateway, MCP, PostgreSQL, EMQX, and Tailscale images are separate dependencies.

Each build records the source URL, full Git SHA, actual branch, working-tree modification
state, and UTC build time. Dockerfile labels explicitly identify this project instead of
inheriting the base image's title, version, source, revision, creation time, or license.
`make image-info` inspects the image currently assigned to the local tag. A running
container continues using its original immutable image until explicitly recreated;
authenticated `GET /system/info` reports that image's metadata without network requests.
Image IDs identify local images; registry digests may be absent for local builds and are
never replaced with image IDs. The recorded build time is the UTC start of the build;
Docker's image creation timestamp is reported separately and can retain a cached image timestamp.
Missing or unrecognized build metadata is reported as unknown.

Use `make build API_IMAGE=rehuony/hummingbot-api:verify` and
`make image-info API_IMAGE=rehuony/hummingbot-api:verify` for an isolated validation tag.
Build, inspection, and deployment all honor `API_IMAGE` (shell override, then `.env`,
then the default `rehuony/hummingbot-api:main`).

CI runs on pushes to `main` and manual dispatch. It validates installation/deployment
configuration, builds `rehuony/hummingbot-api:ci`, and runs offline tests and fatal Python
lint checks in an isolated container. It does not publish images or deploy. Check the
CI run for the exact published SHA before scheduling a deployment.

Useful local static checks include:

```bash
for script in scripts/shell/*.sh scripts/shell/lib/*.sh; do bash -n "$script"; done
shellcheck -x scripts/shell/*.sh scripts/shell/lib/*.sh
git diff --check
conda run -n hummingbot-api python -m compileall -q main.py config.py services routers utils models database
conda run -n hummingbot-api flake8 --select E9,F63,F7,F82 .
```

Run the applicable formatting/import checks on changed Python files using the existing
isort/flake8 configuration. Validate Compose with an isolated dummy `.env`, never print
the resolved production configuration into logs. Check hidden files and tracked templates
as well as application code when auditing references.

## Existing installations

This migration changes source and repository settings only. Existing services continue
running their old code until an explicit deployment; no database or runtime configuration
is modified by the migration.

For a deployment checkout still on `custom`, save any local changes first. Fetch only
`origin`, verify it is this repository, then move to the new `main`:

Before the first branch switch, also save
`bots/credentials/master_account/conf_client.yml` privately outside the checkout.
It was formerly tracked, so switching history can remove an unchanged copy. Restore that
saved profile at the same path after the switch, before any setup or deployment. Subsequent
updates on `main` leave this ignored runtime file alone. The checkout used for this migration
has already retained its original profile byte-for-byte.

```bash
git remote -v
git fetch origin --prune
git switch --track origin/main
```

If a local `main` already exists from the old upstream flow, preserve it and its working
tree privately before replacing it. A safe alternative is to clone `main` into a separate
directory for inspection, then schedule the deployment transition. Do not blindly run
`git reset --hard` or copy a new `.env` over existing credentials.

Review any `docker-compose.override.yml`, `COMPOSE_FILE`, deployment automation, or
external checkout that selected `custom` or the official API image. An override takes
precedence over the base Compose file. Keep the existing Compose project name and volume
bindings: changing the checkout directory can otherwise attach a new empty database.

Before first deploying the new code, stop trading activity deliberately, back up the
database, `.env`, bot credentials, certificates, and other persistent state privately.
Existing running bot configurations may still enable remote reporting. The API disables
it for newly generated instances; review and redeploy existing bots separately.
`templates/master_account/conf_client.yml` supplies defaults for a new installation.
[`scripts/shell/setup.sh`](../scripts/shell/setup.sh) copies it only when the local profile is missing. The runtime
`bots/credentials/master_account/conf_client.yml` is ignored, so editing that profile
does not become a source change or get overwritten by normal updates.

## Update and deployment

On an already migrated, clean deployment checkout:

```bash
make check-update
make update
# Review the source/CI and prepare a database and configuration backup.
make deploy
make image-info
make doctor
```

`make help` lists available commands and their effects. `make check-update` validates
`origin`, fetches `origin/main`, and compares it with the current checkout. It reports
equal, behind, ahead, or diverged histories and any local modifications. It changes
only Git remote metadata, leaving the current branch and working tree untouched.

For a running API, authenticated `GET /system/update-check` compares its recorded build
commit with `main` at `rehuony/hummingbot-api` through GitHub. It reports an update only
when the remote commit descends from the clean running build. Modified builds, missing
metadata, ahead or diverged histories, and network failures have explicit reasons.
The check runs on request with a timeout; it does not update source, pull images,
deploy, restart, or provide a self-upgrade endpoint. Condor's existing upgrade UI
requires separate adaptation to this interface.

`make update` fast-forwards source from this repository's `origin/main` only. It does not
install dependencies, build, restart, or migrate anything. `make build` builds the image.
`make deploy` checks configuration, builds current source, generates broker bootstrap
credentials, then starts or recreates services and may configure tailnet access. Every
invocation builds first; a build failure leaves running services unchanged. The API's
startup calls `AsyncDatabaseManager.create_tables()` in `database/connection.py`,
including its lightweight schema changes and removal of legacy Hummingbot tables.
There is no separate Alembic migration command. Review this startup behavior and back up
the database before deploying changes to persistence.

A Git commit records source; a push publishes source; CI checks source; a build produces
an image; deployment applies that image/configuration; startup may migrate the database.
A restart uses the container's current image and does not fetch or build new source.
`make doctor` and local logs diagnose the running service without changing its data.
None of these operations proves that another host has been updated.

## Operational scripts and resource ownership

Makefile targets delegate to `scripts/shell/`; reusable shell functions and their AWK
parser live in `scripts/shell/lib/`. Python tools live in `scripts/python/`, and database
initialization SQL lives in `scripts/sql/`. See the [script directory guide](../scripts/README.md)
for entry points and naming conventions. Each entry point locates the checkout from its own path, so direct
calls such as `bash /path/to/hummingbot-api/scripts/shell/setup.sh` work from another directory.
External installers must use this new setup path. Setup only creates configuration;
`make pull-bot-image` handles bot image preparation and `make deploy` builds the API.

PostgreSQL publishing is part of `docker-compose.yml`; remove references to the former
source overlay from local automation. Existing `.env` files remain valid with localhost
defaults, and may explicitly set the variables listed in the
[network guide](user-guide.md#network-bindings). Preserve the Compose project name and
existing volume identities when moving a checkout. SQL initialization now mounts
`scripts/sql/init-db.sql` and still runs only when PostgreSQL initializes a new data volume.

`make stop` handles both the base stack and its Tailscale sidecar, even if Tailscale has
subsequently been disabled in `.env`. It preserves persistent volumes. Lifecycle commands
verify container checkout ownership and the project/resource labels of existing named
volumes and networks before changing resources. A mismatch requires inspection, not
renaming or deleting a foreign resource. External volumes and networks remain unmanaged.
Lifecycle commands select the repository's
Compose files and include `docker-compose.override.yml` when present. Review that override
before deployment; `COMPOSE_FILE` is rejected to keep lifecycle operations on this stack.

`make emqx-auth-reset` recreates the project's broker and removes its currently resolved
`emqx-data` volume; it refuses external broker data and leaves historical volumes alone.
`make reset` verifies teardown before removing
local configuration and credential files. If Docker is unavailable, it refuses unless the
operator explicitly selects `ALLOW_PARTIAL_RESET=1`; that option cannot claim Docker data
was removed. Neither reset command is a routine validation or update step.

A foreground development session tracks its supervisor and dedicated process group
per checkout. Stopping it terminates its reload workers as well as its launcher.
A surviving process group continues to block deployment and reset even if the supervisor
has exited. An incomplete session record also blocks these operations; after a forced
termination, verify that its workers have stopped before manually removing the record.
Stop the session before resetting; reset never kills a process merely
because its command contains `uvicorn main:app`. Other checkouts and their processes
must remain untouched.

## Recovery

Prefer a normal revert commit:

```bash
git revert <bad-commit>
# Validate the revert.
git push origin main
# Deploy deliberately after reviewing schema compatibility and backups.
make deploy
```

A revert restores source behavior; it cannot undo database migrations, executed trades,
external exchange state, or changes in persistent configuration. Keep a known-good local
image identified by its image ID/digest before replacing a build if rapid service recovery
is needed. Restore data only from a verified backup under a separate explicit recovery plan.

Never use `docker compose down -v` as a generic repair. `make reset` deletes configuration,
credentials, and volumes; `make emqx-auth-reset` deletes broker state. These are destructive
administrative tools, not update or rollback steps.

The migration's private recovery backup is outside the repository under
`~/.codex/backups/hummingbot-api/20261020T022559Z/`. It contains an all-refs Git bundle,
the complete original `.git` directory (including reflogs), tracked/nonignored working
files, binary working-tree/index patches, and repository metadata. It is owner-readable
only and must never be published. Ignored runtime configuration/data stayed untouched
in the original checkout.

Inspect recovery in a new directory:

```bash
git clone ~/.codex/backups/hummingbot-api/20261020T022559Z/all-refs.bundle /path/to/recovery
```

For exact refs/reflogs and any original uncommitted files, extract `working-tree.tar.gz`
and `git-directory.tar.gz` into a new empty directory instead. Do not overwrite the
live checkout or force-push recovered history as a routine rollback.
