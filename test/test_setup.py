"""Configuration setup stays offline and independent of installation or deployment."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def checkout(tmp_path):
    repository = tmp_path / "checkout"
    (repository / "scripts/shell/lib").mkdir(parents=True)
    shutil.copy(ROOT / "scripts/shell/setup.sh", repository / "scripts/shell/setup.sh")
    shutil.copy(ROOT / "scripts/shell/lib/tailnet-state.sh", repository / "scripts/shell/lib/tailnet-state.sh")
    shutil.copytree(ROOT / "templates", repository / "templates")
    commands = tmp_path / "bin"
    commands.mkdir()
    for name in ("docker", "curl", "sudo", "apt-get", "conda", "tailscale"):
        command = commands / name
        command.write_text(f'#!/bin/sh\nprintf "%s\\n" "{name}" >> "$CALL_LOG"\nexit 97\n')
        command.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{commands}:{os.environ['PATH']}",
        "CALL_LOG": str(tmp_path / "calls"),
        "HBAPI_NONINTERACTIVE": "1",
        "HBAPI_USERNAME": "personal",
        "HBAPI_PASSWORD": "PrivateApiCredential1",
        "HBAPI_CONFIG_PASSWORD": "PrivateEncryptionCredential2",
        "TAILSCALE_MODE": "none",
    }
    return repository, environment


def run_setup(checkout, **overrides):
    repository, environment = checkout
    return subprocess.run(
        ["bash", str(repository / "scripts/shell/setup.sh")],
        cwd=repository.parent,
        env={**environment, **overrides},
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_fresh_setup_only_initializes_configuration_from_any_directory(checkout):
    repository, environment = checkout
    result = run_setup(checkout)
    assert result.returncode == 0, result.stderr
    configuration = (repository / ".env").read_text()
    assert "USERNAME=personal\n" in configuration
    assert f"BOTS_PATH={repository}\n" in configuration
    for setting in (
        "API_BIND=127.0.0.1",
        "DB_BIND=127.0.0.1",
        "DB_PORT=5432",
        "BROKER_BIND=127.0.0.1",
        "BROKER_PUBLISHED_PORT=1883",
        "BROKER_DASHBOARD_BIND=127.0.0.1",
        "BROKER_DASHBOARD_PORT=18083",
    ):
        assert f"{setting}\n" in configuration
    assert (repository / ".env").stat().st_mode & 0o777 == 0o600
    assert not (repository / ".setup-complete").exists()
    assert not Path(environment["CALL_LOG"]).exists()
    assert environment["HBAPI_PASSWORD"] not in result.stdout + result.stderr


def test_existing_sentinel_does_not_skip_missing_configuration(checkout):
    repository, _ = checkout
    (repository / ".setup-complete").touch()
    assert run_setup(checkout).returncode == 0
    assert (repository / ".env").exists()


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"HBAPI_BACKTESTING_MAX_CONCURRENT": "2"}, "BACKTESTING_MAX_CONCURRENT=2\n"),
        ({"HBAPI_BACKTESTING_TIMEOUT_SECONDS": "120.5"}, "BACKTESTING_TIMEOUT_SECONDS=120.5\n"),
        ({"TAILSCALE_MODE": "host", "TAILSCALE_ENABLED": "true"}, "TAILSCALE_MODE=host\n"),
        (
            {
                "TAILSCALE_MODE": "sidecar",
                "TAILSCALE_AUTH_KEY": "tskey-auth-example",
                "TAILSCALE_HOSTNAME": "personal-api",
            },
            "TAILSCALE_HOSTNAME=personal-api\n",
        ),
    ],
)
def test_noninteractive_parameters_are_preserved_without_dependency_actions(checkout, overrides, expected):
    repository, environment = checkout
    result = run_setup(checkout, **overrides)
    assert result.returncode == 0, result.stderr
    assert expected in (repository / ".env").read_text()
    assert not Path(environment["CALL_LOG"]).exists()


@pytest.mark.parametrize(
    "overrides",
    [
        {"HBAPI_PASSWORD": "secret$(touch /tmp/never-execute)"},
        {"HBAPI_BACKTESTING_MAX_CONCURRENT": "2\nPASSWORD=secret"},
        {"TAILSCALE_AUTH_KEY": "tskey-auth-secret\nPASSWORD=secret", "TAILSCALE_MODE": "sidecar"},
    ],
)
def test_malformed_inputs_do_not_create_configuration_or_expose_values(checkout, overrides):
    repository, _ = checkout
    result = run_setup(checkout, **overrides)
    assert result.returncode != 0
    assert not (repository / ".env").exists()
    assert "secret" not in result.stdout + result.stderr
