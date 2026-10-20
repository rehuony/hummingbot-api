"""Render Compose in temporary directories without reading runtime credentials."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def compose_config(tmp_path):
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker CLI is required for offline Compose rendering")
    environment = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", str(tmp_path))}
    version = subprocess.run(
        [docker, "compose", "version"], env=environment, capture_output=True, text=True, timeout=15
    )
    if version.returncode:
        pytest.skip("Docker Compose plugin is not installed")
    for filename in ("docker-compose.yml", "docker-compose.tailscale.yml", "tailscale-serve.json"):
        shutil.copy(ROOT / filename, tmp_path / filename)
    (tmp_path / "scripts/sql").mkdir(parents=True)
    shutil.copy(ROOT / "scripts/sql/init-db.sql", tmp_path / "scripts/sql/init-db.sql")
    shutil.copytree(ROOT / "emqx", tmp_path / "emqx")

    def render(values=None, *, process_values=None, tailscale=False):
        settings = {
            "USERNAME": "test-user",
            "PASSWORD": "test-password",
            "BROKER_USERNAME": "test-broker",
            "BROKER_PASSWORD": "test-broker-password",
            **(values or {}),
        }
        (tmp_path / ".env").write_text("".join(f"{key}={value}\n" for key, value in settings.items()))
        args = [
            docker, "compose", "--project-directory", str(tmp_path),
            "--env-file", str(tmp_path / ".env"), "-p", "isolated-ports-test",
            "-f", str(tmp_path / "docker-compose.yml"),
        ]
        if tailscale:
            args += ["-f", str(tmp_path / "docker-compose.tailscale.yml")]
        result = subprocess.run(
            [*args, "config", "--format", "json"],
            cwd=tmp_path,
            env={**environment, **(process_values or {})},
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return json.loads(result.stdout)

    return render


def published_ports(service):
    return {(port["host_ip"], int(port["published"]), port["target"]) for port in service["ports"]}


@pytest.mark.parametrize("tailscale", [False, True])
def test_default_compose_ports_are_local_and_internal_network_is_unchanged(compose_config, tailscale):
    config = compose_config(tailscale=tailscale)
    services = config["services"]
    assert published_ports(services["hummingbot-api"]) == {("127.0.0.1", 8000, 8000)}
    assert not services["postgres"].get("ports")
    assert published_ports(services["emqx"]) == {
        ("127.0.0.1", 1883, 1883), ("127.0.0.1", 18083, 18083),
    }
    api = services["hummingbot-api"]
    assert api["image"] == "rehuony/hummingbot-api:main"
    assert api["pull_policy"] == "never"
    assert api["environment"]["BROKER_HOST"] == "emqx"
    assert api["environment"]["BROKER_PORT"] == "1883"
    assert api["environment"]["BROKER_BIND"] == "127.0.0.1"
    assert api["environment"]["BROKER_PUBLISHED_PORT"] == "1883"
    assert api["environment"]["DATABASE_URL"].endswith("@postgres:5432/hummingbot_api")
    assert api["environment"]["GATEWAY_URL"] == "https://gateway:15888"
    assert set(services["postgres"]["networks"]) == {"emqx-bridge"}
    init_mount = next(
        mount for mount in services["postgres"]["volumes"]
        if mount["target"] == "/docker-entrypoint-initdb.d/init-db.sql"
    )
    assert init_mount["source"].endswith("/scripts/sql/init-db.sql")
    assert init_mount["read_only"] is True
    assert services["emqx"]["environment"]["EMQX_AUTHORIZATION__NO_MATCH"] == "deny"
    assert services["emqx"]["environment"]["EMQX_AUTHENTICATION__1__MECHANISM"] == "password_based"
    if tailscale:
        assert services["tailscale"]["network_mode"] == "host"


def test_custom_host_ports_do_not_change_container_endpoints(compose_config):
    config = compose_config({
        "API_IMAGE": "rehuony/hummingbot-api:verify-ports",
        "API_BIND": "100.64.0.1",
        "DB_BIND": "127.0.0.2",
        "DB_PORT": "15432",
        "BROKER_BIND": "100.64.0.2",
        "BROKER_PUBLISHED_PORT": "2883",
        "BROKER_DASHBOARD_BIND": "127.0.0.3",
        "BROKER_DASHBOARD_PORT": "28083",
        "BROKER_PORT": "2883",
    })
    services = config["services"]
    assert published_ports(services["hummingbot-api"]) == {("100.64.0.1", 8000, 8000)}
    assert not services["postgres"].get("ports")
    assert published_ports(services["emqx"]) == {
        ("100.64.0.2", 2883, 1883), ("127.0.0.3", 28083, 18083),
    }
    api = services["hummingbot-api"]
    assert api["image"] == "rehuony/hummingbot-api:verify-ports"
    assert api["environment"]["BROKER_PORT"] == "1883"
    assert api["environment"]["BROKER_BIND"] == "100.64.0.2"
    assert api["environment"]["BROKER_PUBLISHED_PORT"] == "2883"
    assert api["environment"]["DATABASE_URL"].endswith("@postgres:5432/hummingbot_api")


def test_compose_controls_use_process_precedence_but_credentials_come_from_env_file(compose_config):
    config = compose_config(
        {"BROKER_BIND": "127.0.0.1", "BROKER_PUBLISHED_PORT": "2883"},
        process_values={
            "API_IMAGE": "rehuony/hummingbot-api:process",
            "BROKER_BIND": "100.64.0.3",
            "BROKER_PUBLISHED_PORT": "3883",
            "BROKER_PASSWORD": "not-the-container-password",
        },
    )
    services = config["services"]
    assert ("100.64.0.3", 3883, 1883) in published_ports(services["emqx"])
    api = services["hummingbot-api"]
    assert api["image"] == "rehuony/hummingbot-api:process"
    assert api["environment"]["BROKER_BIND"] == "100.64.0.3"
    assert api["environment"]["BROKER_PUBLISHED_PORT"] == "3883"
    assert api["environment"]["BROKER_PASSWORD"] == "test-broker-password"


def test_deploy_image_preflight_reads_api_image_not_dependency_images(compose_config, tmp_path):
    compose_config({"API_IMAGE": "rehuony/hummingbot-api:fixture"})
    shutil.copytree(ROOT / "scripts/shell/lib", tmp_path / "scripts/shell/lib")
    command = ["bash", "-c", '. "$1"; load_compose_settings; verify_api_image',
               "image-preflight", str(tmp_path / "scripts/shell/lib/common.sh")]
    environment = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", str(tmp_path))}
    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    (tmp_path / "docker-compose.override.yml").write_text(
        "services:\n  hummingbot-api:\n    image: old/api:main\n"
    )
    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "image differs" in result.stderr
    assert "test-broker-password" not in result.stdout + result.stderr
