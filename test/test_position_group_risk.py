"""Read-only quotes, exact two-leg sizing, and verified empty-account setup."""

import asyncio
import copy
from decimal import Decimal

import pytest

from services.executors.risk import RiskError, RiskService, liquidation_price


@pytest.fixture
def request_data():
    return {
        "account_name": "test",
        "connector_name": "binance_perpetual",
        "trading_pair": "ETH-USDT",
        "side": "long",
        "entry_low": "99",
        "entry_high": "101",
        "stop_price": "95",
        "tp1_price": "105",
        "tp2_price": "110",
        "margin": "10",
        "leverage": 10,
        "mode": "live",
        "entry_expires_at": 2000,
    }


class FakeConnector:
    def __init__(self):
        self.writes = []
        self.reads = []
        self.accept_changes = True
        self.data = {
            "v1/accountConfig": {"canTrade": True, "multiAssetsMargin": False, "dualSidePosition": False},
            "v1/symbolConfig": [{"symbol": "ETHUSDT", "marginType": "ISOLATED", "leverage": 10, "isAutoAddMargin": False}],
            "v3/account": {"assets": [{"asset": "USDT", "availableBalance": "1000"}]},
            "v3/positionRisk": [],
            "v1/openOrders": [],
            "v1/openAlgoOrders": [],
            "v1/leverageBracket": {
                "symbol": "ETHUSDT",
                "brackets": [
                    {
                        "notionalFloor": "0",
                        "notionalCap": "10000",
                        "maintMarginRatio": ".005",
                        "cum": "0",
                        "initialLeverage": 20,
                    }
                ],
            },
            "v1/commissionRate": {"takerCommissionRate": ".0004"},
            "v1/ticker/bookTicker": {"askPrice": "100", "bidPrice": "100", "time": 1000000},
            "v1/premiumIndex": {"markPrice": "100", "time": 1000000},
            "v1/exchangeInfo": {
                "symbols": [
                    {
                        "symbol": "ETHUSDT",
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                        "marginAsset": "USDT",
                        "quoteAsset": "USDT",
                        "filters": [
                            {"filterType": "LOT_SIZE", "stepSize": ".01", "minQty": ".01", "maxQty": "1000"},
                            {"filterType": "MARKET_LOT_SIZE", "stepSize": ".01", "minQty": ".01", "maxQty": "1000"},
                            {"filterType": "PRICE_FILTER", "tickSize": ".1"},
                            {"filterType": "MIN_NOTIONAL", "notional": "5"},
                        ],
                    }
                ]
            },
        }

    async def exchange_symbol_associated_to_pair(self, trading_pair):
        assert trading_pair == "ETH-USDT"
        return "ETHUSDT"

    async def _api_get(self, path_url, **kwargs):
        self.reads.append((path_url, kwargs))
        response = self.data[path_url]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)

    async def _api_post(self, path_url, data, **kwargs):
        self.writes.append((path_url, data))
        if self.accept_changes:
            self.data["v1/symbolConfig"][0].update({key: val for key, val in data.items() if key != "symbol"})
        return {"code": 200}


@pytest.fixture
def connector():
    return FakeConnector()


def service(connector):
    async def provider(request):
        return connector

    return RiskService(provider, clock=lambda: 1000)


@pytest.mark.parametrize("margin,quantity", [("10", "1"), ("20", "2"), ("50", "5"), ("12.34", "1.22")])
async def test_amount_means_margin_and_both_legs_round_down_without_increasing_it(request_data, connector, margin, quantity):
    request_data["margin"] = margin
    quote = await service(connector).quote(request_data)
    assert Decimal(quote["total_quantity"]) == Decimal(quantity)
    assert Decimal(quote["leg_quantities"][0]) == Decimal(quote["leg_quantities"][1]) == Decimal(quantity) / 2
    assert Decimal(quote["margin"]) <= Decimal(margin)
    assert Decimal(quote["notional"]) == Decimal(quantity) * 100
    assert Decimal(quote["max_loss"]) > Decimal(quantity) * 5  # Includes estimated open and close fees.
    assert quote["liquidation_price_kind"] == "estimate"
    assert connector.writes == []


async def test_preview_can_show_required_setup_but_never_performs_it(request_data, connector):
    connector.data["v1/symbolConfig"][0].update(marginType="CROSSED", leverage=20)
    quote = await service(connector).quote(request_data)
    assert quote["configuration_required"] is True
    assert connector.writes == []
    verified = await service(connector).prepare_live(request_data)
    assert verified["configuration_required"] is False
    assert [path for path, _ in connector.writes] == ["v1/marginType", "v1/leverage"]
    assert all(path not in ("v1/positionSide/dual", "v1/multiAssetsMargin", "v1/order") for path, _ in connector.writes)


