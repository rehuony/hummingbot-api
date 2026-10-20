"""Source-update requests use only pinned public revisions and mock HTTP responses."""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import aiohttp
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services import source_updates
from utils.compose import SOURCE_REPOSITORY

CURRENT = "a" * 40
LATEST = "b" * 40
BUILD = {"source": SOURCE_REPOSITORY, "revision": CURRENT, "branch": "main", "created": None, "dirty": False}


class Response:
    def __init__(self, body=None, status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.options = None

    def factory(self, **kwargs):
        self.options = kwargs
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        assert not self.responses

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def github(monkeypatch):
    def make(*responses):
        session = Session(responses)
        monkeypatch.setattr(source_updates.aiohttp, "ClientSession", session.factory)
        return session

    return make


def branch(revision=LATEST):
    return Response({"object": {"type": "commit", "sha": revision}})


@pytest.mark.asyncio
async def test_matching_revision_needs_only_one_public_request(github):
    session = github(branch(CURRENT))
    result = await source_updates.check_source_update(BUILD)

    assert result == {
        "status": "up_to_date",
        "current_revision": CURRENT,
        "latest_revision": CURRENT,
        "checked_at": result["checked_at"],
        "reason": "The running source matches main.",
        "compare_url": f"{SOURCE_REPOSITORY}/compare/{CURRENT}...{CURRENT}",
    }
    assert datetime.fromisoformat(result["checked_at"]).utcoffset().total_seconds() == 0
    assert session.requests == [(f"{source_updates.GITHUB_API}/git/ref/heads/main", {"allow_redirects": False})]
    assert session.options["trust_env"] is False
    assert session.options["timeout"].total == 10
    assert session.options["timeout"].connect == 5
    assert "Authorization" not in session.options["headers"]


@pytest.mark.parametrize(
    "comparison, expected", [("ahead", "update_available"), ("behind", "ahead"), ("diverged", "diverged")]
)
@pytest.mark.asyncio
async def test_comparison_pins_both_revisions(github, comparison, expected):
    session = github(branch(), Response({"status": comparison}))
    result = await source_updates.check_source_update(BUILD)

    assert result["status"] == expected
    assert result["current_revision"] == CURRENT
    assert result["latest_revision"] == LATEST
    assert result["compare_url"] == f"{SOURCE_REPOSITORY}/compare/{CURRENT}...{LATEST}"
    assert session.requests[1] == (
        f"{source_updates.GITHUB_API}/compare/{CURRENT}...{LATEST}?per_page=1",
        {"allow_redirects": False},
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"source": "https://github.com/hummingbot/hummingbot-api"},
        {"revision": None},
        {"revision": "abc1234"},
        {"revision": "../main"},
        {"dirty": True},
        {"dirty": None},
        {"dirty": "false"},
    ],
)
@pytest.mark.asyncio
async def test_unverifiable_build_does_not_make_http_requests(github, changes):
    session = github()
    result = await source_updates.check_source_update({**BUILD, **changes})

    assert result["status"] == "unknown"
    assert result["latest_revision"] is None
    assert result["compare_url"] is None
    assert result["reason"]
    assert session.options is None


@pytest.mark.parametrize("source_branch", [None, "detached", "feature/source-check"])
@pytest.mark.asyncio
async def test_clean_revision_can_be_compared_without_main_branch_metadata(github, source_branch):
    github(branch(), Response({"status": "behind"}))
    result = await source_updates.check_source_update({**BUILD, "branch": source_branch})

    assert result["status"] == "ahead"


@pytest.mark.parametrize(
    "response, reason",
    [
        (Response(status=429), "rate limit"),
        (Response(status=403, headers={"X-RateLimit-Remaining": "0"}), "rate limit"),
        (Response(status=403, headers={"Retry-After": "60"}), "rate limit"),
        (Response(status=403), "HTTP 403"),
        (Response(status=404), "HTTP 404"),
        (Response(status=500), "HTTP 500"),
        (Response(status=302, headers={"Location": "https://unrelated.invalid"}), "HTTP 302"),
        (asyncio.TimeoutError("private proxy detail"), "timed out"),
        (aiohttp.ClientConnectionError("secret connection detail"), "Could not contact"),
        (Response(ValueError("secret response detail")), "invalid update response"),
        (Response([]), "invalid update response"),
        (Response({}), "invalid main branch revision"),
        (Response({"object": {"type": "commit", "sha": "not-a-sha"}}), "invalid main branch revision"),
    ],
)
@pytest.mark.asyncio
async def test_remote_failures_are_explicit_and_sanitized(github, response, reason):
    github(response)
    result = await source_updates.check_source_update(BUILD)

    assert result["status"] == "error"
    assert reason in result["reason"]
    assert "secret" not in result["reason"]
    assert "private" not in result["reason"]
    assert result["latest_revision"] is None


@pytest.mark.asyncio
async def test_unpublished_running_commit_is_unknown(github):
    github(branch(), Response(status=404))
    result = await source_updates.check_source_update(BUILD)

    assert result["status"] == "unknown"
    assert result["latest_revision"] == LATEST
    assert "running commit is unavailable" in result["reason"]


@pytest.mark.parametrize("payload", [{}, {"status": "unknown"}, {"status": "identical"}, {"status": []}])
@pytest.mark.asyncio
async def test_invalid_comparison_never_claims_up_to_date(github, payload):
    github(branch(), Response(payload))
    result = await source_updates.check_source_update(BUILD)

    assert result["status"] == "error"
    assert result["latest_revision"] == LATEST


def test_update_endpoint_uses_running_image_labels(github, monkeypatch):
    from deps import get_docker_service
    from routers import system

    github(branch(), Response({"status": "ahead"}))
    container = SimpleNamespace(
        id="running-api",
        name="hummingbot-api",
        labels={},
        attrs={"Config": {"Image": "rehuony/hummingbot-api:main"}},
        image=SimpleNamespace(
            id="sha256:running-image",
            attrs={
                "Config": {
                    "Labels": {
                        "org.opencontainers.image.source": SOURCE_REPOSITORY,
                        "org.opencontainers.image.revision": CURRENT,
                        "io.rehuony.hummingbot-api.dirty": "false",
                    }
                }
            },
        ),
    )
    monkeypatch.setattr(system, "own_container", lambda client: container)
    app = FastAPI()
    app.include_router(system.router)
    app.dependency_overrides[get_docker_service] = lambda: SimpleNamespace(client=SimpleNamespace(ping=lambda: True))

    response = TestClient(app).get("/system/update-check")

    assert response.status_code == 200
    assert response.json()["status"] == "update_available"
    assert response.json()["current_revision"] == CURRENT


@pytest.mark.parametrize("path", ["/system/info", "/system/update-check"])
def test_system_version_routes_require_authentication(path):
    from fastapi.testclient import TestClient

    from main import app

    # No lifespan context: authentication must reject before any service starts.
    response = TestClient(app).get(path)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Basic"
