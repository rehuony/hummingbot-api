# Repository scripts

Use `make help` from the repository root for routine commands. Scripts are grouped
by language; shell entry points resolve the checkout from their own location.

```text
scripts/
├── shell/
│   ├── setup.sh
│   ├── source-update.sh
│   ├── build-image.sh
│   ├── image-info.sh
│   ├── stack.sh
│   ├── dev.sh
│   ├── doctor.sh
│   ├── emqx.sh
│   ├── reset.sh
│   └── lib/
│       ├── common.sh
│       ├── compose-resources.awk
│       ├── read-env.awk
│       └── tailnet-state.sh
├── python/
│   ├── backfill_gas_tokens.py
│   ├── dev_session.py
│   └── validate_dev_config.py
└── sql/
    └── init-db.sql
```

| Directory | Responsibility |
| --- | --- |
| `shell/` | Configuration, source updates, image builds, service lifecycle and diagnostics |
| `shell/lib/` | Shared shell functions and private AWK configuration/resource parsers |
| `python/` | Maintenance tools, development session supervision and configuration validation |
| `sql/` | PostgreSQL initialization, mounted by Compose |

Shell and SQL filenames use lowercase words separated by hyphens. Python filenames
use underscores so tools can also be addressed as Python modules. Shared shell files
are sourced by the entry points; `shell/dev.sh` invokes `validate_dev_config.py` and
`dev_session.py` using the existing Conda environment.

External installers initialize configuration with:

```bash
bash /path/to/hummingbot-api/scripts/shell/setup.sh
```

The gas-token repair tool uses the project's Conda environment. From the repository
root, inspect its options with:

```bash
conda run --no-capture-output -n hummingbot-api python -m scripts.python.backfill_gas_tokens --help
```

For direct execution from another directory, pass the absolute path to
`scripts/python/backfill_gas_tokens.py`. Database repair requires an intentional
maintenance operation; moving the script does not run it.

When moving an entry point, update its callers, repository-root discovery, Makefile,
CI, test fixtures, documentation, and external installers together. SQL moves also
require updating the Compose mount. Keep operational details in the
[maintenance guide](../docs/maintenance.md) and usage in the
[user guide](../docs/user-guide.md).
