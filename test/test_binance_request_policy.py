"""Real Hummingbot REST assistant/feed integration; every transport is a local fake."""

import asyncio
import time
from email.utils import formatdate
from types import SimpleNamespace

import pytest
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.api_throttler.data_types import LinkedLimitWeightPair, RateLimit
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory

from services.binance_candles import APIBinancePerpetualCandles, APIBinanceSpotCandles
from services.binance_rate_limits import BinanceRateLimitError, BinanceRateLimits, BinanceRequestBudget, BinanceRESTPolicy


class Response:
    def __init__(self, status=200, headers=None, payload=None):
        self.status = status
        self.headers = headers or {}
        self.payload = payload if payload is not None else []

    async def json(self):
        return self.payload

    async def text(self):
        return str(self.payload)


class Transport:
    def __init__(self, response=None):
        self.response = response or Response()
        self.requests = []

    async def get_rest_connection(self):
        return self

    async def call(self, request):
        self.requests.append(request)
        return self.response

    async def close(self):
        pass


def factory(registry, name="binance_perpetual", *, response=None, wait=False, limit=1200):
    transport = Transport(response)
    throttle = AsyncThrottler(
        [
            RateLimit("REQUEST_WEIGHT", limit, 60),
            RateLimit("klines", limit, 60, linked_limits=[LinkedLimitWeightPair("REQUEST_WEIGHT", 1)]),
        ]
    )
    result = WebAssistantsFactory(throttle, connections_factory=transport)
    registry.install(name, result, wait_on_cooldown=wait)
    return result, transport


async def request(factory, limit=499):
    assistant = await factory.get_rest_assistant()
    return await assistant.execute_request(
        url="https://fapi.binance.com/fapi/v1/klines",
        method=RESTMethod.GET,
        params={"limit": limit},
        throttler_limit_id="klines",
    )


@pytest.mark.parametrize("cls", [APIBinanceSpotCandles, APIBinancePerpetualCandles])
@pytest.mark.parametrize("limit", [0, 1, 50, 99, 100, 499, 500, 1500])
async def test_actual_candle_request_forwards_its_bounded_limit(cls, limit):
    feed = cls("BTC-USDT", "1m")
    transport = Transport()
    feed._api_factory = WebAssistantsFactory(feed._api_factory.throttler, connections_factory=transport)
    registry = BinanceRateLimits()
    name = "binance" if cls is APIBinanceSpotCandles else "binance_perpetual"
    registry.install(name, feed._api_factory, wait_on_cooldown=False)
    candles = await feed.fetch_candles(end_time=1_700_000_000, limit=limit)
    params = transport.requests[0].params
    assert params["limit"] == min(max(limit, 1), feed.candles_max_result_per_rest_request)
    assert params["endTime"] - params["startTime"] == params["limit"] * 60_000 - 1
    assert candles.shape == (0, 10)


@pytest.mark.parametrize("limit,weight", [(50, 1), (99, 1), (100, 2), (499, 2), (500, 5), (1000, 5), (1500, 10)])
async def test_futures_weight_tiers_are_used_at_the_transport(limit, weight):
    registry = BinanceRateLimits()
    client, transport = factory(registry)
    await request(client, limit)
    assert len(transport.requests) == 1
    logs = registry._budgets["binance_perpetual"].throttler._task_logs
    assert sum(log.weight for log in logs if log.rate_limit.limit_id == "shared_weight") == weight


async def test_two_independent_clients_consume_one_budget_and_cancellation_does_not_send():
    registry = BinanceRateLimits()
    first, one = factory(registry, limit=3)  # 80% headroom => budget 2
    second, two = factory(registry, limit=3)
    await request(first)  # 499 rows consumes both units
    pending = asyncio.create_task(request(second))
    await asyncio.sleep(0.02)
    assert len(one.requests) == 1
    assert two.requests == []
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending


@pytest.mark.parametrize("status", [418, 429])
async def test_rate_limit_blocks_other_clients_and_preserves_retry_after(status):
    registry = BinanceRateLimits()
    first, one = factory(registry, response=Response(status, {"Retry-After": "90"}))
    second, two = factory(registry)
    with pytest.raises(BinanceRateLimitError) as caught:
        await request(first)
    assert caught.value.status == status
    assert 89 <= caught.value.retry_after <= 90
    with pytest.raises(BinanceRateLimitError):
        await request(second)
    assert len(one.requests) == 1
    assert two.requests == []
    registry._budgets["binance_perpetual"].blocked_until = 0
    await request(second)
    assert len(two.requests) == 1


