"""REST must remain available when persistent exchange WebSockets fill their pool."""

import asyncio

import aiohttp
import pytest
from aiohttp import web
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.web_assistant.connections.connections_factory import ConnectionsFactory
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory

from utils.exchange_connections import exchange_connections


@pytest.fixture(autouse=True)
def fresh_factory(monkeypatch):
    monkeypatch.setattr(ConnectionsFactory, "_instance", None)


async def test_full_websocket_pool_does_not_block_rest_and_shutdown_closes_both(monkeypatch):
    # Two real long-lived sockets reproduce exhaustion without opening 100 sockets.
    connector = aiohttp.TCPConnector
    monkeypatch.setattr("utils.exchange_connections.aiohttp.TCPConnector", lambda **_: connector(limit=2))

    async def socket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for _ in ws:
            pass
        return ws

    async def price(request):
        return web.json_response({"price": 105})

    app = web.Application()
    app.router.add_get("/ws", socket)
    app.router.add_get("/price", price)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    sockets = []
    try:
        async with exchange_connections() as factory:
            # Default factories used by real feeds and trading connectors must
            # inherit the same transport setup without per-feed injection.
            clients = [WebAssistantsFactory(AsyncThrottler([])) for _ in range(3)]
            assert all(client._connections_factory is factory for client in clients)
            for client in clients[:2]:
                ws = await client.get_ws_assistant()
                await ws.connect(ws_url=f"http://127.0.0.1:{port}/ws", ping_timeout=30)
                sockets.append(ws)
            ws_session = factory._ws_independent_session
            assert len(ws_session.connector._acquired) == 2

            rest = await clients[2].get_rest_assistant()
            response = await asyncio.wait_for(
                rest.call(RESTRequest(method=RESTMethod.GET, url=f"http://127.0.0.1:{port}/price")), timeout=1
            )
            assert await response.json() == {"price": 105}
            rest_session = factory._shared_client
            assert rest_session.connector is not ws_session.connector
            assert not rest_session.closed and not ws_session.closed

        assert rest_session.closed and ws_session.closed
        assert factory._shared_client is None
        assert factory._ws_independent_session is None
    finally:
        for ws in sockets:
            await ws.disconnect()
        await runner.cleanup()


async def test_startup_failure_closes_sessions_and_next_lifespan_can_start():
    with pytest.raises(ValueError, match="startup failed"):
        async with exchange_connections() as factory:
            await factory.get_rest_connection()
            rest, ws = factory._shared_client, factory._ws_independent_session
            raise ValueError("startup failed")
    assert rest.closed and ws.closed
    async with exchange_connections() as factory:
        assert factory._ws_independent_session is not ws


async def test_overlapping_lifespan_cannot_replace_an_active_pool():
    async with exchange_connections() as factory:
        ws = factory._ws_independent_session
        with pytest.raises(RuntimeError, match="before exchange clients start"):
            async with exchange_connections():
                pytest.fail("Active shared sessions were replaced")
        assert factory._ws_independent_session is ws
        assert not ws.closed
