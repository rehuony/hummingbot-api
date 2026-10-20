"""Host-network bot endpoints follow local publishes without rewriting profiles."""

from unittest.mock import Mock

import pytest
import yaml
from pydantic import ValidationError

from config import BrokerSettings, settings
from models import V2ControllerDeployment
from services.docker_service import DockerService


@pytest.mark.parametrize(
    "profile_endpoint,bind,published_port,expected_host,expected_port",
    [
        ({}, "127.0.0.1", 1883, "127.0.0.1", 1883),
        ({"mqtt_host": "localhost"}, "127.0.0.1", 2883, "127.0.0.1", 2883),
        ({"mqtt_host": "127.0.0.1", "mqtt_port": 1883}, "100.64.0.2", 2883, "100.64.0.2", 2883),
        ({"mqtt_host": "127.0.0.1", "mqtt_port": "1883"}, "0.0.0.0", 2883, "127.0.0.1", 2883),
        ({"mqtt_host": "127.0.0.2", "mqtt_port": "1883"}, "0.0.0.0", 2883, "127.0.0.2", "1883"),
        ({"mqtt_host": "127.10.20.30", "mqtt_port": 1883}, "127.0.0.1", 2883, "127.10.20.30", 1883),
        ({"mqtt_host": "::1", "mqtt_port": 1883}, "[::]", 2883, "::1", 2883),
        ({"mqtt_host": "localhost", "mqtt_port": 1883}, "[::1]", 2883, "::1", 2883),
        ({"mqtt_host": "my-broker", "mqtt_port": 1883}, "127.0.0.1", 2883, "my-broker", 1883),
        ({"mqtt_host": "localhost", "mqtt_port": 9883}, "100.64.0.2", 2883, "localhost", 9883),
        ({"mqtt_host": "10.0.0.2", "mqtt_port": 9883}, "127.0.0.1", 2883, "10.0.0.2", 9883),
    ],
)
def test_bot_endpoint_uses_published_broker_only_for_default_local_profiles(
    tmp_path, monkeypatch, profile_endpoint, bind, published_port, expected_host, expected_port
):
    profile = tmp_path / "bots/credentials/personal/conf_client.yml"
    profile.parent.mkdir(parents=True)
    profile.write_text(yaml.safe_dump({"mqtt_bridge": {"mqtt_logger": True, **profile_endpoint}}))
    original = profile.read_bytes()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings.security, "config_password", "")
    monkeypatch.setattr(settings.broker, "bind", bind)
    monkeypatch.setattr(settings.broker, "published_port", published_port)
    monkeypatch.setattr(settings.broker, "username", "test-user")
    monkeypatch.setattr(settings.broker, "password", "test-password")
    service = DockerService.__new__(DockerService)
    service.SOURCE_PATH = str(tmp_path)
    service.client = Mock()

    result = service.create_hummingbot_instance(
        V2ControllerDeployment(instance_name="test-bot", credentials_profile="personal", controllers_config=[])
    )

    assert result["success"] is True
    deployed = yaml.safe_load((tmp_path / "bots/instances/test-bot/conf/conf_client.yml").read_text())
    bridge = deployed["mqtt_bridge"]
    assert bridge["mqtt_host"] == expected_host
    assert bridge["mqtt_port"] == expected_port
    assert bridge["mqtt_username"] == "test-user"
    assert bridge["mqtt_password"] == "test-password"
    assert bridge["mqtt_logger"] is True
    assert service.client.containers.run.call_args.kwargs["network_mode"] == "host"
    assert profile.read_bytes() == original


@pytest.mark.parametrize("published_port", [0, -1, 65536])
def test_broker_rejects_invalid_published_ports(published_port):
    with pytest.raises(ValidationError):
        BrokerSettings(published_port=published_port)
