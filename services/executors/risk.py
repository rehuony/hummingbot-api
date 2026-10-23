"""Binance USDT isolated-position preflight, using the existing authenticated connector.

Preview performs reads only. Exchange configuration writes are confined to
``prepare_live`` and require an empty symbol. Liquidation before a fill is an
estimate; it must never be presented as an execution guarantee.
"""

import asyncio
import time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from math import lcm

from fastapi import HTTPException

ZERO = Decimal("0")
ONE = Decimal("1")
EXCHANGE_REQUEST_TIMEOUT = 5.0


class RiskError(HTTPException):
    def __init__(self, detail, status_code=409):
        super().__init__(status_code=status_code, detail=detail)


def value(request, key):
    return request[key] if isinstance(request, dict) else getattr(request, key)


def decimal(value_, name, *, positive=False, nonnegative=False):
    try:
        result = Decimal(str(value_))
    except (InvalidOperation, ValueError, TypeError):
        raise RiskError(f"Missing or invalid {name}", 502) from None
    if not result.is_finite() or (positive and result <= 0) or (nonnegative and result < 0):
        raise RiskError(f"Missing or invalid {name}", 502)
    return result


def flag(value_, name):
    if value_ is True or value_ == "true":
        return True
    if value_ is False or value_ == "false":
        return False
    raise RiskError(f"Missing or invalid {name}", 502)


def quantize(amount, step, rounding=ROUND_FLOOR):
    return (amount / step).to_integral_value(rounding=rounding) * step


def common_step(first, second):
    scale = Decimal(10) ** -min(first.as_tuple().exponent, second.as_tuple().exponent)
    return Decimal(lcm(int(first * scale), int(second * scale))) / scale


