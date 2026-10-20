"""Bot deployments disable external reporting without changing the source profile."""

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from hummingbot.client.config.client_config_map import AnonymizedMetricsDisabledMode, ClientConfigMap

from config import settings
from models import V2ControllerDeployment
from services.docker_service import DockerService


@pytest.mark.parametrize(
    "reporting",
    [
        {},
        {"send_error_logs": True, "anonymized_metrics_mode": {"anonymized_metrics_interval_min": 15}},
        {"send_error_logs": False, "anonymized_metrics_mode": "anonymized_metrics_disabled"},
    ],
)
def test_deployment_disables_reporting_for_existing_profiles(tmp_path, monkeypatch, reporting):
    profile = tmp_path / "bots" / "credentials" / "personal"
    profile.mkdir(parents=True)
    original = {
        "instance_id": "old-instance",
        "log_level": "INFO",
        "db_mode": {"db_engine": "sqlite"},
        "mqtt_bridge": {"mqtt_host": "localhost", "mqtt_logger": True, "mqtt_events": True},
        **reporting,
    }
    source = profile / "conf_client.yml"
    source.write_text(yaml.safe_dump(original))
    source_bytes = source.read_bytes()
    monkeypatch.chdir(tmp_path)
    # Stop before container creation; the test only exercises configuration generation.
    monkeypatch.setattr(settings.security, "config_password", "")
    service = DockerService.__new__(DockerService)
    service.SOURCE_PATH = str(tmp_path)
    service.client = Mock()

    service.create_hummingbot_instance(
        V2ControllerDeployment(
            instance_name="personal-bot",
            credentials_profile="personal",
            controllers_config=[],
            script_config="strategy.yml",
        )
    )

    deployed = yaml.safe_load(
        (tmp_path / "bots" / "instances" / "personal-bot" / "conf" / "conf_client.yml").read_text()
    )
    assert deployed["send_error_logs"] is False
    mode = ClientConfigMap.validate_anonymized_metrics_mode(deployed["anonymized_metrics_mode"])
    assert isinstance(mode, AnonymizedMetricsDisabledMode)
    assert deployed["instance_id"] == "personal-bot"
    assert deployed["log_level"] == original["log_level"]
    assert deployed["db_mode"] == original["db_mode"]
    assert deployed["mqtt_bridge"]["mqtt_logger"] is True
    assert deployed["mqtt_bridge"]["mqtt_events"] is True
    assert deployed["mqtt_bridge"]["mqtt_host"] == settings.broker.bind
    assert source.read_bytes() == source_bytes
    service.client.containers.run.assert_not_called()


def test_default_profile_uses_hummingbot_disabled_reporting_mode():
    path = Path(__file__).resolve().parents[1] / "templates/master_account/conf_client.yml"
    profile = yaml.safe_load(path.read_text())
    assert profile["send_error_logs"] is False
    mode = ClientConfigMap.validate_anonymized_metrics_mode(profile["anonymized_metrics_mode"])
    assert isinstance(mode, AnonymizedMetricsDisabledMode)


@pytest.mark.parametrize("existing_profile", [False, True])
def test_setup_seeds_only_missing_profiles_and_preserves_configuration(tmp_path, existing_profile):
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "scripts/shell/lib").mkdir(parents=True)
    shutil.copy(root / "scripts/shell/setup.sh", tmp_path / "scripts/shell/setup.sh")
    shutil.copy(root / "scripts/shell/lib/tailnet-state.sh", tmp_path / "scripts/shell/lib/tailnet-state.sh")
    shutil.copytree(root / "templates", tmp_path / "templates")
    environment_file = tmp_path / ".env"
    environment_file.write_text("EXISTING_CONFIGURATION=keep\n")
    original_environment = environment_file.read_bytes()
    (tmp_path / ".setup-complete").touch()
    profile = tmp_path / "bots/credentials/master_account/conf_client.yml"
    original_profile = b"# Personal configuration\nsend_error_logs: true\nlog_level: DEBUG\n"
    if existing_profile:
        profile.parent.mkdir(parents=True)
        profile.write_bytes(original_profile)

    # Setup is configuration-only, even when Docker and package tools are present.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text("#!/bin/sh\necho Unexpected Docker call >&2\nexit 97\n")
    docker.chmod(0o755)
    subprocess.run(
        ["bash", str(tmp_path / "scripts/shell/setup.sh")],
        cwd=tmp_path.parent,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert environment_file.read_bytes() == original_environment
    if existing_profile:
        assert profile.read_bytes() == original_profile
    else:
        assert profile.read_bytes() == (tmp_path / "templates/master_account/conf_client.yml").read_bytes()
        assert profile.stat().st_mode & 0o777 == 0o600
