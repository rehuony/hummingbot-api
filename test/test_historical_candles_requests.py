"""Exercise history pagination, cache/coalescing and HTTP errors without exchange I/O."""

import asyncio
from test.test_binance_request_policy import Response, Transport

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.data_feed.candles_feed.data_types import HistoricalCandlesConfig

from config import settings
from routers.market_data import router
from services.binance_candles import APIBinancePerpetualCandles
from services.binance_rate_limits import BinanceRateLimitError
from services.market_data_service import MarketDataService

START = 1_700_000_040


class CandleTransport(Transport):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.closed = False

    async def call(self, request):
        self.requests.append(request)
        self.owner.entered.set()
        if self.owner.release is not None:
            await self.owner.release.wait()
        await asyncio.sleep(0)
        if self.owner.error:
            raise self.owner.error
        params = request.params
        start = (params["startTime"] + 59_999) // 60_000 * 60_000
        timestamps = list(range(start, params["endTime"] + 1, 60_000))[: params["limit"]]
        rows = [[ts, 1, 2, 0.5, 1.5, 10, ts + 59_999, 15, 1, 4, 6, 0] for ts in timestamps]
        return Response(payload=[] if self.owner.empty else rows)

    async def close(self):
        self.closed = True


class Feeds:
    def __init__(self):
        self.feeds = []
        self.transports = []
        self.entered = asyncio.Event()
        self.release = None
        self.error = None
        self.empty = False

    def create(self, config):
        feed = APIBinancePerpetualCandles(config.trading_pair, config.interval, config.max_records)
        transport = CandleTransport(self)
        feed._api_factory = WebAssistantsFactory(feed._api_factory.throttler, connections_factory=transport)

        def fail_start():
            pytest.fail("A one-shot historical request started a live candle feed")

        feed.start = fail_start
        self.feeds.append(feed)
        self.transports.append(transport)
        return feed

    @property
    def requests(self):
        return [request for transport in self.transports for request in transport.requests]


@pytest.fixture
def history(monkeypatch, tmp_path):
    feeds = Feeds()
    monkeypatch.setattr("services.market_data_service.create_candle_feed", feeds.create)
    monkeypatch.setattr(settings.market_data, "historical_cache_path", str(tmp_path / "candles"))
    return MarketDataService(None), feeds


def config(*, count=50, offset=0, pair="BTC-USDT"):
    return HistoricalCandlesConfig(
        connector_name="binance_perpetual",
        trading_pair=pair,
        interval="1m",
        start_time=START + offset,
        end_time=START + (count - 1) * 60 + offset,
    )


@pytest.mark.parametrize("count,limits", [(1, [1]), (50, [50]), (499, [499]), (500, [499, 1]), (1000, [499, 499, 2])])
async def test_history_downloads_exact_inclusive_range_without_live_feed(history, count, limits):
    service, feeds = history
    result = await service.get_historical_candles(config(count=count))
    assert list(result.timestamp) == list(range(START, START + count * 60, 60))
    assert [request.params["limit"] for request in feeds.requests] == limits
    assert all(transport.closed for transport in feeds.transports if transport.requests)
    assert service._candle_feeds == {}
    assert all(feed._listen_candles_task is None and feed._fill_candles_task is None for feed in feeds.feeds)


async def test_concurrent_and_rolling_requests_reuse_data_and_do_not_share_mutation(history):
    service, feeds = history
    frames = await asyncio.gather(*(service.get_historical_candles(config(offset=offset)) for offset in range(10)))
    assert len(feeds.requests) == 1
    assert list(frames[0].timestamp) == list(range(START, START + 50 * 60, 60))
    assert list(frames[1].timestamp) == list(range(START + 60, START + 50 * 60, 60))
    frames[0].iloc[0, 1] = -999
    cached = await service.get_historical_candles(config())
    assert cached.iloc[0, 1] == 1
    assert len(feeds.requests) == 1
    assert service._historical_tasks == {}


async def test_different_pairs_and_ranges_do_not_share_cache_entries(history):
    service, feeds = history
    await service.get_historical_candles(config())
    await service.get_historical_candles(config(pair="ETH-USDT"))
    await service.get_historical_candles(config(count=51))
    assert len(feeds.requests) == 3


async def test_cancelling_one_caller_preserves_the_shared_download(history):
    service, feeds = history
    feeds.release = asyncio.Event()
    one = asyncio.create_task(service.get_historical_candles(config()))
    await feeds.entered.wait()
    two = asyncio.create_task(service.get_historical_candles(config()))
    await asyncio.sleep(0.01)
    one.cancel()
    with pytest.raises(asyncio.CancelledError):
        await one
    feeds.release.set()
    assert len(await two) == 50
    assert len(feeds.requests) == 1


async def test_timeout_closes_connections_and_next_request_can_retry(history, monkeypatch):
    service, feeds = history
    feeds.release = asyncio.Event()
    monkeypatch.setattr(settings.market_data, "candles_ready_timeout", 0.05)
    with pytest.raises(asyncio.TimeoutError):
        await service.get_historical_candles(config())
    assert service._historical_tasks == {}
    assert all(transport.closed for transport in feeds.transports if transport.requests)
    feeds.release.set()
    assert len(await service.get_historical_candles(config())) == 50


async def test_failure_and_empty_results_are_not_cached(history):
    service, feeds = history
    feeds.error = BinanceRateLimitError(120, 418)
    with pytest.raises(BinanceRateLimitError):
        await service.get_historical_candles(config())
    feeds.error = None
    feeds.empty = True
    assert (await service.get_historical_candles(config())).empty
    feeds.empty = False
    assert len(await service.get_historical_candles(config())) == 50
    assert len(feeds.requests) == 3


async def test_expired_cache_refetches_instead_of_serving_stale(history):
    service, feeds = history
    await service.get_historical_candles(config())
    service._historical_cache._ttl = 0
    await asyncio.sleep(0.01)
    await service.get_historical_candles(config())
    assert len(feeds.requests) == 2


async def test_cache_entry_count_is_bounded(history, monkeypatch):
    service, _ = history
    monkeypatch.setattr(settings.market_data, "historical_cache_entries", 2)
    for count in range(1, 5):
        await service.get_historical_candles(config(count=count))
    assert len(list(service._historical_cache._path.glob("*.pkl"))) == 2


async def test_shutdown_cancels_pending_downloads(history):
    service, feeds = history
    feeds.release = asyncio.Event()
    request = asyncio.create_task(service.get_historical_candles(config()))
    await feeds.entered.wait()
    service.stop()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert all(transport.closed for transport in feeds.transports if transport.requests)
    assert service._historical_tasks == {}


def test_route_preserves_rate_limit_status_and_retry_after():
    class Service:
        async def get_historical_candles(self, config):
            raise BinanceRateLimitError(180, 418)

    app = FastAPI()
    app.state.market_data_service = Service()
    app.include_router(router)
    with TestClient(app) as client:
        result = client.post("/market-data/historical-candles", json=config().model_dump())
    assert result.status_code == 429
    assert result.headers["retry-after"] == "180"
    assert "HTTP 418" in result.json()["detail"]
