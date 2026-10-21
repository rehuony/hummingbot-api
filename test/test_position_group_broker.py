"""Submission uncertainty never creates a second order or hides a confirmed fill."""

import asyncio
import copy
from decimal import Decimal

import pytest

from services.executors.broker import Broker
from services.executors.risk import RiskError


@pytest.fixture
def request_data():
    return {"connector_name": "binance_perpetual", "trading_pair": "ETH-USDT", "side": "long"}


@pytest.fixture
def order():
    return {"client_order_id": "tc-leg-1-open", "side": "BUY", "amount": "1", "order_type": "MARKET", "position_action": "OPEN"}


class FakeExchange:
    def __init__(self):
        self.posts = []
        self.deletes = []
        self.queries = []
        self.post_error = None
        self.query_error = None
        self.cancel_error = None
        self.fill_error = None
        self.hedge = False
        self.position_rows = []
        self.order = {
            "symbol": "ETHUSDT",
            "clientOrderId": "tc-leg-1-open",
            "orderId": 17,
            "status": "FILLED",
            "executedQty": "1",
            "cumQuote": "100",
        }
        self.trades = [
            {
                "symbol": "ETHUSDT",
                "orderId": 17,
                "id": 12,
                "qty": "1",
                "quoteQty": "100",
                "price": "100",
                "commission": ".04",
                "commissionAsset": "USDT",
                "time": 1000000,
            }
        ]

    async def exchange_symbol_associated_to_pair(self, trading_pair):
        return "ETHUSDT"

    async def _api_get(self, path_url, params=None, **kwargs):
        self.queries.append((path_url, params))
        if path_url == "v1/accountConfig":
            return {"dualSidePosition": self.hedge}
        if path_url == "v1/order":
            if self.query_error:
                raise self.query_error
            return copy.deepcopy(self.order)
        if path_url == "v1/userTrades":
            if self.fill_error:
                raise self.fill_error
            return copy.deepcopy(self.trades)
        if path_url == "v3/positionRisk":
            return copy.deepcopy(self.position_rows)
        if path_url == "v1/premiumIndex":
            return {"markPrice": "100", "time": 1000000}
        raise AssertionError(path_url)

    async def _api_post(self, path_url, data, **kwargs):
        self.posts.append((path_url, data))
        if self.post_error:
            raise self.post_error
        return copy.deepcopy(self.order)

    async def _api_delete(self, path_url, params, **kwargs):
        self.deletes.append((path_url, params))
        if self.cancel_error:
            raise self.cancel_error
        return copy.deepcopy(self.order)


@pytest.fixture
def exchange():
    return FakeExchange()


def broker(exchange):
    async def provider(request):
        return exchange

    return Broker(provider, clock=lambda: 1000)


async def settled(adapter, request, order):
    observed = await adapter.reconcile(request, order)
    return await adapter.settle_fees(request, {**order, **observed})


async def test_post_timeout_after_acceptance_reconciles_the_same_stable_id(request_data, order, exchange):
    exchange.post_error = TimeoutError("response lost")
    result = await broker(exchange).submit(request_data, order)
    assert result["status"] == "FILLED"
    assert result["filled_base"] == "1"
    assert result["fees_complete"] is False
    assert not any(path == "v1/userTrades" for path, _ in exchange.queries)
    result = await broker(exchange).settle_fees(request_data, {**order, **result})
    assert result["fees_complete"] is True
    assert result["fills_complete"] is True
    assert result["fills"] == [
        {
            "trade_id": "12",
            "base_amount": "1",
            "quote_amount": "100",
            "price": "100",
            "commission_asset": "USDT",
            "commission": "0.04",
            "time": 1000000,
        }
    ]
    assert Decimal(result["fees_quote"]) == Decimal(".04")
    assert len(exchange.posts) == 1
    assert exchange.posts[0][1]["newClientOrderId"] == order["client_order_id"]
    assert ("v1/order", {"symbol": "ETHUSDT", "origClientOrderId": order["client_order_id"]}) in exchange.queries


async def test_timeout_with_not_found_remains_unknown_and_does_not_retry(request_data, order, exchange):
    exchange.post_error = TimeoutError()
    exchange.query_error = IOError('{"code": -2013, "msg": "Order does not exist"}')
    result = await broker(exchange).submit(request_data, order)
    assert result["status"] == "UNKNOWN"
    assert len(exchange.posts) == 1
    assert (await broker(exchange).reconcile(request_data, order))["status"] == "NOT_FOUND"


async def test_preflight_failure_is_known_unsent_and_can_retry_the_same_intent(request_data, order, exchange):
    original_get = exchange._api_get

    async def unavailable_config(path_url, **kwargs):
        if path_url == "v1/accountConfig":
            raise TimeoutError("Account configuration unavailable before submission")
        return await original_get(path_url, **kwargs)

    exchange._api_get = unavailable_config
    adapter = broker(exchange)
    result = await adapter.submit(request_data, order)
    assert result["status"] == "NOT_SUBMITTED" and result["attempted"] is False
    assert exchange.posts == []
    assert not any(path == "v1/order" for path, _ in exchange.queries)

    exchange._api_get = original_get
    assert (await adapter.submit(request_data, order))["status"] == "FILLED"
    assert len(exchange.posts) == 1
    assert exchange.posts[0][1]["newClientOrderId"] == order["client_order_id"]