async def test_success_response_without_actual_exchange_configuration_is_rejected(request_data, connector):
    connector.data["v1/symbolConfig"][0]["leverage"] = 20
    connector.accept_changes = False
    with pytest.raises(RiskError) as error:
        await service(connector).prepare_live(request_data)
    assert "did not confirm" in error.value.detail


@pytest.mark.parametrize(
    "path,payload",
    [
        ("v1/accountConfig", {"canTrade": True, "multiAssetsMargin": True, "dualSidePosition": False}),
        ("v1/accountConfig", {"canTrade": True, "dualSidePosition": False}),
        ("v1/symbolConfig", [{"symbol": "ETHUSDT", "marginType": "ISOLATED", "leverage": 10, "isAutoAddMargin": True}]),
        ("v3/positionRisk", [{"symbol": "ETHUSDT", "positionAmt": "0.1"}]),
        ("v1/openOrders", [{"orderId": 1}]),
        ("v1/openAlgoOrders", [{"algoId": 2}]),
        ("v3/account", {"assets": [{"asset": "USDT", "availableBalance": "9"}]}),
        ("v1/leverageBracket", IOError("signed-url-secret-must-not-escape")),
        ("v1/premiumIndex", {"markPrice": "100", "time": 900000}),
    ],
)
async def test_unknown_unsafe_or_busy_account_blocks_before_any_write(request_data, connector, path, payload):
    connector.data[path] = payload
    with pytest.raises(RiskError) as error:
        await service(connector).prepare_live(request_data)
    assert connector.writes == []
    assert "signed-url-secret" not in error.value.detail


async def test_mock_cannot_configure_even_an_empty_account(request_data, connector):
    request_data["mode"] = "mock"
    with pytest.raises(RiskError):
        await service(connector).prepare_live(request_data)
    assert connector.reads == [] and connector.writes == []
    await service(connector).quote(request_data)
    assert connector.writes == []


@pytest.mark.parametrize(
    "update,reason",
    [({"margin": ".5"}, "two valid"), ({"stop_price": "90"}, "liquidation"), ({"entry_expires_at": 999}, "expired")],
)
async def test_invalid_size_stop_and_expiry_are_refused(request_data, connector, update, reason):
    request_data.update(update)
    with pytest.raises(RiskError) as error:
        await service(connector).quote(request_data)
    assert reason in error.value.detail


async def test_limit_edge_is_rounded_inside_zone_and_quantity_can_also_close_at_market(request_data, connector):
    request_data.update(entry_low="99.5", entry_high="99.96")
    connector.data["v1/exchangeInfo"]["symbols"][0]["filters"][1]["stepSize"] = ".03"
    result = await service(connector).quote(request_data)
    assert result["entry_type"] == "LIMIT"
    assert Decimal(result["entry_price"]) == Decimal("99.9")
    assert Decimal(result["leg_quantities"][0]) == Decimal(".48")
    assert Decimal(result["min_base_amount_increment"]) == Decimal(".03")


@pytest.mark.parametrize(
    "update",
    [
        {"entry_low": "100.04", "entry_high": "100.5"},
        {"side": "short", "entry_low": "98.5", "entry_high": "99.5", "stop_price": "105", "tp1_price": "95", "tp2_price": "90"},
    ],
)
async def test_price_past_the_zone_toward_the_stop_is_refused_not_filled_at_once(request_data, connector, update):
    request_data.update(update)
    with pytest.raises(RiskError) as error:
        await service(connector).quote(request_data)
    assert "toward the stop" in error.value.detail


@pytest.mark.parametrize("side", ["long", "short"])
def test_liquidation_uses_tier_at_liquidation_not_entry_notional(side):
    brackets = [
        {"notionalFloor": "0", "notionalCap": "100", "maintMarginRatio": ".005", "cum": "0"},
        {"notionalFloor": "100", "notionalCap": "1000", "maintMarginRatio": ".01", "cum": ".5"},
    ]
    price = liquidation_price(side, Decimal("1"), Decimal("100"), Decimal("10"), brackets)
    if side == "long":
        assert price == Decimal("90") / Decimal(".995")
    else:
        assert price == Decimal("110.5") / Decimal("1.01")


async def test_a_position_arriving_between_configuration_writes_prevents_leverage_change(request_data, connector):
    connector.data["v1/symbolConfig"][0].update(marginType="CROSSED", leverage=20)
    post = connector._api_post

    async def appearing_position(*args, **kwargs):
        await post(*args, **kwargs)
        connector.data["v3/positionRisk"] = [{"symbol": "ETHUSDT", "positionAmt": "1"}]

    connector._api_post = appearing_position
    with pytest.raises(RiskError):
        await service(connector).prepare_live(request_data)
    assert [path for path, _ in connector.writes] == ["v1/marginType"]


