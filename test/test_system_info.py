"""Local system information remains available without a registry or updater."""
from types import SimpleNamespace

import pytest
from docker.errors import DockerException, NotFound
from fastapi import FastAPI
from fastapi.testclient import TestClient

from version import VERSION

CONTAINER_ID = "3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4e3f2a"  # noqa: mock  (a Docker container id, not a key)
WORKING_DIR = "/home/hbot/hummingbot-api"


def _container(
    digests=("hummingbot-api-local@sha256:abc123",),
    config_files=f"{WORKING_DIR}/docker-compose.yml",
    image="hummingbot-api-local:main",
    name="hummingbot-api",
    project="hummingbot-api",
):
    """A docker-py Container as this route reads it."""
    return SimpleNamespace(
        id=CONTAINER_ID,
        name=name,
        labels={
            "com.docker.compose.project": project,
            "com.docker.compose.project.working_dir": WORKING_DIR,
            "com.docker.compose.project.config_files": config_files,
            "com.docker.compose.service": "hummingbot-api",
        },
        attrs={"Config": {"Image": image}},
        image=SimpleNamespace(
            id="sha256:deadbeef",
            tags=[image],
            attrs={"RepoDigests": list(digests)},
        ),
    )


class _Containers:
    def __init__(self, container, found_by_hostname=True, others=(), found_by_id=None):
        self.container = container
        self.found_by_hostname = found_by_hostname
        self.found_by_id = found_by_id
        self.others = list(others)
        self.list_filters = None

    def get(self, name):
        if self.found_by_hostname or (self.found_by_id and name == self.found_by_id):
            return self.container
        raise NotFound(f"no such container: {name}")

    def list(self, filters=None):
        self.list_filters = filters
        return ([self.container] if self.container is not None else []) + self.others


class _Client:
    def __init__(self, container, found_by_hostname=True, reachable=True, others=(), found_by_id=None):
        self.containers = _Containers(container, found_by_hostname, others, found_by_id)
        self.reachable = reachable

    def ping(self):
        if not self.reachable:
            raise DockerException("Error while fetching server API version")
        return True


@pytest.fixture(autouse=True)
def inside_a_container(monkeypatch):
    """Run as the containerised API by default, with no container id in the mount table."""
    from utils import compose

    monkeypatch.setattr(compose, "_container_id_from_mountinfo", lambda: None)
    monkeypatch.setattr(compose, "_in_container", lambda: True)
    return monkeypatch


@pytest.fixture
def make_client():
    """Build a TestClient whose /system/info talks to the given fake docker client."""
    from deps import get_docker_service
    from routers import system

    def _make(docker_client):
        app = FastAPI()
        app.include_router(system.router)
        app.dependency_overrides[get_docker_service] = lambda: SimpleNamespace(client=docker_client)
        return TestClient(app)

    return _make


def test_reports_versions_and_container(make_client):
    body = make_client(_Client(_container())).get("/system/info").json()

    assert body["api_version"] == VERSION
    assert body["hummingbot_version"]  # the library is installed in the test environment
    assert body["docker_available"] is True
    assert body["container"] == {
        "id": CONTAINER_ID,
        "name": "hummingbot-api",
        "image": "hummingbot-api-local:main",
        "image_id": "sha256:deadbeef",
        "digest": "hummingbot-api-local@sha256:abc123",
        "compose_project": "hummingbot-api",
        "compose_working_dir": WORKING_DIR,
        "compose_config_files": f"{WORKING_DIR}/docker-compose.yml",
    }
    assert "pinned" not in body


def test_locally_built_image_without_a_digest_is_reported(make_client):
    body = make_client(_Client(_container(digests=()))).get("/system/info").json()

    assert body["container"]["image"] == "hummingbot-api-local:main"
    assert body["container"]["digest"] is None


def test_containerd_local_digest_is_reported(make_client):
    container = _container(digests=("hbapi@sha256:abc123",), image="hbapi:main")
    body = make_client(_Client(container)).get("/system/info").json()

    assert body["container"]["digest"] == "hbapi@sha256:abc123"


def test_digest_from_another_repository_is_not_reported(make_client):
    container = _container(digests=("another-api@sha256:abc123",))
    body = make_client(_Client(container)).get("/system/info").json()

    assert body["container"]["digest"] is None


def test_compose_overlay_metadata_is_preserved(make_client):
    config_files = f"{WORKING_DIR}/docker-compose.yml,{WORKING_DIR}/docker-compose.tailscale.yml"
    body = make_client(_Client(_container(config_files=config_files))).get("/system/info").json()

    assert body["container"]["compose_config_files"] == config_files


