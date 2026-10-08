"""Process-wide Binance REST admission shared by API connectors and candle feeds.

Keep the connector's own order/IP throttles intact. This additional weight budget
groups API-owned clients by Binance product/domain, including separate accounts.
External bots/processes are not covered; response headers provide a safety brake
when another consumer uses the same egress IP.
"""

import asyncio
import logging
import math
import re
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.api_throttler.data_types import LinkedLimitWeightPair, RateLimit
from hummingbot.core.web_assistant.rest_post_processors import RESTPostProcessorBase
from hummingbot.core.web_assistant.rest_pre_processors import RESTPreProcessorBase

logger = logging.getLogger(__name__)

BINANCE_CONNECTORS = {"binance", "binance_us", "binance_perpetual", "binance_perpetual_testnet"}


class BinanceRateLimitError(IOError):
    def __init__(self, retry_after: float, status: int = 429):
        self.status = status
        self.retry_after = max(1, math.ceil(retry_after))
        super().__init__(f"Binance REST requests paused; retry after {self.retry_after}s (HTTP {status})")


class BinanceRequestBudget:
    def __init__(self, limit: int, interval: float):
        self.limit = limit
        self.interval = interval
        self.throttler = AsyncThrottler([RateLimit("shared_weight", limit, interval)])
        self.blocked_until = 0.0
        self.status = 429

    def raise_if_blocked(self):
        remaining = self.blocked_until - time.monotonic()
        if remaining > 0:
            raise BinanceRateLimitError(remaining, self.status)

    async def wait(self, wait_on_cooldown: bool):
        while self.blocked_until > time.monotonic():
            if not wait_on_cooldown:
                self.raise_if_blocked()
            await asyncio.sleep(max(0, self.blocked_until - time.monotonic()))

    def block(self, seconds: float, status: int):
        until = time.monotonic() + max(1, seconds)
        if until > self.blocked_until:
            self.blocked_until = until
            self.status = status
            logger.warning("Binance REST budget paused for %.0fs (HTTP %s)", seconds, status)

    async def acquire(self, weight: int, wait_on_cooldown: bool):
        while True:
            await self.wait(wait_on_cooldown)
            if not weight:
                return
            limit_id = f"weight:{weight}"
            self.throttler.add_rate_limits(
                [RateLimit(limit_id, self.limit, self.interval, linked_limits=[LinkedLimitWeightPair("shared_weight", weight)])]
            )
            async with self.throttler.execute_task(limit_id):
                # Another request may have been banned while this one waited for capacity.
                # After a cooldown, obtain a fresh permit, rather than spending an
                # expired reservation and releasing all sleeping requests at once.
                if self.blocked_until <= time.monotonic():
                    return


def _retry_after(headers, payload, status):
    seconds = []
    value = headers.get("retry-after")
    if value is not None:
        try:
            seconds.append(float(value))
        except (TypeError, ValueError):
            try:
                seconds.append(parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    # REST -1003 responses often carry the ban expiry in the message, in epoch ms.
    match = re.search(r"banned until\s+(\d{10,13})", str(payload.get("msg", "")), re.IGNORECASE)
    if match:
        stamp = int(match.group(1))
        seconds.append((stamp / 1000 if stamp > 10**11 else stamp) - time.time())
    if seconds and max(seconds) > 0:
        return max(seconds)
    return 120 if status == 418 else 60


class BinanceRESTPolicy(RESTPreProcessorBase, RESTPostProcessorBase):
    def __init__(self, budget, upstream_throttler, wait_on_cooldown):
        self.budget = budget
        self.upstream_throttler = upstream_throttler
        self.wait_on_cooldown = wait_on_cooldown

    def request_weight(self, request):
        path = urlsplit(request.url).path
        params = request.params or {}
        if path == "/fapi/v1/klines":
            limit = int(params.get("limit", 500))
            return 1 if limit < 100 else 2 if limit < 500 else 5 if limit <= 1000 else 10
        if path == "/api/v3/klines":
            return 2
        # Reuse connector endpoint definitions for account, order and other reads.
        direct, related = self.upstream_throttler.get_related_limits(request.throttler_limit_id)
        weights = [weight for limit, weight in related if limit.limit_id == "REQUEST_WEIGHT"]
        if weights:
            return max(weights)
        return int(direct.weight) if direct is not None else 1

    async def pre_process(self, request):
        await self.budget.acquire(self.request_weight(request), self.wait_on_cooldown)
        return request

    async def post_process(self, response):
        headers = {key.lower(): value for key, value in response.headers.items()}
        if response.status in (418, 429):
            try:
                payload = await response.json()
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            self.budget.block(_retry_after(headers, payload, response.status), response.status)
            self.budget.raise_if_blocked()
        try:
            used = int(headers.get("x-mbx-used-weight-1m", 0))
        except (TypeError, ValueError):
            used = 0
        if used >= self.budget.limit:
            # A full minute is conservative even if another process spent the quota.
            self.budget.block(60, 429)
        return response


class BinanceRateLimits:
    """Owned by UnifiedConnectorService: same IP budget across all API accounts."""

    def __init__(self):
        self._budgets = {}

    def install(self, connector_name, factory, *, wait_on_cooldown=True):
        if connector_name not in BINANCE_CONNECTORS:
            return
        if any(isinstance(p, BinanceRESTPolicy) for p in factory._rest_pre_processors):
            return
        budget = self._budgets.get(connector_name)
        if budget is None:
            upstream, _ = factory.throttler.get_related_limits("REQUEST_WEIGHT")
            # Reserve 20% for other IP users and endpoint-weight drift. The exchange's
            # response headers still stop traffic if that headroom is exhausted.
            limit = max(1, int(upstream.limit) * 4 // 5) if upstream is not None else 960
            interval = upstream.time_interval if upstream is not None else 60
            budget = self._budgets[connector_name] = BinanceRequestBudget(limit, interval)
        policy = BinanceRESTPolicy(budget, factory.throttler, wait_on_cooldown)
        # Install before any assistants are created, preserving existing auth and
        # timestamp preprocessors. Both hooks run through Hummingbot's extension API.
        factory._rest_pre_processors.insert(0, policy)
        factory._rest_post_processors.insert(0, policy)

    def raise_if_blocked(self, connector_name):
        budget = self._budgets.get(connector_name)
        if budget is not None:
            budget.raise_if_blocked()