@pytest.mark.parametrize("hedge", [False, True])
async def test_live_recovery_verifies_settings_without_empty_symbol_or_entry_ttl(request_data, connector, hedge):
    request_data["entry_expires_at"] = 999
    connector.data["v1/accountConfig"]["dualSidePosition"] = hedge
    connector.data["v3/positionRisk"] = [{"symbol": "ETHUSDT", "positionAmt": "1"}]
    connector.data["v1/openOrders"] = [{"orderId": 1}]
    connector.data["v3/account"] = IOError("Recovery must not quote another entry")
    await service(connector).verify_live(request_data, "HEDGE" if hedge else "ONEWAY")
    assert {path for path, _ in connector.reads} == {"v1/accountConfig", "v1/symbolConfig"}
    assert connector.writes == []


@pytest.mark.parametrize(
    "path,changes",
    [
        ("v1/accountConfig", {"canTrade": False}),
        ("v1/accountConfig", {"multiAssetsMargin": True}),
        ("v1/accountConfig", {"dualSidePosition": True}),
        ("v1/symbolConfig", {"marginType": "CROSSED"}),
        ("v1/symbolConfig", {"leverage": 20}),
        ("v1/symbolConfig", {"isAutoAddMargin": True}),
    ],
)
async def test_live_recovery_refuses_changed_risk_settings_without_reconfiguring(request_data, connector, path, changes):
    row = connector.data[path][0] if path == "v1/symbolConfig" else connector.data[path]
    row.update(changes)
    with pytest.raises(RiskError) as error:
        await service(connector).verify_live(request_data, "ONEWAY")
    assert error.value.status_code == 409
    assert connector.writes == []


@pytest.mark.parametrize(
    "path,missing",
    [
        ("v1/accountConfig", "canTrade"),
        ("v1/accountConfig", "multiAssetsMargin"),
        ("v1/accountConfig", "dualSidePosition"),
        ("v1/symbolConfig", "marginType"),
        ("v1/symbolConfig", "leverage"),
        ("v1/symbolConfig", "isAutoAddMargin"),
    ],
)
async def test_live_recovery_does_not_invent_missing_risk_settings(request_data, connector, path, missing):
    row = connector.data[path][0] if path == "v1/symbolConfig" else connector.data[path]
    row.pop(missing)
    with pytest.raises(RiskError) as error:
        await service(connector).verify_live(request_data, "ONEWAY")
    assert error.value.status_code == 502
    assert connector.writes == []


async def test_live_recovery_requires_the_persisted_position_mode(request_data, connector):
    with pytest.raises(RiskError) as error:
        await service(connector).verify_live(request_data, None)
    assert error.value.status_code == 502
    assert connector.reads == [] and connector.writes == []


@pytest.mark.parametrize("path", ["v1/accountConfig", "v1/symbolConfig"])
async def test_live_risk_read_timeout_is_bounded_and_remains_unavailable(request_data, connector, monkeypatch, path):
    monkeypatch.setattr("services.executors.risk.EXCHANGE_REQUEST_TIMEOUT", 0.01)
    original = connector._api_get

    async def stalled_read(path_url, **kwargs):
        if path_url == path:
            await asyncio.Event().wait()
        return await original(path_url, **kwargs)

    connector._api_get = stalled_read
    with pytest.raises(RiskError) as error:
        await asyncio.wait_for(service(connector).verify_live(request_data, "ONEWAY"), 0.5)
    assert error.value.status_code == 502
    assert "unavailable" in error.value.detail
    assert connector.writes == []


async def test_risk_connector_initialization_is_also_bounded(request_data, connector, monkeypatch):
    monkeypatch.setattr("services.executors.risk.EXCHANGE_REQUEST_TIMEOUT", 0.01)

    async def stalled_provider(request):
        await asyncio.Event().wait()

    with pytest.raises(RiskError) as error:
        await asyncio.wait_for(RiskService(stalled_provider).verify_live(request_data, "ONEWAY"), 0.5)
    assert error.value.status_code == 502
    assert connector.reads == [] and connector.writes == []


async def test_configuration_timeout_does_not_claim_success_or_continue_writes(request_data, connector, monkeypatch):
    monkeypatch.setattr("services.executors.risk.EXCHANGE_REQUEST_TIMEOUT", 0.01)
    connector.data["v1/symbolConfig"][0].update(marginType="CROSSED", leverage=20)
    original = connector._api_post

    async def accepted_without_ack(*args, **kwargs):
        await original(*args, **kwargs)
        await asyncio.Event().wait()

    connector._api_post = accepted_without_ack
    with pytest.raises(RiskError) as error:
        await asyncio.wait_for(service(connector).prepare_live(request_data), 0.5)
    assert error.value.status_code == 502
    assert "could not be confirmed" in error.value.detail
    assert [path for path, _ in connector.writes] == ["v1/marginType"]
    assert connector.data["v1/symbolConfig"][0]["leverage"] == 20
    connector._api_post = original
    await service(connector).prepare_live(request_data)
    assert [path for path, _ in connector.writes] == ["v1/marginType", "v1/leverage"]
