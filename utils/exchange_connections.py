"""Own Hummingbot's process-wide exchange sessions for the API lifespan."""

from contextlib import asynccontextmanager

import aiohttp
from hummingbot.core.web_assistant.connections.connections_factory import ConnectionsFactory


@asynccontextmanager
async def exchange_connections():
    """Keep persistent WebSockets out of the REST connection pool.

    Hummingbot's connection factory is a singleton. Its supported independent
    WebSocket session must be installed before any exchange clients start; a
    feed borrows these sessions and must never close them itself. Both pools
    remain bounded and are closed only after all exchange consumers stop.
    """
    factory = ConnectionsFactory()
    if factory._shared_client is not None or factory._ws_independent_session is not None:
        raise RuntimeError("Exchange connections must be initialized before exchange clients start")
    factory._ws_independent_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=100))
    try:
        yield factory
    finally:
        await factory.close()