async def test_preflight_interruption_does_not_claim_a_durable_unsent_result(request_data, order, exchange):
    async def interrupted_config(*args, **kwargs):
        raise asyncio.CancelledError()

    exchange._api_get = interrupted_config
    with pytest.raises(asyncio.CancelledError):
        await broker(exchange).submit(request_data, order)
    assert exchange.posts == []


async def test_explicit_exchange_rejection_is_failed_only_after_reconciliation(request_data, order, exchange):
    exchange.post_error = IOError('{"code": -2019, "msg": "Margin is insufficient"}')
    exchange.query_error = IOError('{"code": -2013, "msg": "Order does not exist"}')
    result = await broker(exchange).submit(request_data, order)
    assert result["status"] == "FAILED"
    assert result["error_code"] == -2019


@pytest.mark.parametrize("hedge", [False, True])
async def test_close_reduces_correct_position_without_opening_the_opposite_side(request_data, order, exchange, hedge):
    exchange.hedge = hedge
    order.update(side="SELL", position_action="CLOSE")
    await broker(exchange).submit(request_data, order)
    sent = exchange.posts[0][1]
    if hedge:
        assert sent["positionSide"] == "LONG" and "reduceOnly" not in sent
    else:
        assert sent["reduceOnly"] == "true" and "positionSide" not in sent


async def test_cancel_ack_lost_with_fill_winning_preserves_fill(request_data, order, exchange):
    exchange.cancel_error = IOError("cancel acknowledgement lost")
    result = await broker(exchange).cancel(request_data, order)
    assert result["status"] == "FILLED"
    assert result["filled_base"] == "1"
    assert exchange.deletes[0][1]["origClientOrderId"] == order["client_order_id"]


@pytest.mark.parametrize("incomplete", ["missing", "other_asset", "quantity_mismatch"])
async def test_missing_fee_detail_never_hides_fill_or_claims_complete_net_pnl(request_data, order, exchange, incomplete):
    if incomplete == "missing":
        exchange.fill_error = IOError("unavailable")
    elif incomplete == "other_asset":
        exchange.trades[0]["commissionAsset"] = "BNB"
    else:
        exchange.trades[0]["qty"] = ".5"
    result = await settled(broker(exchange), request_data, order)
    assert result["status"] == "FILLED" and result["filled_base"] == "1"
    assert result["fees_complete"] is False
    assert result["fills_complete"] is (incomplete == "other_asset")


@pytest.mark.parametrize("missing", ["price", "quoteQty", "time"])
async def test_missing_trade_fields_are_not_invented(request_data, order, exchange, missing):
    exchange.trades[0].pop(missing)
    result = await settled(broker(exchange), request_data, order)
    assert result["filled_base"] == "1" and result["status"] == "FILLED"
    assert result["fills"] == [] and result["fills_complete"] is False


async def test_full_fill_page_is_complete_when_cumulative_totals_match(request_data, order, exchange):
    exchange.trades = [
        {**exchange.trades[0], "id": index, "qty": ".001", "quoteQty": ".1", "commission": ".00004"} for index in range(1000)
    ]
    result = await settled(broker(exchange), request_data, order)
    assert len(result["fills"]) == 1000
    assert result["fills_complete"] is True and result["fees_complete"] is True


async def test_fill_cursor_collects_two_pages_and_reuses_complete_saved_evidence(request_data, order, exchange):
    exchange.order.update(executedQty="1.001", cumQuote="100.1")
    source = dict(exchange.trades[0])
    exchange.trades = [{**source, "id": index, "qty": ".001", "quoteQty": ".1", "commission": ".00004"} for index in range(1000)]
    adapter = broker(exchange)
    first = await settled(adapter, request_data, order)
    assert len(first["fills"]) == 1000 and first["fills_complete"] is False
    order.update(first)
    exchange.trades = [{**source, "id": 1000, "qty": ".001", "quoteQty": ".1", "commission": ".0001", "commissionAsset": "BNB"}]
    second = await settled(adapter, request_data, order)
    assert len(second["fills"]) == 1001 and second["fills_complete"] is True
    assert second["fees_complete"] is False
    assert Decimal(second["fees_quote"]) == Decimal(".04")
    assert {asset: Decimal(amount) for asset, amount in second["fee_assets"].items()} == {
        "USDT": Decimal(".04"),
        "BNB": Decimal(".0001"),
    }
    assert [params["fromId"] for path, params in exchange.queries if path == "v1/userTrades"] == [0, 1000]

    order.update(second)
    exchange.fill_error = IOError("The complete ledger must not fetch another page")
    third = await settled(adapter, request_data, order)
    assert third["fills"] == second["fills"] and third["fee_assets"] == second["fee_assets"]
    assert len([path for path, _ in exchange.queries if path == "v1/userTrades"]) == 2


