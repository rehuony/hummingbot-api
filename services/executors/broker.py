"""Stable-ID order submission and reconciliation through an API-owned connector.

The group journal owns order intent and recovery. No new HTTP client, credential
reader, fire-and-forget submit, or automatic resubmission is introduced here.
"""

import asyncio
import re
import time
from decimal import Decimal

from services.executors.risk import RiskError, _rows, decimal, flag, read, value

REQUEST_TIMEOUT = 5.0

STATUS = {
    "NEW": "OPEN",
    "PARTIALLY_FILLED": "PARTIALLY_FILLED",
    "FILLED": "FILLED",
    "CANCELED": "CANCELED",
    "EXPIRED": "CANCELED",
    "EXPIRED_IN_MATCH": "CANCELED",
    "REJECTED": "FAILED",
}
# These are explicit Binance order rejections, not ambiguous transport failures.
REJECTIONS = {
    -1013,
    -1100,
    -1102,
    -1111,
    -1116,
    -1117,
    -1118,
    -1119,
    -1121,
    -2019,
    -2021,
    -2022,
    -4003,
    -4004,
    -4005,
    -4014,
    -4023,
    -4164,
}


def _code(error):
    match = re.search(r'["\']?code["\']?\s*:\s*(-\d+)', str(error))
    return int(match.group(1)) if match else None


def _validated_fill(fill):
    trade_id = str(fill.get("trade_id", ""))
    if not re.fullmatch(r"\d+", trade_id):
        raise RiskError("Invalid exchange trade ID", 502)
    timestamp = decimal(fill.get("time"), "fill timestamp", positive=True)
    if timestamp != timestamp.to_integral_value():
        raise RiskError("Invalid fill timestamp", 502)
    asset = fill.get("commission_asset")
    if not isinstance(asset, str) or not asset:
        raise RiskError("Commission asset unavailable", 502)
    return {
        "trade_id": str(int(trade_id)),
        "base_amount": str(decimal(fill.get("base_amount"), "fill quantity", positive=True)),
        "quote_amount": str(decimal(fill.get("quote_amount"), "fill quote quantity", positive=True)),
        "price": str(decimal(fill.get("price"), "fill price", positive=True)),
        "commission_asset": asset,
        "commission": str(decimal(fill.get("commission"), "commission")),
        "time": int(timestamp),
    }


def _fill_summary(fills, filled, quote):
    records = sorted(fills.values(), key=lambda fill: int(fill["trade_id"]))
    quantity = sum((Decimal(fill["base_amount"]) for fill in records), Decimal("0"))
    quote_amount = sum((Decimal(fill["quote_amount"]) for fill in records), Decimal("0"))
    assets = {}
    for fill in records:
        asset = fill["commission_asset"]
        assets[asset] = assets.get(asset, Decimal("0")) + Decimal(fill["commission"])
    complete = quantity == filled and quote_amount == quote
    return {
        "fills": records,
        "fills_complete": complete,
        "fees_quote": str(assets.get("USDT", Decimal("0"))),
        "fee_assets": {asset: str(amount) for asset, amount in assets.items()},
        "fees_complete": complete and all(asset == "USDT" for asset in assets),
    }


def _saved_fills(order):
    fills = {}
    for saved in _rows(order.get("fills", []), "saved order fills"):
        fill = _validated_fill(saved)
        if fill["trade_id"] in fills and fills[fill["trade_id"]] != fill:
            raise RiskError("Conflicting saved order fills", 502)
        fills[fill["trade_id"]] = fill
    return fills


