"""Validate local development endpoints without Docker or application imports."""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/python/validate_dev_config.py"
SPEC = importlib.util.spec_from_file_location("validate_dev_config", SCRIPT)
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


@pytest.mark.parametrize(
    "host,port,bind,published",
    [
        ("127.0.0.2", 2883, "127.0.0.1", 1883),
        ("127.0.0.2", 1883, "127.0.0.1", 1883),
        ("127.10.20.30", 1883, "127.0.0.1", 1883),
        ("127.0.0.2", 1883, "127.0.0.2", 2883),
        ("localhost", 1883, "127.0.0.2", 1883),
        ("::1", 1883, "127.0.0.1", 1883),
        ("127.0.0.1", 1883, "[::1]", 1883),
        ("emqx", 1883, "127.0.0.1", 1883),
        ("postgres", 5432, "127.0.0.1", 5432),
    ],
)
def test_local_endpoint_must_match_the_published_address_and_port(host, port, bind, published):
    with pytest.raises(ValueError):
        validator.check_endpoint(host, port, bind, published, "endpoint")


@pytest.mark.parametrize(
    "host,port,bind,published",
    [
        ("localhost", 1883, "127.0.0.1", 1883),
        ("127.0.0.2", 2883, "127.0.0.2", 2883),
        ("127.10.20.30", 2883, "127.10.20.30", 2883),
        ("::1", 2883, "[::1]", 2883),
        ("::1", 2883, "[::]", 2883),
        ("127.0.0.1", 2883, "0.0.0.0", 2883),
        ("external-broker.example", 9883, "127.0.0.1", 1883),
    ],
)
def test_valid_local_and_explicit_external_endpoints_are_preserved(host, port, bind, published):
    validator.check_endpoint(host, port, bind, published, "endpoint")


@pytest.mark.parametrize(
    "configuration",
    [
        "BROKER_HOST=127.0.0.2\nBROKER_PORT=2883\n",
        "DATABASE_URL=postgresql+asyncpg://user:private-credential@127.0.0.2:5432/database\n",
    ],
)
def test_cli_rejects_loopback_mismatch_without_exposing_credentials(tmp_path, configuration):
    script = tmp_path / "scripts/python/validate_dev_config.py"
    script.parent.mkdir(parents=True)
    shutil.copyfile(SCRIPT, script)
    (tmp_path / ".env").write_text(configuration)
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path.parent,
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert "do not match published ports" in result.stderr
    assert "private-credential" not in result.stdout + result.stderr