def test_self_upgrade_routes_are_absent(make_client):
    client = make_client(_Client(_container()))
    for method, url in (("GET", "/system/upgrade/preflight"), ("POST", "/system/upgrade"), ("GET", "/system/upgrade/status")):
        assert client.request(method, url).status_code == 404


def test_falls_back_to_the_compose_service_label(make_client):
    client = _Client(_container(), found_by_hostname=False)
    body = make_client(client).get("/system/info").json()

    assert client.containers.list_filters == {"label": "com.docker.compose.service=hummingbot-api"}
    assert body["container"]["name"] == "hummingbot-api"


def test_finds_itself_by_container_id_when_the_hostname_is_overridden(make_client, inside_a_container):
    from utils import compose

    inside_a_container.setattr(compose, "_container_id_from_mountinfo", lambda: CONTAINER_ID)
    decoy = _container(name="decoy-api", project="some-other-stack", image="alpine:latest")
    client = _Client(_container(), found_by_hostname=False, found_by_id=CONTAINER_ID, others=[decoy])
    body = make_client(client).get("/system/info").json()

    assert body["container"]["name"] == "hummingbot-api"
    assert client.containers.list_filters is None  # never needed the label fallback


def test_two_candidates_for_the_service_label_report_no_container(make_client):
    # Another stack on the same host runs a hummingbot-api service too. Picking one would
    # report its image as this server's.
    decoy = _container(name="decoy-api", project="some-other-stack", image="alpine:latest")
    body = make_client(_Client(_container(), found_by_hostname=False, others=[decoy])).get("/system/info").json()

    assert body["docker_available"] is True
    assert body["container"] is None


def test_running_from_source_does_not_claim_a_container(make_client, inside_a_container):
    # From source this process is no container at all; the containerised API on the same
    # host carries the service label, but it is not us.
    from utils import compose

    inside_a_container.setattr(compose, "_in_container", lambda: False)
    client = _Client(_container(), found_by_hostname=False)
    body = make_client(client).get("/system/info").json()

    assert body["container"] is None
    assert client.containers.list_filters is None


def test_docker_unavailable_still_reports_versions(make_client):
    body = make_client(_Client(_container(), reachable=False)).get("/system/info").json()

    assert body["docker_available"] is False
    assert body["container"] is None
    assert body["api_version"] == VERSION
    assert body["hummingbot_version"]


def test_container_not_identified_reports_no_container(make_client):
    client = _Client(None, found_by_hostname=False)
    body = make_client(client).get("/system/info").json()

    assert body["docker_available"] is True
    assert body["container"] is None
    assert body["api_version"] == VERSION


def test_docker_service_without_a_client_never_raises():
    """DockerService leaves `client` unset when the daemon was absent at startup."""
    from deps import get_docker_service
    from routers import system

    app = FastAPI()
    app.include_router(system.router)
    app.dependency_overrides[get_docker_service] = lambda: SimpleNamespace()

    body = TestClient(app).get("/system/info").json()
    assert body["docker_available"] is False
    assert body["container"] is None
    assert body["api_version"] == VERSION


def test_reports_the_market_data_tunables(make_client):
    # The dashboard shows these read-only: they come from the server's .env, which is not
    # mounted into the container, so the API can report them but never rewrite them.
    from config import settings

    body = make_client(_Client(_container())).get("/system/info").json()

    assert body["market_data"] == settings.market_data.model_dump(exclude={"historical_cache_path"})
    assert body["market_data"]["ticker_update_interval"] == settings.market_data.ticker_update_interval


def test_market_data_tunables_carry_no_secret(make_client, monkeypatch):
    from config import settings

    # Local cache paths are not part of the dashboard's numeric tuning contract.
    monkeypatch.setattr(settings.market_data, "historical_cache_path", "/private/deployment/cache")
    body = make_client(_Client(_container())).get("/system/info").json()

    assert body["market_data"]
    assert "historical_cache_path" not in body["market_data"]
    assert all(isinstance(v, (int, float)) for v in body["market_data"].values())
    assert not any(
        word in key.lower()
        for key in body["market_data"]
        for word in ("password", "secret", "token", "key", "user")
    )


def test_market_data_is_reported_without_docker(make_client):
    # The versions and the tunables are the part that works from source too.
    body = make_client(_Client(_container(), reachable=False)).get("/system/info").json()

    assert body["docker_available"] is False
    assert body["market_data"]