class Broker:
    def __init__(self, connector_provider, *, clock=time.time):
        self._provider = connector_provider
        self._clock = clock

    async def _connector(self, request):
        if value(request, "connector_name") != "binance_perpetual":
            raise RiskError("Managed groups support Binance USDT perpetuals only", 400)
        async with asyncio.timeout(REQUEST_TIMEOUT):
            connector = await self._provider(request)
            symbol = await connector.exchange_symbol_associated_to_pair(value(request, "trading_pair"))
        return connector, symbol

    async def price(self, request):
        connector, symbol = await self._connector(request)
        result = await read(connector, "v1/premiumIndex", {"symbol": symbol}, authenticated=False)
        age = Decimal(str(self._clock())) * 1000 - decimal(result.get("time"), "mark price timestamp", positive=True)
        if age > 60000 or age < -5000:
            raise RiskError("Exchange mark price is stale", 502)
        return decimal(result.get("markPrice"), "mark price", positive=True)

    async def position_snapshot(self, request):
        connector, symbol = await self._connector(request)
        rows = _rows(await read(connector, "v3/positionRisk", {"symbol": symbol}), "positions")
        expected_side = "LONG" if value(request, "side") == "long" else "SHORT"
        matches = []
        for row in rows:
            if row.get("symbol") != symbol:
                raise RiskError("Unexpected symbol in position response", 502)
            amount = decimal(row.get("positionAmt"), "position amount")
            position_side = row.get("positionSide")
            if position_side not in ("BOTH", "LONG", "SHORT"):
                raise RiskError("Unknown exchange position side", 502)
            expected = position_side == expected_side or (position_side == "BOTH" and (amount > 0) == (expected_side == "LONG"))
            if amount != 0 and not expected:
                raise RiskError("An unmanaged opposite-side position exists on this symbol")
            if expected:
                if amount != 0:
                    matches.append(row)
        if len(matches) > 1:
            raise RiskError("Ambiguous exchange position state", 502)
        if not matches:
            return {"amount": "0", "liquidation_price": None}
        row = matches[0]
        liquidation = decimal(row.get("liquidationPrice"), "exchange liquidation price", nonnegative=True)
        return {
            "amount": str(abs(decimal(row["positionAmt"], "position amount"))),
            "liquidation_price": str(liquidation) if liquidation > 0 else None,
            "liquidation_price_kind": "exchange",
            "mark_price": str(decimal(row.get("markPrice"), "mark price", positive=True)),
            "isolated_wallet": str(decimal(row.get("isolatedWallet"), "isolated wallet", nonnegative=True)),
        }

    async def position_amount(self, request):
        return Decimal((await self.position_snapshot(request))["amount"])

    @staticmethod
    def _live(request):
        if value(request, "mode") != "live":
            raise RiskError("Dry-run requests cannot submit or cancel exchange orders", 400)

    @staticmethod
    def _unknown(order, status="UNKNOWN"):
        return {
            "status": status,
            "filled_base": str(order.get("filled_base", "0")),
            "filled_quote": str(order.get("filled_quote", "0")),
            "fees_quote": str(order.get("fees_quote", "0")),
            "fees_complete": False,
            "fills_complete": False,
        }

    async def _prepare_submission(self, request, order):
        """Resolve and validate a submission without sending an exchange order."""
        if not re.fullmatch(r"[.A-Z:/a-z0-9_-]{1,36}", order.get("client_order_id", "")):
            raise RiskError("Invalid stable client order ID", 400)
        if order.get("order_type") not in ("MARKET", "LIMIT") or order.get("position_action") not in ("OPEN", "CLOSE"):
            raise RiskError("Unsupported managed order", 400)
        opening = order["position_action"] == "OPEN"
        expected_side = "BUY" if value(request, "side") == "long" else "SELL"
        if not opening:
            expected_side = "SELL" if expected_side == "BUY" else "BUY"
        if order.get("side") != expected_side:
            raise RiskError("Order direction does not match the managed position", 400)
        amount = decimal(order.get("amount"), "order amount", positive=True)
        connector, symbol = await self._connector(request)
        account = await read(connector, "v1/accountConfig")
        hedge = flag(account.get("dualSidePosition"), "dualSidePosition")
        data = {
            "symbol": symbol,
            "side": order["side"],
            "type": order["order_type"],
            "quantity": format(amount, "f"),
            "newClientOrderId": order["client_order_id"],
            "newOrderRespType": "RESULT",
        }
        if hedge:
            data["positionSide"] = "LONG" if value(request, "side") == "long" else "SHORT"
        elif not opening:
            data["reduceOnly"] = "true"
        if order["order_type"] == "LIMIT":
            data.update(price=format(decimal(order.get("price"), "limit price", positive=True), "f"), timeInForce="GTC")
        return connector, symbol, data

    async def submit(self, request, order):
        self._live(request)
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                connector, symbol, data = await self._prepare_submission(request, order)
        except asyncio.CancelledError:
            raise
        except Exception:
            # This return proves no order request was attempted. The durable owner
            # may restore PREPARED and retry this intent. A process interruption
            # before this result is committed remains SUBMITTING/UNKNOWN instead.
            return {
                **self._unknown(order, "NOT_SUBMITTED"),
                "attempted": False,
                "error": "Submission preflight unavailable; no exchange order request was sent",
            }
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                result = await connector._api_post(path_url="v1/order", data=data, is_auth_required=True, limit_id="POSTv1/order")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # A timeout can happen after acceptance. Reconcile the persisted ID;
            # the caller must never replace it with a new ID and submit again.
            reconciled = await self.reconcile(request, order)
            if reconciled["status"] == "NOT_FOUND":
                if _code(error) in REJECTIONS:
                    return {**self._unknown(order, "FAILED"), "error_code": _code(error)}
                return self._unknown(order)
            return reconciled
        return await self._result(connector, symbol, order, result)

    async def reconcile(self, request, order):
        connector, symbol = await self._connector(request)
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                result = await connector._api_get(
                    path_url="v1/order",
                    params={"symbol": symbol, "origClientOrderId": order["client_order_id"]},
                    is_auth_required=True,
                    limit_id="GETv1/order",
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return self._unknown(order, "NOT_FOUND" if _code(error) == -2013 else "UNKNOWN")
        return await self._result(connector, symbol, order, result)

    async def cancel(self, request, order):
        self._live(request)
        connector, symbol = await self._connector(request)
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                result = await connector._api_delete(
                    path_url="v1/order",
                    params={"symbol": symbol, "origClientOrderId": order["client_order_id"]},
                    is_auth_required=True,
                    limit_id="DELETEv1/order",
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            return await self.reconcile(request, order)
        return await self._result(connector, symbol, order, result)

    async def _result(self, connector, symbol, order, result):
        if (
            not isinstance(result, dict)
            or result.get("symbol") != symbol
            or result.get("clientOrderId") != order["client_order_id"]
            or result.get("status") not in STATUS
            or result.get("orderId") is None
        ):
            return self._unknown(order)
        try:
            filled = decimal(result.get("executedQty"), "executed quantity", nonnegative=True)
            quote = decimal(result.get("cumQuote"), "executed quote", nonnegative=True)
        except RiskError:
            return self._unknown(order)
        response = {
            "status": STATUS[result["status"]],
            "filled_base": str(filled),
            "filled_quote": str(quote),
            "exchange_order_id": str(result["orderId"]),
            "fees_quote": str(order.get("fees_quote", "0")),
            "fees_complete": filled == 0,
            "fee_assets": dict(order.get("fee_assets") or {}),
            "fills": [],
            "fills_complete": filled == 0,
        }
        if not filled:
            return response
        try:
            fills = _saved_fills(order)
            if fills:
                response.update(_fill_summary(fills, filled, quote))
        except RiskError:
            pass
        # Confirmed cumulative fills must reach the durable owner before any
        # optional fee I/O. The separate settlement worker collects trade pages.
        return response

    async def settle_fees(self, request, order):
        """Collect one trade page for a previously persisted execution snapshot."""
        response = dict(order)
        filled = decimal(order.get("filled_base"), "executed quantity", nonnegative=True)
        quote = decimal(order.get("filled_quote"), "executed quote", nonnegative=True)
        if not filled or not order.get("exchange_order_id"):
            return response
        try:
            fills = _saved_fills(order)
            response.update(_fill_summary(fills, filled, quote))
            if response["fills_complete"]:
                return response
            connector, symbol = await self._connector(request)
            # One bounded page per observation. The persisted ledger is the cursor;
            # complete evidence is reused, and partial evidence survives failures.
            trades = _rows(
                await read(
                    connector,
                    "v1/userTrades",
                    {
                        "symbol": symbol,
                        "orderId": order["exchange_order_id"],
                        "fromId": max((int(trade_id) for trade_id in fills), default=-1) + 1,
                        "limit": 1000,
                    },
                ),
                "order fills",
            )
            combined = dict(fills)
            for trade in trades:
                if str(trade.get("orderId")) != str(order["exchange_order_id"]) or trade.get("symbol") != symbol:
                    raise RiskError("Unexpected order fill", 502)
                fill = _validated_fill(
                    {
                        "trade_id": trade.get("id"),
                        "base_amount": trade.get("qty"),
                        "quote_amount": trade.get("quoteQty"),
                        "price": trade.get("price"),
                        "commission_asset": trade.get("commissionAsset"),
                        "commission": trade.get("commission"),
                        "time": trade.get("time"),
                    }
                )
                if fill["trade_id"] in combined and combined[fill["trade_id"]] != fill:
                    raise RiskError("Conflicting exchange order fills", 502)
                combined[fill["trade_id"]] = fill
            if combined:
                response.update(_fill_summary(combined, filled, quote))
        except RiskError:
            # Fills remain actionable even while fee data is delayed or unavailable.
            pass
        return response
