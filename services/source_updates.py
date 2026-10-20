"""Explicit, read-only comparison of a running build with the personal source repository."""

import asyncio
import re
from datetime import datetime, timezone

import aiohttp

from utils.compose import SOURCE_REPOSITORY

GITHUB_API = "https://api.github.com/repos/rehuony/hummingbot-api"
REVISION = re.compile(r"[0-9a-f]{40}")


def _revision(value):
    return value if isinstance(value, str) and REVISION.fullmatch(value) else None


class _CheckFailure(Exception):
    def __init__(self, reason, status="error"):
        super().__init__(reason)
        self.status = status


async def _github_json(session, path, *, comparing=False):
    async with session.get(f"{GITHUB_API}/{path}", allow_redirects=False) as response:
        if response.status == 404 and comparing:
            raise _CheckFailure("The running commit is unavailable in the source repository.", "unknown")
        if response.status == 429 or (
            response.status == 403
            and (response.headers.get("X-RateLimit-Remaining") == "0" or response.headers.get("Retry-After"))
        ):
            raise _CheckFailure("GitHub rate limit reached; try the update check later.")
        if response.status != 200:
            raise _CheckFailure(f"GitHub update check failed (HTTP {response.status}).")
        body = await response.json()
        if not isinstance(body, dict):
            raise _CheckFailure("GitHub returned an invalid update response.")
        return body


async def check_source_update(build):
    """Compare only a known clean running revision, never a host checkout or image tag."""
    build = build or {}
    current = _revision(build.get("revision"))
    result = {
        "status": "unknown",
        "current_revision": current,
        "latest_revision": None,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "reason": None,
        "compare_url": None,
    }
    if build.get("source") != SOURCE_REPOSITORY or current is None:
        result["reason"] = "The running image has no verifiable source revision for this repository."
        return result
    if build.get("dirty") is not False:
        result["reason"] = (
            "The running image includes uncommitted source changes."
            if build.get("dirty") is True
            else "The running image's source cleanliness is unknown."
        )
        return result

    try:
        # Neither environment credentials nor redirects can change the fixed public source.
        timeout = aiohttp.ClientTimeout(total=10, connect=5)
        async with aiohttp.ClientSession(
            timeout=timeout,
            trust_env=False,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "rehuony-hummingbot-api"},
        ) as session:
            remote = await _github_json(session, "git/ref/heads/main")
            remote_object = remote.get("object")
            latest = _revision(remote_object.get("sha")) if isinstance(remote_object, dict) else None
            if latest is None or remote_object.get("type") != "commit":
                raise _CheckFailure("GitHub returned an invalid main branch revision.")
            result["latest_revision"] = latest
            result["compare_url"] = f"{SOURCE_REPOSITORY}/compare/{current}...{latest}"
            if current == latest:
                result.update(status="up_to_date", reason="The running source matches main.")
                return result

            comparison = await _github_json(session, f"compare/{current}...{latest}?per_page=1", comparing=True)
            states = {
                "ahead": ("update_available", "main contains source changes newer than the running build."),
                "behind": ("ahead", "The running source is ahead of main."),
                "diverged": ("diverged", "The running source and main have diverged."),
            }
            state = states.get(comparison.get("status"))
            if state is None:
                raise _CheckFailure("GitHub returned an invalid source comparison.")
            result.update(status=state[0], reason=state[1])
    except _CheckFailure as exc:
        result.update(status=exc.status, reason=str(exc))
    except asyncio.TimeoutError:
        result.update(status="error", reason="GitHub update check timed out.")
    except aiohttp.ClientError:
        result.update(status="error", reason="Could not contact GitHub for the update check.")
    except (ValueError, TypeError):
        result.update(status="error", reason="GitHub returned an invalid update response.")
    return result