async def test_failed_next_fill_page_preserves_saved_records_and_fees(request_data, order, exchange):
    exchange.trades[0].update(qty=".4", quoteQty="40", commission=".016", commissionAsset="BNB")
    adapter = broker(exchange)
    first = await settled(adapter, request_data, order)
    order.update(first)
    exchange.fill_error = IOError("Next fill page unavailable")
    second = await settled(adapter, request_data, order)
    assert second["fills"] == first["fills"] and second["fee_assets"] == first["fee_assets"]
    assert second["fills_complete"] is False and second["fees_complete"] is False


async def test_incomplete_fill_page_retains_exact_known_trades(request_data, order, exchange):
    exchange.trades[0].update(qty=".4", quoteQty="40", commission=".016")
    result = await settled(broker(exchange), request_data, order)
    assert result["fills"][0]["trade_id"] == "12" and result["fills"][0]["base_amount"] == "0.4"
    assert result["fills_complete"] is False and result["fees_complete"] is False


async def test_position_read_is_scoped_to_requested_side_and_reports_real_liquidation(request_data, exchange):
    exchange.position_rows = [
        {
            "symbol": "ETHUSDT",
            "positionSide": "LONG",
            "positionAmt": "1",
            "markPrice": "101",
            "liquidationPrice": "90.48",
            "isolatedWallet": "10",
        }
    ]
    snapshot = await broker(exchange).position_snapshot(request_data)
    assert snapshot["amount"] == "1" and snapshot["liquidation_price"] == "90.48"
    assert snapshot["liquidation_price_kind"] == "exchange"
    request_data["side"] = "short"
    with pytest.raises(RiskError):
        await broker(exchange).position_amount(request_data)


async def test_control_price_is_fresh_mark_price(request_data, exchange):
    assert await broker(exchange).price(request_data) == Decimal("100")
    stale_broker = Broker(lambda request: None, clock=lambda: 1100)
    stale_broker._provider = broker(exchange)._provider
    with pytest.raises(RiskError):
        await stale_broker.price(request_data)


async def test_malformed_response_never_invents_an_order_result(request_data, order, exchange):
    exchange.order.pop("executedQty")
    assert (await broker(exchange).reconcile(request_data, order))["status"] == "UNKNOWN"


@pytest.mark.parametrize("method", ["submit", "reconcile", "cancel"])
async def test_confirmed_fills_do_not_wait_for_fee_endpoint(request_data, order, exchange, method):
    original = exchange._api_get

    async def unavailable_fees(path_url, **kwargs):
        if path_url == "v1/userTrades":
            pytest.fail("Protection must persist confirmed fills before optional fee reads")
        return await original(path_url, **kwargs)

    exchange._api_get = unavailable_fees
    result = await getattr(broker(exchange), method)(request_data, order)
    assert result["status"] == "FILLED" and result["filled_base"] == "1"
    assert not result["fees_complete"]


@pytest.mark.parametrize("method", ["submit", "reconcile", "cancel"])
async def test_stalled_order_io_is_bounded_without_resubmitting(request_data, order, exchange, monkeypatch, method):
    monkeypatch.setattr("services.executors.broker.REQUEST_TIMEOUT", 0.01)
    original = exchange._api_get
    attempts = []

    async def stalled_write(*args, **kwargs):
        attempts.append(kwargs)
        await asyncio.Event().wait()

    async def query(path_url, **kwargs):
        if path_url == "v1/order":
            if method == "reconcile":
                await asyncio.Event().wait()
            raise IOError('{"code": -2013}')
        return await original(path_url, **kwargs)

    exchange._api_post = stalled_write
    exchange._api_delete = stalled_write
    exchange._api_get = query
    result = await asyncio.wait_for(getattr(broker(exchange), method)(request_data, order), timeout=1)
    assert result["status"] in {"UNKNOWN", "NOT_FOUND"}
    assert result["filled_base"] == "0"
    assert len(attempts) == (0 if method == "reconcile" else 1)


async def test_stalled_preflight_is_proven_unsent(request_data, order, exchange, monkeypatch):
    monkeypatch.setattr("services.executors.broker.REQUEST_TIMEOUT", 0.01)

    async def stalled_provider(request):
        await asyncio.Event().wait()

    result = await asyncio.wait_for(Broker(stalled_provider).submit(request_data, order), timeout=1)
    assert result["status"] == "NOT_SUBMITTED" and result["attempted"] is False
    assert exchange.posts == []


async def test_stalled_fee_read_preserves_confirmed_totals(request_data, order, exchange, monkeypatch):
    monkeypatch.setattr("services.executors.risk.EXCHANGE_REQUEST_TIMEOUT", 0.01)
    adapter = broker(exchange)
    observed = await adapter.reconcile(request_data, order)
    original = exchange._api_get

    async def stalled_fees(path_url, **kwargs):
        if path_url == "v1/userTrades":
            await asyncio.Event().wait()
        return await original(path_url, **kwargs)

    exchange._api_get = stalled_fees
    result = await asyncio.wait_for(adapter.settle_fees(request_data, {**order, **observed}), timeout=1)
    assert result["status"] == "FILLED" and result["filled_base"] == "1"
    assert not result["fees_complete"]