async def test_live_client_sleeps_during_ban_and_is_cancellable():
    registry = BinanceRateLimits()
    client, transport = factory(registry, wait=True)
    registry._budgets["binance_perpetual"].block(120, 418)
    pending = asyncio.create_task(request(client))
    await asyncio.sleep(0.02)
    assert not pending.done()
    assert transport.requests == []
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending


async def test_pending_capacity_wait_rechecks_a_new_ban():
    registry = BinanceRateLimits()
    client, transport = factory(registry, limit=3)
    budget = registry._budgets["binance_perpetual"]
    await request(client)
    pending = asyncio.create_task(request(client))
    await asyncio.sleep(0.02)
    budget.block(120, 418)
    # Advance just the local weight window, making the queued request admissible.
    for log in budget.throttler._task_logs:
        log.timestamp -= 65
    with pytest.raises(BinanceRateLimitError):
        await asyncio.wait_for(pending, 1)
    assert len(transport.requests) == 1


@pytest.mark.parametrize("kind", ["date", "ban_message", "invalid"])
async def test_retry_time_formats_and_missing_header_fallback(kind):
    registry = BinanceRateLimits()
    headers, payload = {}, {}
    if kind == "date":
        headers["Retry-After"] = formatdate(time.time() + 180, usegmt=True)
    elif kind == "ban_message":
        payload = {"code": -1003, "msg": f"IP banned until {int((time.time() + 180) * 1000)}"}
    else:
        headers["Retry-After"] = "invalid"
    client, _ = factory(registry, response=Response(418, headers, payload))
    with pytest.raises(BinanceRateLimitError) as caught:
        await request(client)
    expected = 120 if kind == "invalid" else 180
    assert expected - 1 <= caught.value.retry_after <= expected


async def test_header_usage_from_other_ip_consumers_stops_followup_request():
    registry = BinanceRateLimits()
    client, transport = factory(registry, response=Response(headers={"X-MBX-USED-WEIGHT-1M": "1100"}))
    assert await request(client) == []  # The successful result is still usable.
    with pytest.raises(BinanceRateLimitError):
        await request(client)
    assert len(transport.requests) == 1


async def test_product_domains_are_independent_and_install_is_idempotent():
    registry = BinanceRateLimits()
    futures, _ = factory(registry)
    spot, transport = factory(registry, name="binance")
    registry.install("binance", spot)
    assert len(spot._rest_pre_processors) == 1
    registry._budgets["binance_perpetual"].block(120, 418)
    await request(spot)
    assert len(transport.requests) == 1


def test_other_endpoints_reuse_the_connectors_weight_definitions():
    throttle = AsyncThrottler(
        [
            RateLimit("REQUEST_WEIGHT", 1200, 60),
            RateLimit("account", 1200, 60, linked_limits=[LinkedLimitWeightPair("REQUEST_WEIGHT", 5)]),
        ]
    )
    policy = BinanceRESTPolicy(BinanceRequestBudget(960, 60), throttle, False)
    req = RESTRequest(method=RESTMethod.GET, url="https://fapi.binance.com/fapi/v2/account", throttler_limit_id="account")
    assert policy.request_weight(req) == 5


def test_non_binance_factory_is_untouched():
    factory = SimpleNamespace()
    BinanceRateLimits().install("okx", factory)
    assert vars(factory) == {}


@pytest.mark.parametrize("name", ["binance", "binance_perpetual"])
async def test_real_keyless_connector_and_candle_feed_share_the_api_policy(name):
    from hummingbot.data_feed.candles_feed.data_types import CandlesConfig

    from services.market_data_service import MarketDataService
    from services.unified_connector_service import UnifiedConnectorService

    connectors = UnifiedConnectorService(secrets_manager=None)
    connector = connectors.get_best_connector_for_market(name)
    service = MarketDataService(connectors)
    feed = service._create_candle_feed(CandlesConfig(connector=name, trading_pair="BTC-USDT", interval="1m"), live=False)
    connector_policy = next(p for p in connector._web_assistants_factory._rest_pre_processors if isinstance(p, BinanceRESTPolicy))
    feed_policy = next(p for p in feed._api_factory._rest_pre_processors if isinstance(p, BinanceRESTPolicy))
    assert feed_policy.budget is connector_policy.budget
    assert feed._api_factory.throttler is connector.throttler
    assert feed._connector is connector
    assert not feed_policy.wait_on_cooldown
    assert connector_policy.wait_on_cooldown
    assert len(service._candle_feeds) == 0
    await feed._api_factory.close()
    await connector._web_assistants_factory.close()