async def read(connector, path, params=None, *, authenticated=True):
    """Retain connector authentication, retry policy and process-wide Binance budget.

    New account endpoints share the existing account limiter (weight 5). This
    conservatively overcounts the weight-1 reads rather than bypassing throttling.
    """
    try:
        async with asyncio.timeout(EXCHANGE_REQUEST_TIMEOUT):
            return await connector._api_get(
                path_url=path,
                params=params or {},
                is_auth_required=authenticated,
                limit_id="v2/account" if authenticated else path,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        # Connector exceptions may contain signed URLs. Do not expose them in API responses.
        raise RiskError(f"Exchange risk data unavailable ({path})", 502) from None


def _rows(payload, name):
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise RiskError(f"Invalid exchange {name} response", 502)
    return payload


def _symbol_config(payload, symbol):
    matches = [row for row in _rows(payload, "symbol configuration") if row.get("symbol") == symbol]
    if len(matches) != 1:
        raise RiskError("Exchange symbol configuration unavailable", 502)
    row = matches[0]
    if row.get("marginType") not in ("ISOLATED", "CROSSED"):
        raise RiskError("Unknown exchange margin mode", 502)
    flag(row.get("isAutoAddMargin"), "isAutoAddMargin")
    decimal(row.get("leverage"), "leverage", positive=True)
    return row


def _account_mode(account):
    if not isinstance(account, dict):
        raise RiskError("Invalid exchange account configuration", 502)
    if not flag(account.get("canTrade"), "canTrade"):
        raise RiskError("Account is not enabled for trading")
    if flag(account.get("multiAssetsMargin"), "multiAssetsMargin"):
        raise RiskError("Single-asset USDT mode is required; account mode will not be changed")
    return "HEDGE" if flag(account.get("dualSidePosition"), "dualSidePosition") else "ONEWAY"


def leverage_of(request):
    """The group's whole-number leverage; persisted requests without one used 10x."""
    leverage = request.get("leverage", 10) if isinstance(request, dict) else request.leverage
    if isinstance(leverage, bool) or not isinstance(leverage, int) or leverage < 1:
        raise RiskError("Leverage must be a whole number of at least 1x", 400)
    return leverage


def _initial_leverage(row):
    return int(decimal(row.get("initialLeverage"), "maximum leverage", positive=True))


def notional_cap(brackets, leverage):
    """The largest position the user's brackets allow at this leverage (zero if none)."""
    return max(
        (decimal(row.get("notionalCap"), "notionalCap", positive=True) for row in brackets if _initial_leverage(row) >= leverage),
        default=ZERO,
    )


def liquidation_price(side, quantity, entry, collateral, brackets):
    """Solve isolated equity = maintenance margin in the resulting notional tier.

    Maintenance = abs(quantity) * mark * maintenance_ratio - cumulative_amount.
    The authenticated endpoint returns the user's brackets; do not multiply those
    already returned bounds a second time by the informational notionalCoef.
    """
    if collateral <= 0:
        raise RiskError("Margin is insufficient after estimated entry fees")
    for bracket in brackets:
        floor = decimal(bracket.get("notionalFloor"), "notionalFloor", nonnegative=True)
        cap = decimal(bracket.get("notionalCap"), "notionalCap", positive=True)
        ratio = decimal(bracket.get("maintMarginRatio"), "maintMarginRatio", nonnegative=True)
        cumulative = decimal(bracket.get("cum"), "maintenance amount", nonnegative=True)
        if cap <= floor or ratio >= 1:
            raise RiskError("Invalid maintenance-margin bracket", 502)
        if side == "long":
            price = (quantity * entry - collateral - cumulative) / (quantity * (ONE - ratio))
        else:
            price = (quantity * entry + collateral + cumulative) / (quantity * (ONE + ratio))
        if price > 0 and floor <= quantity * price < cap:
            return price
    raise RiskError("Cannot determine a valid liquidation-price risk bracket")


class RiskService:
    def __init__(self, connector_provider, *, clock=time.time):
        self._provider = connector_provider
        self._clock = clock

    async def _connector(self, request):
        try:
            async with asyncio.timeout(EXCHANGE_REQUEST_TIMEOUT):
                connector = await self._provider(request)
                symbol = await connector.exchange_symbol_associated_to_pair(value(request, "trading_pair"))
                return connector, symbol
        except asyncio.CancelledError:
            raise
        except HTTPException:
            raise
        except Exception:
            raise RiskError("Exchange connector unavailable", 502) from None

    async def verify_live(self, request, position_mode):
        """Recheck persisted risk settings without requiring an empty, unexpired trade.

        Recovery may already own fills or orders. Never configure the account here;
        a mismatch suspends new entries while the caller preserves known exposure.
        """
        if value(request, "mode") != "live":
            raise RiskError("Live risk verification requires a live group", 400)
        if value(request, "connector_name") != "binance_perpetual" or not value(request, "trading_pair").endswith("-USDT"):
            raise RiskError("Managed position groups require Binance USDT perpetuals", 400)
        leverage = leverage_of(request)
        if position_mode not in ("HEDGE", "ONEWAY"):
            raise RiskError("Persisted position mode is unavailable", 502)
        connector, symbol = await self._connector(request)
        account, configs = await asyncio.gather(
            read(connector, "v1/accountConfig"), read(connector, "v1/symbolConfig", {"symbol": symbol})
        )
        if _account_mode(account) != position_mode:
            raise RiskError("Exchange position mode differs from the managed group's confirmed mode")
        config = _symbol_config(configs, symbol)
        if config["marginType"] != "ISOLATED" or decimal(config["leverage"], "leverage", positive=True) != leverage:
            raise RiskError(f"Exchange did not confirm isolated margin and {leverage}x leverage")
        if flag(config["isAutoAddMargin"], "isAutoAddMargin"):
            raise RiskError("Disable automatic margin addition in Binance before opening a managed group")

    async def _context(self, request):
        if value(request, "connector_name") != "binance_perpetual" or not value(request, "trading_pair").endswith("-USDT"):
            raise RiskError("Managed position groups require Binance USDT perpetuals", 400)
        leverage_of(request)
        if value(request, "entry_expires_at") <= self._clock():
            raise RiskError("Trade plan has expired")
        connector, symbol = await self._connector(request)
        responses = await asyncio.gather(
            read(connector, "v1/accountConfig"),
            read(connector, "v1/symbolConfig", {"symbol": symbol}),
            read(connector, "v3/account"),
            read(connector, "v3/positionRisk", {"symbol": symbol}),
            read(connector, "v1/openOrders", {"symbol": symbol}),
            read(connector, "v1/openAlgoOrders", {"symbol": symbol}),
            read(connector, "v1/leverageBracket", {"symbol": symbol}),
            read(connector, "v1/commissionRate", {"symbol": symbol}),
            read(connector, "v1/exchangeInfo", authenticated=False),
            read(connector, "v1/ticker/bookTicker", {"symbol": symbol}, authenticated=False),
            read(connector, "v1/premiumIndex", {"symbol": symbol}, authenticated=False),
        )
        account_config, configs, account, positions, orders, algo_orders, tier, fees, exchange, ticker, mark = responses
        if not isinstance(account, dict):
            raise RiskError("Invalid exchange account response", 502)
        position_mode = _account_mode(account_config)
        config = _symbol_config(configs, symbol)
        if flag(config.get("isAutoAddMargin"), "isAutoAddMargin"):
            raise RiskError("Disable automatic margin addition in Binance before opening a managed group")
        self._assert_empty(symbol, positions, orders, algo_orders)
        assets = [row for row in _rows(account.get("assets"), "account assets") if row.get("asset") == "USDT"]
        if len(assets) != 1:
            raise RiskError("USDT account balance unavailable", 502)
        available = decimal(assets[0].get("availableBalance"), "USDT available balance", nonnegative=True)
        if isinstance(tier, list):
            matching = [row for row in tier if isinstance(row, dict) and row.get("symbol") == symbol]
            tier = matching[0] if len(matching) == 1 else None
        if not isinstance(tier, dict) or tier.get("symbol") != symbol:
            raise RiskError("User leverage brackets unavailable", 502)
        brackets = _rows(tier.get("brackets"), "leverage brackets")
        if not brackets:
            raise RiskError("User leverage brackets unavailable", 502)
        if not isinstance(fees, dict) or not isinstance(ticker, dict) or not isinstance(mark, dict):
            raise RiskError("Market prices or commission rates unavailable", 502)
        for data, name in ((ticker, "entry quote"), (mark, "mark price")):
            stamp = decimal(data.get("time"), f"{name} timestamp", positive=True)
            age = Decimal(str(self._clock())) * 1000 - stamp
            if age > 60000 or age < -5000:
                raise RiskError(f"Exchange {name} is stale", 502)
        fee_rate = decimal(fees.get("takerCommissionRate"), "taker commission rate", nonnegative=True)
        if fee_rate >= 1:
            raise RiskError("Invalid taker commission rate", 502)
        if not isinstance(exchange, dict):
            raise RiskError("Exchange trading rules unavailable", 502)
        markets = [row for row in _rows(exchange.get("symbols"), "exchange symbols") if row.get("symbol") == symbol]
        if len(markets) != 1 or markets[0].get("status") != "TRADING" or markets[0].get("contractType") != "PERPETUAL":
            raise RiskError("The selected USDT perpetual market is unavailable")
        if markets[0].get("marginAsset") != "USDT" or markets[0].get("quoteAsset") != "USDT":
            raise RiskError("Only USDT-margined contracts are supported")
        return {
            "connector": connector,
            "symbol": symbol,
            "config": config,
            "position_mode": position_mode,
            "available": available,
            "brackets": brackets,
            "fee_rate": fee_rate,
            "market": markets[0],
            "current": decimal(
                ticker.get("askPrice" if value(request, "side") == "long" else "bidPrice"), "entry quote", positive=True
            ),
            "mark": decimal(mark.get("markPrice"), "mark price", positive=True),
            "as_of": self._clock(),
        }

    @staticmethod
    def _assert_empty(symbol, positions, orders, algo_orders):
        for row in _rows(positions, "positions"):
            if row.get("symbol") != symbol:
                raise RiskError("Unexpected symbol in position response", 502)
            if decimal(row.get("positionAmt"), "position amount") != 0:
                raise RiskError("This symbol already has an exchange position")
            if decimal(row.get("isolatedWallet"), "isolated wallet", nonnegative=True) != 0:
                raise RiskError("This symbol already has allocated isolated margin")
        if _rows(orders, "open orders") or _rows(algo_orders, "conditional orders"):
            raise RiskError("This symbol already has open or conditional orders")

    @staticmethod
    def _terms(request, context):
        """Plan prices, exchange filters and the entry this price would get.

        ``blocked`` says why the price cannot enter now; the entry is then the
        zone edge it would wait for, so limits stay visible while it waits.
        """
        current, mark = context["current"], context["mark"]
        low, high = value(request, "entry_low"), value(request, "entry_high")
        low, high = decimal(low, "entry_low", positive=True), decimal(high, "entry_high", positive=True)
        stop = decimal(value(request, "stop_price"), "stop_price", positive=True)
        tp1 = decimal(value(request, "tp1_price"), "tp1_price", positive=True)
        tp2 = decimal(value(request, "tp2_price"), "tp2_price", positive=True)
        side = value(request, "side")
        blocked = None
        if (side == "long" and not stop < mark < tp1) or (side == "short" and not tp1 < mark < stop):
            blocked = "Price has already passed the stop or first target"
        elif current < low if side == "long" else current > high:
            # A limit at the zone's far edge would be marketable and fill at once
            # outside the plan, after the pullback already broke through it.
            blocked = "Price is past the entry zone toward the stop; wait for it to return"
        filters = {row.get("filterType"): row for row in _rows(context["market"].get("filters"), "trading filters")}
        lot, market_lot = filters.get("LOT_SIZE"), filters.get("MARKET_LOT_SIZE")
        price_filter, notional = filters.get("PRICE_FILTER"), filters.get("MIN_NOTIONAL")
        if not all(isinstance(part, dict) for part in (lot, market_lot, price_filter, notional)):
            raise RiskError("Required exchange trading filters unavailable", 502)
        # Entry can be LIMIT; protective and manual reductions are MARKET. Each
        # leg must be valid for both so an accepted entry remains closeable.
        step = common_step(
            decimal(lot.get("stepSize"), "quantity step", positive=True),
            decimal(market_lot.get("stepSize"), "market quantity step", positive=True),
        )
        tick = decimal(price_filter.get("tickSize"), "price tick", positive=True)
        entry_type = "MARKET" if low <= current <= high else "LIMIT"
        if entry_type == "MARKET":
            entry = current
        elif side == "short":
            entry = quantize(low, tick, ROUND_CEILING)
        else:
            entry = quantize(high, tick)
        if entry_type == "LIMIT" and not low <= entry <= high:
            raise RiskError("Entry zone has no price supported by the exchange tick size")
        return {
            "side": side,
            "stop": stop,
            "tp1": tp1,
            "tp2": tp2,
            "current": current,
            "mark": mark,
            "entry_type": entry_type,
            "entry": entry,
            "blocked": blocked,
            "step": step,
            "minimum": max(decimal(item.get("minQty"), "minimum quantity", positive=True) for item in (lot, market_lot)),
            "maximum": min(decimal(item.get("maxQty"), "maximum quantity", positive=True) for item in (lot, market_lot)),
            "min_notional": decimal(notional.get("notional"), "minimum notional", positive=True),
        }

    def _quote(self, request, context):
        terms = self._terms(request, context)
        if terms["blocked"]:
            raise RiskError(terms["blocked"])
        side, stop, tp1, tp2 = terms["side"], terms["stop"], terms["tp1"], terms["tp2"]
        current, mark, entry, step = terms["current"], terms["mark"], terms["entry"], terms["step"]
        leverage = leverage_of(request)
        requested_margin = decimal(value(request, "margin"), "margin", positive=True)
        leg = quantize(requested_margin * leverage / entry / 2, step)
        if leg < terms["minimum"] or leg * min(entry, mark) < terms["min_notional"]:
            raise RiskError("The selected margin is too small for two valid equal-size legs")
        if leg > terms["maximum"]:
            raise RiskError("Each leg exceeds the exchange maximum order quantity")
        total = leg * 2
        notional_amount, margin = total * entry, total * entry / leverage
        if leverage > max(_initial_leverage(row) for row in context["brackets"]):
            raise RiskError(f"{leverage}x exceeds this symbol's maximum leverage")
        if total * max(entry, mark) >= notional_cap(context["brackets"], leverage):
            raise RiskError(f"Position exceeds the account's {leverage}x leverage risk bracket")
        fee_rate = context["fee_rate"]
        opening_fee = notional_amount * fee_rate
        if total * max(entry, mark) / leverage + opening_fee > context["available"]:
            raise RiskError("USDT available balance is insufficient for both legs and estimated entry fees")
        liquidation = liquidation_price(side, total, entry, margin - opening_fee, context["brackets"])
        if (side == "long" and liquidation >= stop) or (side == "short" and liquidation <= stop):
            raise RiskError("Estimated liquidation price is not beyond the stop")
        return {
            "total_quantity": str(total),
            "leg_quantities": [str(leg), str(leg)],
            "entry_type": terms["entry_type"],
            "entry_price": str(entry),
            "current_price": str(current),
            "mark_price": str(mark),
            "margin": str(margin),
            "requested_margin": str(requested_margin),
            "notional": str(notional_amount),
            "max_loss": str(total * abs(entry - stop) + opening_fee + total * stop * fee_rate),
            "liquidation_price": str(liquidation),
            "liquidation_price_kind": "estimate",
            "expected_profit_tp1": str(leg * abs(tp1 - entry) - leg * (entry + tp1) * fee_rate),
            "expected_profit_tp2": str(leg * abs(tp2 - entry) - leg * (entry + tp2) * fee_rate),
            "estimated_entry_fees": str(opening_fee),
            "taker_fee_rate": str(fee_rate),
            "min_base_amount_increment": str(step),
            "min_order_size": str(terms["minimum"]),
            "min_notional_size": str(terms["min_notional"]),
            "position_mode": context["position_mode"],
            "margin_mode": "ISOLATED",
            "leverage": leverage,
            "is_auto_add_margin": False,
            "as_of": context["as_of"],
            "configuration_required": context["config"]["marginType"] != "ISOLATED"
            or int(context["config"]["leverage"]) != leverage,
        }

    def _limits(self, request, context):
        terms = self._terms(request, context)
        side, stop, entry, mark, step = terms["side"], terms["stop"], terms["entry"], terms["mark"], terms["step"]
        brackets, fee_rate, available = context["brackets"], context["fee_rate"], context["available"]
        # The smallest valid leg: what every leverage's minimum margin is built from.
        leg = max(terms["minimum"], quantize(terms["min_notional"] / min(entry, mark), step, ROUND_CEILING))
        total = leg * 2

        def stop_safe(leverage):
            margin = total * entry / leverage
            try:
                liquidation = liquidation_price(side, total, entry, margin - total * entry * fee_rate, brackets)
            except RiskError:
                return False
            return liquidation < stop if side == "long" else liquidation > stop

        exchange_max = max(_initial_leverage(row) for row in brackets)
        safe_max = next((lev for lev in range(exchange_max, 0, -1) if stop_safe(lev)), 0)
        leverage = leverage_of(request)
        high = max(entry, mark)
        # Margin buys leverage × margin / entry of quantity, rounded down to the step.
        ceilings = [
            available / (high / entry + leverage * fee_rate),
            notional_cap(brackets, leverage) * entry / (leverage * high),
            terms["maximum"] * 2 * entry / leverage,
        ]
        cent = Decimal("0.01")
        return {
            "leverage": leverage,
            "max_leverage": min(exchange_max, safe_max),
            "exchange_max_leverage": exchange_max,
            "stop_safe_max_leverage": safe_max,
            "min_margin": str(quantize(total * entry / leverage, cent, ROUND_CEILING)),
            "max_margin": str(max(quantize(min(ceilings), cent), ZERO)),
            "available_balance": str(available),
            "entry_type": terms["entry_type"],
            "entry_price": str(entry),
            "current_price": str(terms["current"]),
            "mark_price": str(mark),
            "blocked": terms["blocked"],
            "as_of": context["as_of"],
        }

    async def limits(self, request):
        """Leverage range and margin bounds for a plan; reads only, never refuses a price."""
        return self._limits(request, await self._context(request))

    async def quote(self, request):
        return self._quote(request, await self._context(request))

    async def prepare_live(self, request):
        if value(request, "mode") != "live":
            raise RiskError("A mock request cannot configure an exchange account", 400)
        context = await self._context(request)
        self._quote(request, context)  # Reject invalid sizing before any configuration write.
        connector, symbol, config = context["connector"], context["symbol"], context["config"]
        changes = []
        if config["marginType"] != "ISOLATED":
            changes.append(("v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"}))
        leverage = leverage_of(request)
        if int(config["leverage"]) != leverage:
            changes.append(("v1/leverage", {"symbol": symbol, "leverage": leverage}))
        for path, data in changes:
            positions, orders, algo_orders = await asyncio.gather(
                read(connector, "v3/positionRisk", {"symbol": symbol}),
                read(connector, "v1/openOrders", {"symbol": symbol}),
                read(connector, "v1/openAlgoOrders", {"symbol": symbol}),
            )
            self._assert_empty(symbol, positions, orders, algo_orders)
            try:
                async with asyncio.timeout(EXCHANGE_REQUEST_TIMEOUT):
                    await connector._api_post(path_url=path, data=data, is_auth_required=True, limit_id="v1/leverage")
            except asyncio.CancelledError:
                raise
            except Exception:
                raise RiskError(f"Exchange configuration could not be confirmed ({path}); no orders placed", 502) from None
        verified = await self._context(request)
        if verified["config"]["marginType"] != "ISOLATED" or int(verified["config"]["leverage"]) != leverage:
            raise RiskError(f"Exchange did not confirm isolated margin and {leverage}x leverage", 502)
        return self._quote(request, verified)
