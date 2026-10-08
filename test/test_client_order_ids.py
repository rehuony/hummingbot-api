"""Chinese symbols must not leak into exchange-restricted client order IDs."""

import re
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hummingbot.connector import exchange_py_base
from hummingbot.connector import utils as connector_utils
from hummingbot.connector.derivative.binance_perpetual.binance_perpetual_derivative import BinancePerpetualDerivative
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, TradeType

from utils.client_order_ids import get_ascii_client_order_id, install_client_order_id_compatibility

VALID_ORDER_ID = re.compile(r"[.A-Za-z0-9_:/-]{1,36}")


@pytest.fixture
def installed(monkeypatch):
    # Restore both shared bindings after each test.
    monkeypatch.setattr(connector_utils, "get_new_client_order_id", connector_utils.get_new_client_order_id)
    monkeypatch.setattr(exchange_py_base, "get_new_client_order_id", exchange_py_base.get_new_client_order_id)
    install_client_order_id_compatibility()


@pytest.mark.parametrize("pair", ["牛来-USDT", "币-人民币", "éTH-USDT", "🐂-USDT", "BTC-USDT"])
@pytest.mark.parametrize("is_buy", [False, True])
@pytest.mark.parametrize("max_len", [24, 32, 36])
def test_generated_ids_follow_exchange_charset_and_length(pair, is_buy, max_len):
    order_id = get_ascii_client_order_id(is_buy, pair, "x-nbQe1H39", max_len)
    assert VALID_ORDER_ID.fullmatch(order_id)
    assert len(order_id) <= max_len
    assert order_id.startswith("x-nbQe1H39" + ("B" if is_buy else "S"))


def test_ascii_pair_keeps_existing_identifier(monkeypatch):
    monkeypatch.setattr(connector_utils, "get_tracking_nonce", lambda: 123456789)
    monkeypatch.setattr(connector_utils, "_bot_instance_id", lambda: "a" * 32)
    assert get_ascii_client_order_id(False, "BTC-USDT", "x-nbQe1H39", 32) == ("x-nbQe1H39SBCUT75bcd15" + "a" * 10)


def test_unicode_normalization_keeps_nonce_unique(installed):
    ids = {connector_utils.get_new_client_order_id(False, "牛来-USDT", "x-nbQe1H39", 32) for _ in range(1000)}
    assert len(ids) == 1000
    assert all(VALID_ORDER_ID.fullmatch(order_id) for order_id in ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("method,trade_type", [("sell", TradeType.SELL), ("buy", TradeType.BUY)])
async def test_order_submission_preserves_symbol_amount_price_and_close_action(installed, monkeypatch, method, trade_type):
    pending = []
    monkeypatch.setattr(exchange_py_base, "safe_ensure_future", pending.append)
    connector = SimpleNamespace(
        client_order_id_prefix="x-nbQe1H39",
        client_order_id_max_length=32,
        _create_order=AsyncMock(),
    )
    order_id = getattr(exchange_py_base.ExchangePyBase, method)(
        connector,
        trading_pair="牛来-USDT",
        amount=Decimal("835"),
        order_type=OrderType.LIMIT,
        price=Decimal("0.07270"),
        position_action=PositionAction.CLOSE,
    )
    for coroutine in pending:
        await coroutine
    assert VALID_ORDER_ID.fullmatch(order_id)
    connector._create_order.assert_awaited_once_with(
        trade_type=trade_type,
        order_id=order_id,
        trading_pair="牛来-USDT",
        amount=Decimal("835"),
        order_type=OrderType.LIMIT,
        price=Decimal("0.07270"),
        position_action=PositionAction.CLOSE,
    )


def test_install_is_idempotent(installed):
    install_client_order_id_compatibility()
    assert connector_utils.get_new_client_order_id is get_ascii_client_order_id
    assert exchange_py_base.get_new_client_order_id is get_ascii_client_order_id
    assert VALID_ORDER_ID.fullmatch(connector_utils.get_new_client_order_id(False, "牛来-USDT", "x-nbQe1H39", 32))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [PositionMode.HEDGE, PositionMode.ONEWAY])
async def test_binance_close_payload_keeps_chinese_symbol_and_position_safety(mode):
    connector = SimpleNamespace(
        exchange_symbol_associated_to_pair=AsyncMock(return_value="牛来USDT"),
        position_mode=mode,
        _api_post=AsyncMock(return_value={"orderId": 123, "updateTime": 1791437211000}),
    )
    order_id = get_ascii_client_order_id(False, "牛来-USDT", "x-nbQe1H39", 32)
    response = await BinancePerpetualDerivative._place_order(
        connector,
        order_id,
        "牛来-USDT",
        Decimal("835"),
        TradeType.SELL,
        OrderType.LIMIT,
        Decimal("0.07270"),
        position_action=PositionAction.CLOSE,
    )
    payload = connector._api_post.await_args.kwargs["data"]
    assert VALID_ORDER_ID.fullmatch(payload["newClientOrderId"])
    assert payload["symbol"] == "牛来USDT"
    assert payload["side"] == "SELL"
    assert payload["quantity"] == "835"
    assert payload["price"] == "0.07270"
    assert payload["type"] == "LIMIT"
    assert payload["timeInForce"] == "GTC"
    if mode == PositionMode.HEDGE:
        assert payload["positionSide"] == "LONG"
        assert "reduceOnly" not in payload
    else:
        assert payload["reduceOnly"] == "true"
        assert "positionSide" not in payload
    assert response == ("123", 1791437211.0)
