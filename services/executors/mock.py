"""Live-price mock fills. This adapter has no access to broker mutation methods."""

import time
from decimal import Decimal


class MockBroker:
    def __init__(self, price_source):
        self.price_source = price_source

    async def price(self, request):
        return await self.price_source(request)

    async def submit(self, request, order):
        return await self.reconcile(request, {**order, "status": "OPEN"})

    async def reconcile(self, request, order):
        if order["status"] in ("FILLED", "CANCELED", "FAILED"):
            return order
        price = Decimal(str(await self.price(request)))
        limit = Decimal(str(order.get("price") or price))
        fills = order["order_type"] == "MARKET" or (price <= limit if order["side"] == "BUY" else price >= limit)
        if not fills:
            return {"status": "OPEN", "filled_base": "0", "filled_quote": "0", "fees_quote": "0"}
        amount = Decimal(order["amount"])
        quote = amount * price
        fee = quote * Decimal("0.0005")
        return {
            "status": "FILLED",
            "filled_base": str(amount),
            "filled_quote": str(quote),
            "fees_quote": str(fee),
            "fees_complete": True,
            "fills_complete": True,
            "fills": [
                {
                    "trade_id": "mock-" + order["client_order_id"],
                    "base_amount": str(amount),
                    "quote_amount": str(quote),
                    "price": str(price),
                    "commission_asset": "USDT",
                    "commission": str(fee),
                    "time": int(time.time() * 1000),
                }
            ],
            "exchange_order_id": "mock-" + order["client_order_id"],
        }

    async def cancel(self, request, order):
        # A market crossing during cancellation is deliberately treated as a fill.
        current = await self.reconcile(request, order)
        if current["status"] != "FILLED":
            current["status"] = "CANCELED"
        return current
