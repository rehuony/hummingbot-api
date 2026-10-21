"""Two-leg managed positions with durable commands and conservative reconciliation."""

import asyncio
import copy
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from models.position_groups import PositionGroupRequest
from services.executors.managed_position import (
    TERMINAL_ORDERS,
    ManagedPositionConfig,
    ManagedPositionExecutor,
    decimal,
    leg_accounting,
)
from services.executors.store import RevisionConflict, needs_attention

logger = logging.getLogger(__name__)
TERMINAL_GROUPS = {"CLOSED", "DETACHED", "FAILED"}
UNKNOWN_ORDERS = {"UNKNOWN", "NOT_FOUND", "SUBMITTING"}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def request_data(request):
    data = request.model_dump(mode="json")
    # Decimal spellings 10/10.0 must identify the same request.
    for name in ("entry_low", "entry_high", "stop_price", "tp1_price", "tp2_price", "margin"):
        data[name] = format(decimal(data[name]).normalize(), "f")
    return data


class CheckpointError(RuntimeError):
    """A failed checkpoint must discard the mutable in-memory working copy."""


class PositionGroupService:
    def __init__(self, store, risk, broker, executor_service=None, *, clock=time.time, interval=1.0):
        self.store = store
        self.risk = risk
        self.broker = broker
        self.executor_service = executor_service
        self.clock = clock
        self.interval = interval
        self._locks = {}
        self._groups = {}
        self._task = None
        self._workers = {}
        self._fee_tasks = {}
        self._recover_pending = set()

    async def _save(self, group, *, create=False):
        try:
            await self.store.save(group, create=create)
        except (RevisionConflict, IntegrityError):
            raise
        except Exception as error:
            raise CheckpointError("Managed checkpoint unavailable") from error

    @staticmethod
    def capabilities():
        return {
            "version": 1,
            "protocol_version": 1,
            "executor_type": "managed_position_executor",
            "connectors": ["binance_perpetual"],
            "actions": ["cancel_entry", "reduce_half", "close", "detach"],
        }

    def _lock(self, group_id):
        return self._locks.setdefault(group_id, asyncio.Lock())

    async def preview(self, request):
        if request.entry_expires_at <= self.clock():
            raise HTTPException(422, "The entry opportunity has expired")
        quote = await self.risk.quote(request)
        return self._quote_aliases(quote)

    @staticmethod
    def _quote_aliases(quote):
        quote = dict(quote)
        quote["quantity"] = str(quote["total_quantity"])
        quote["entry_method"] = quote["entry_type"]
        quote["profit_tp1"] = str(quote.get("expected_profit_tp1", "0"))
        quote["profit_tp2"] = str(quote.get("expected_profit_tp2", "0"))
        return quote

    async def create(self, group_id, request):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", group_id):
            raise HTTPException(422, "Invalid group id")
        data = request_data(request)
        digest = fingerprint(data)
        async with self._lock(group_id):
            existing = await self.store.load(group_id)
            if existing is not None:
                if existing["request_hash"] != digest:
                    raise HTTPException(409, "GROUP_CONFLICT: The group id belongs to a different request")
                return self.view(existing)
            if request.entry_expires_at <= self.clock():
                raise HTTPException(422, "The entry opportunity has expired")
            try:
                quote = self._quote_aliases(await self.risk.quote(request))
            except HTTPException as error:
                if error.status_code < 500:
                    raise HTTPException(422, "RISK_REJECTED: " + str(error.detail)) from None
                raise
            now = self.clock()
            group = {
                "id": group_id,
                "request": data,
                "request_hash": digest,
                "status": "PLACING",
                "revision": 0,
                "created_at": now,
                "quote": quote,
                "current_price": str(quote["current_price"]),
                "entry_closed": False,
                "prepared": False,
                "recovering": False,
                "error": None,
                "close_reason": None,
                "operation_id": None,
                "operations": {},
                "legs": [],
                "history_exported": False,
                "settlement_pending": False,
            }
            for index, amount in enumerate(quote["leg_quantities"], 1):
                leg_id = hashlib.sha256(f"managed:{group_id}:{index}".encode()).hexdigest()[:32]
                group["legs"].append(
                    {
                        "id": leg_id,
                        "number": index,
                        "amount": str(amount),
                        "entry_closed": False,
                        "orders": [],
                        "close_reason": None,
                        "trailing_stop": None,
                        "tp_price": str(request.tp1_price if index == 1 else request.tp2_price),
                    }
                )
            try:
                await self._save(group, create=True)
            except (IntegrityError, RevisionConflict):
                existing = await self.store.load(group_id)
                if existing and existing["request_hash"] == digest:
                    return self.view(existing)
                code = "GROUP_CONFLICT" if existing else "SYMBOL_BUSY"
                raise HTTPException(409, code + ": The group id or live account symbol is already reserved") from None
            self._groups[group_id] = group
            # Creation is now durable. Never return a plain HTTP failure inviting a fresh
            # create if leverage setup or an order submission has an uncertain outcome.
            await self._tick_safely(group)
            return await self.get(group_id)

    async def get(self, group_id):
        group = await self.store.load(group_id)
        if group is None:
            raise HTTPException(404, "Position group not found")
        return self.view(group)

    async def command(self, group_id, command):
        async with self._lock(group_id):
            group = await self.store.load(group_id)
            if group is None:
                raise HTTPException(404, "Position group not found")
            digest = fingerprint({"action": command.action})
            previous = group["operations"].get(command.operation_id)
            if previous:
                if previous["request_hash"] != digest:
                    raise HTTPException(409, "Operation id belongs to a different command")
                return self.view(group)
            if group["status"] in TERMINAL_GROUPS:
                raise HTTPException(409, "The group is no longer managed")
            active = group["operations"].get(group.get("operation_id"))
            if active and active["status"] not in ("COMPLETED", "FAILED"):
                if command.action == "close" and active["action"] in ("cancel_entry", "reduce_half"):
                    active["status"] = "FAILED"
                    active["error"] = "Superseded by a full-close command"
                else:
                    raise HTTPException(409, "Another group command is still being reconciled")
            if command.action == "reduce_half" and not any(self._values(group, leg)["remaining"] for leg in group["legs"]):
                raise HTTPException(409, "There is no filled position to reduce")
            operation = {
                "id": command.operation_id,
                "action": command.action,
                "request_hash": digest,
                "status": "PENDING",
                "created_at": self.clock(),
                "targets": None,
                "error": None,
            }
            group["operations"][command.operation_id] = operation
            group["operation_id"] = command.operation_id
            group["entry_closed"] = True
            await self._save(group)
            self._groups[group_id] = group
            await self._tick_safely(group)
            return await self.get(group_id)

    def _values(self, group, leg):
        return leg_accounting(leg, group["request"]["side"], group["current_price"])

    @staticmethod
    def _unfinished(leg, action=None):
        return [
            order
            for order in leg["orders"]
            if order["status"] not in TERMINAL_ORDERS and (action is None or order["position_action"] == action)
        ]

    async def _new_order(self, group, leg, amount, action, reason):
        sequence = len(leg["orders"])
        client_id = "tc" + hashlib.sha256(f"{leg['id']}:{sequence}".encode()).hexdigest()[:28]
        buy = (group["request"]["side"] == "long") == (action == "OPEN")
        order = {
            "client_order_id": client_id,
            "side": "BUY" if buy else "SELL",
            "amount": str(amount),
            "position_action": action,
            "order_type": group["quote"]["entry_type"] if action == "OPEN" else "MARKET",
            "price": group["quote"]["entry_price"] if action == "OPEN" else None,
            "status": "PREPARED",
            "submission_attempted": False,
            "filled_base": "0",
            "filled_quote": "0",
            "fees_quote": "0",
            "fills": [],
            "fills_complete": False,
            "reason": reason,
            "operation_id": group.get("operation_id") if action == "CLOSE" else None,
        }
        leg["orders"].append(order)
        await self._save(group)
        await self._submit(group, order)

    async def _submit(self, group, order):
        # If interrupted after this commit, recover by the durable client id. Never
        # infer "not sent" from a timeout or one NOT_FOUND response.
        order["status"] = "SUBMITTING"
        order["submission_attempted"] = True
        await self._save(group)
        try:
            outcome = await self.broker.submit(PositionGroupRequest(**group["request"]), dict(order))
            if outcome.get("status") == "NOT_SUBMITTED" and outcome.get("attempted") is False:
                # An explicit adapter acknowledgement before its POST boundary is
                # safe to retry. A timeout or a process crash never grants this.
                order["status"] = "PREPARED"
                order["submission_attempted"] = False
            else:
                self._merge_order(order, outcome)
        except Exception:
            logger.exception("Managed order submission requires reconciliation: %s", order["client_order_id"])
            order["status"] = "UNKNOWN"
        await self._save(group)

    @staticmethod
    def _merge_order(order, outcome):
        state = outcome.get("status", "UNKNOWN")
        if state not in TERMINAL_ORDERS | UNKNOWN_ORDERS | {"OPEN", "PARTIALLY_FILLED"}:
            raise ValueError("Unrecognized broker order state")
        previous_fill = decimal(order.get("filled_base", "0"))
        for key in ("filled_base", "filled_quote", "fees_quote"):
            value = decimal(outcome.get(key, order.get(key, "0")))
            if not value.is_finite() or (key != "fees_quote" and value < 0):
                raise ValueError("Invalid cumulative fill data")
            if key != "fees_quote" and value < decimal(order.get(key, "0")):
                raise ValueError("Cumulative order fills regressed")
            order[key] = str(value)
        if decimal(order["filled_base"]) > decimal(order["amount"]):
            raise ValueError("Order fills exceed requested amount")
        if decimal(order["filled_base"]) > previous_fill:
            order["fee_attempts"] = 0
            order["fee_next_at"] = 0
        order["status"] = state
        if outcome.get("exchange_order_id"):
            order["exchange_order_id"] = str(outcome["exchange_order_id"])
        order["fees_complete"] = outcome.get("fees_complete", order.get("fees_complete", True))
        if outcome.get("fee_assets"):
            order["fee_assets"] = outcome["fee_assets"]
        # Exchange trade IDs identify immutable fills. Keep already observed rows
        # when a later response is truncated or temporarily cannot load trades.
        fills = {fill["trade_id"]: fill for fill in order.get("fills", [])}
        previous_fill_count = len(fills)
        for fill in outcome.get("fills", []):
            fills[str(fill["trade_id"])] = dict(fill)
        order["fills"] = list(fills.values())
        if len(fills) > previous_fill_count:
            order["fee_attempts"] = 0
            order["fee_next_at"] = 0
        order["fills_complete"] = outcome.get("fills_complete", order.get("fills_complete", False))

    async def _reconcile(self, group):
        broker = self.broker
        request = PositionGroupRequest(**group["request"])
        for leg in group["legs"]:
            for order in self._unfinished(leg):
                if order["status"] == "PREPARED":
                    if order["position_action"] == "OPEN" and (group["entry_closed"] or leg["entry_closed"]):
                        order["status"] = "CANCELED"
                    elif order["position_action"] == "OPEN":
                        # Entry submission follows this tick's exchange exposure and
                        # configuration checks, never a previous tick's permission.
                        continue
                    else:
                        await self._submit(group, order)
                    continue
                try:
                    self._merge_order(order, await broker.reconcile(request, dict(order)))
                except Exception:
                    logger.exception("Managed order reconciliation failed: %s", order["client_order_id"])
                    order["status"] = "UNKNOWN"
        await self._save(group)

    async def _cancel_entries(self, group, leg):
        leg["entry_closed"] = True
        await self._save(group)
        request = PositionGroupRequest(**group["request"])
        for order in self._unfinished(leg, "OPEN"):
            if order["status"] == "PREPARED":
                order["status"] = "CANCELED"
                continue
            order["cancel_requested"] = True
            await self._save(group)
            try:
                self._merge_order(order, await self.broker.cancel(request, dict(order)))
            except Exception:
                order["status"] = "UNKNOWN"
                logger.exception("Entry cancellation requires reconciliation: %s", order["client_order_id"])
            await self._save(group)

    async def _close_leg(self, group, leg, reason):
        leg["close_reason"] = reason
        await self._cancel_entries(group, leg)
        # An uncertain entry must never disable protection of its already confirmed
        # fills. A pending close, however, may already have reduced that quantity.
        if self._unfinished(leg, "CLOSE"):
            return
        remaining = self._values(group, leg)["remaining"]
        if remaining:
            rejected = [
                order
                for order in leg["orders"]
                if order["position_action"] == "CLOSE" and order["reason"] == reason and order["status"] == "FAILED"
            ]
            if len(rejected) >= 3:
                group["error"] = "The exchange repeatedly rejected a protective close; manual reconciliation is required"
                return
            await self._new_order(group, leg, remaining, "CLOSE", reason)

    def _barrier(self, group, leg):
        values = self._values(group, leg)
        if not values["remaining"]:
            return None
        request = group["request"]
        mark = decimal(group["current_price"])
        stop = decimal(request["stop_price"])
        target = decimal(leg["tp_price"])
        long = request["side"] == "long"
        if mark <= stop if long else mark >= stop:
            return "STOP_LOSS"
        if leg["number"] == 2:
            tp1 = decimal(request["tp1_price"])
            risk = abs(values["average"] - stop)
            if group.get("tp1_reached") or (mark >= tp1 if long else mark <= tp1) or leg["trailing_stop"] is not None:
                # TP1 arms at gross entry breakeven; subsequent favourable movement
                # trails by the original absolute risk distance. Fees stay in net PnL.
                previous = decimal(leg["trailing_stop"]) if leg["trailing_stop"] is not None else values["average"]
                candidate = max(values["average"], mark - risk) if long else min(values["average"], mark + risk)
                trigger = max(previous, candidate) if long else min(previous, candidate)
                leg["trailing_stop"] = str(trigger)
                if mark <= trigger if long else mark >= trigger:
                    return "TRAILING_STOP"
        if mark >= target if long else mark <= target:
            return "TAKE_PROFIT"
        return None

    async def _apply_command(self, group):
        operation = group["operations"].get(group.get("operation_id"))
        if not operation or operation["status"] in ("COMPLETED", "FAILED"):
            return
        for leg in group["legs"]:
            await self._cancel_entries(group, leg)
        action = operation["action"]
        if action != "close" and any(self._unfinished(leg, "OPEN") for leg in group["legs"]):
            return
        if action == "cancel_entry":
            operation["status"] = "COMPLETED"
            return
        if action == "detach":
            # A pending close cannot be detached until its outcome is known.
            if any(self._unfinished(leg) for leg in group["legs"]):
                return
            group["status"] = "DETACHED"
            group["close_reason"] = "DETACHED"
            operation["status"] = "COMPLETED"
            return
        if action == "reduce_half":
            # Cancel/fill races can change exchange exposure after the tick's first
            # read. Confirm it again only once all entry cancellations are settled.
            await self._verify_position(group)
            if not group.get("exposure_verified") or group.get("recovering"):
                operation["status"] = "FAILED"
                operation["error"] = "Exchange exposure must match the order ledger before a half reduction"
                return
        if operation["targets"] is None or action == "close":
            values = [self._values(group, leg) for leg in group["legs"]]
            remaining = [value["remaining"] for value in values]
            if action == "close":
                reductions = remaining
            else:
                step = decimal(group["quote"]["min_base_amount_increment"])
                minimum = decimal(group["quote"]["min_order_size"])
                minimum_notional = decimal(group["quote"]["min_notional_size"])
                if not group.get("price_available", True):
                    operation["status"] = "FAILED"
                    operation["error"] = "A fresh price is required to validate a half reduction; full close remains available"
                    return
                mark = decimal(group["current_price"])
                total = sum(remaining)
                half = (total / 2 / step).to_integral_value(rounding=ROUND_DOWN) * step
                first = (
                    (remaining[0] / total * half / step).to_integral_value(rounding=ROUND_DOWN) * step if total else Decimal(0)
                )
                reductions = [first, half - first]
                if half <= 0 or any(
                    q
                    and (
                        q < minimum
                        or q * mark < minimum_notional
                        or (left - q and (left - q < minimum or (left - q) * mark < minimum_notional))
                    )
                    for q, left in zip(reductions, remaining)
                ):
                    operation["status"] = "FAILED"
                    operation["error"] = "A half reduction or its residual is below the exchange minimum"
                    return
            operation["targets"] = {
                leg["id"]: str(value["closed"] + amount) for leg, value, amount in zip(group["legs"], values, reductions)
            }
            await self._save(group)
        complete = True
        for leg in group["legs"]:
            values = self._values(group, leg)
            amount = min(values["remaining"], max(Decimal(0), decimal(operation["targets"][leg["id"]]) - values["closed"]))
            if amount:
                complete = False
                if not self._unfinished(leg, "CLOSE"):
                    previous = [o for o in leg["orders"] if o.get("operation_id") == operation["id"] and o["status"] == "FAILED"]
                    if previous:
                        operation["status"] = "FAILED"
                        operation["error"] = "The exchange rejected a reduction; the remaining position stays managed"
                        return
                    await self._new_order(group, leg, amount, "CLOSE", "MANUAL_CLOSE" if action == "close" else "REDUCE_HALF")
        if (
            complete
            or all(
                self._values(group, leg)["closed"] >= decimal(operation["targets"][leg["id"]])
                or self._values(group, leg)["remaining"] == 0
                for leg in group["legs"]
            )
        ) and not any(self._unfinished(leg) for leg in group["legs"]):
            operation["status"] = "COMPLETED"

    async def _tick_safely(self, group):
        try:
            if group["status"] not in TERMINAL_GROUPS:
                group["error"] = None
                await self._tick(group)
        except CheckpointError:
            raise
        except RevisionConflict:
            # Another owner won the database fence. Do not keep executing this copy.
            self._groups.pop(group["id"], None)
            logger.error("Managed group lost its revision fence: %s", group["id"])
            return
        except HTTPException as error:
            if error.status_code < 500 and not any(leg["orders"] for leg in group["legs"]):
                group["status"] = "FAILED"
                group["close_reason"] = "RISK_REJECTED"
                group["error"] = str(error.detail)
            else:
                group["status"] = "RECONCILING"
                group["error"] = str(error.detail)
        except Exception:
            logger.exception("Managed position group requires reconciliation: %s", group["id"])
            group["status"] = "RECONCILING"
            group["error"] = "Execution state is unavailable; retrying reconciliation without resubmitting unknown orders"
        if group["status"] in TERMINAL_GROUPS:
            group.setdefault("closed_at", self.clock())
        self._set_settlement_pending(group)
        await self._save(group)
        self._register_views(group)
        await self._export_history(group)
        self._schedule_fees(group)

    def _set_settlement_pending(self, group):
        group["settlement_pending"] = any(
            decimal(order["filled_base"]) > 0 and not order.get("fees_complete", True) and order.get("fee_attempts", 0) < 12
            for leg in group["legs"]
            for order in leg["orders"]
        )

    def _schedule_fees(self, group):
        if self._task is None or self._task.done() or not group.get("settlement_pending"):
            return
        task = self._fee_tasks.get(group["id"])
        if task is None or task.done():
            self._fee_tasks[group["id"]] = asyncio.create_task(self._settle_fees(group["id"]), name=f"managed-fees-{group['id']}")

    async def _settle_fees(self, group_id):
        """Read slow fee data outside the position-control lock, then merge a fresh checkpoint."""
        try:
            async with self._lock(group_id):
                group = await self.store.load(group_id)
                pending = [
                    copy.deepcopy(order)
                    for leg in group["legs"]
                    for order in leg["orders"]
                    if decimal(order["filled_base"]) > 0
                    and not order.get("fees_complete", True)
                    and order.get("fee_attempts", 0) < 12
                    and self.clock() >= order.get("fee_next_at", 0)
                ]
            for snapshot in pending:
                async with self._lock(group_id):
                    group = await self.store.load(group_id)
                    current = next(
                        o for leg in group["legs"] for o in leg["orders"] if o["client_order_id"] == snapshot["client_order_id"]
                    )
                    if current.get("fees_complete", True):
                        continue
                    attempts = current.get("fee_attempts", 0)
                    current["fee_attempts"] = attempts + 1
                    current["fee_next_at"] = self.clock() + min(300, 5 * 2**attempts)
                    self._set_settlement_pending(group)
                    await self._save(group)
                    snapshot = copy.deepcopy(current)
                    request = PositionGroupRequest(**group["request"])
                    broker = self.broker
                try:
                    outcome = await broker.settle_fees(request, snapshot)
                except Exception:
                    logger.warning("Managed fee settlement remains unavailable for %s", snapshot["client_order_id"])
                    continue
                async with self._lock(group_id):
                    group = await self.store.load(group_id)
                    current = next(
                        o for leg in group["legs"] for o in leg["orders"] if o["client_order_id"] == snapshot["client_order_id"]
                    )
                    if any(
                        decimal(current[key]) != decimal(snapshot[key]) or decimal(outcome.get(key, -1)) != decimal(current[key])
                        for key in ("filled_base", "filled_quote")
                    ):
                        continue
                    before = (current.get("fees_quote"), current.get("fees_complete"))
                    self._merge_order(current, {**outcome, "status": current["status"]})
                    if before != (current.get("fees_quote"), current.get("fees_complete")):
                        group["history_exported"] = False
                    self._set_settlement_pending(group)
                    await self._save(group)
        except Exception:
            # The control worker retains ownership and retries settlement from the
            # durable ledger. Fee or database outages never terminate protection.
            logger.exception("Managed fee checkpoint unavailable for %s", group_id)

    async def _export_history(self, group):
        # Managed completion is owned here, rather than the generic loop, so a
        # crash between ledger completion and history export is recoverable.
        if group["status"] not in TERMINAL_GROUPS or group.get("history_exported"):
            return
        if self.executor_service is None:
            group["history_exported"] = True
        else:
            exported = True
            for leg in group["legs"]:
                view = self.executor_service._active_executors[leg["id"]]
                exported &= bool(await self.executor_service._persist_executor_completed(leg["id"], view))
            if exported:
                group["history_exported"] = True
                for leg in group["legs"]:
                    self.executor_service._active_executors.pop(leg["id"], None)
                    self.executor_service._executor_metadata.pop(leg["id"], None)
        await self._save(group)

    async def _verify_position(self, group):
        group["exposure_verified"] = False
        group["risk_snapshot_available"] = False
        try:
            snapshot = await self.broker.position_snapshot(PositionGroupRequest(**group["request"]))
            actual = decimal(snapshot["amount"])
            if not actual.is_finite() or actual < 0:
                raise ValueError("Invalid exchange position quantity")
            group["position_snapshot"] = snapshot
            group["risk_snapshot_available"] = True
            expected = sum(self._values(group, leg)["remaining"] for leg in group["legs"])
            group["exposure_verified"] = actual == expected
            if actual != expected:
                group["error"] = "Exchange exposure differs from the durable order ledger; operator reconciliation is required"
        except Exception:
            group["error"] = "Exchange exposure cannot be verified; known fills remain protected and new entries are suspended"

    async def _tick(self, group):
        if group["status"] in TERMINAL_GROUPS:
            return
        request = PositionGroupRequest(**group["request"])
        if self.clock() >= request.entry_expires_at:
            group["entry_closed"] = True
        try:
            mark = decimal(await self.broker.price(request))
            if not mark.is_finite() or mark <= 0:
                raise ValueError("A fresh positive price is required")
            group["current_price"] = str(mark)
            group["price_available"] = True
            stop, tp1 = decimal(request.stop_price), decimal(request.tp1_price)
            if mark >= tp1 if request.side == "long" else mark <= tp1:
                group["tp1_reached"] = True
            invalidated = not (stop < mark < tp1 if request.side == "long" else tp1 < mark < stop)
            market_left_zone = group["quote"]["entry_type"] == "MARKET" and not request.entry_low <= mark <= request.entry_high
            if invalidated or market_left_zone:
                # A resumed/second entry must not use a market quote whose entry
                # opportunity already disappeared. Existing fills remain protected.
                group["entry_closed"] = True
            if mark <= stop if request.side == "long" else mark >= stop:
                # An accepted entry can be temporarily invisible after a timeout.
                # Remember a stop breach even when its last known fill is zero;
                # later reconciliation must not revive it after a price rebound.
                for leg in group["legs"]:
                    if any(
                        order["position_action"] == "OPEN"
                        and order["status"] not in ("PREPARED", "FAILED")
                        and not (order["status"] == "CANCELED" and not order.get("submission_attempted", True))
                        and (order["status"] not in TERMINAL_ORDERS or decimal(order["filled_base"]) > 0)
                        for order in leg["orders"]
                    ):
                        leg["close_reason"] = leg.get("close_reason") or "STOP_LOSS"
        except Exception:
            # Cancellation and a reduce-only full close do not require market data.
            # Keep the last mark for display, explicitly stale, never for new risk.
            group["price_available"] = False
            group["error"] = "Market price is unavailable; cancellation and full close remain available"
        if group["legs"][0].get("close_reason") == "TAKE_PROFIT":
            group["tp1_reached"] = True
        await self._reconcile(group)
        unknown = any(order["status"] in UNKNOWN_ORDERS for leg in group["legs"] for order in self._unfinished(leg))
        await self._verify_position(group)
        if group.get("recovering"):
            if group.get("recovery_risk_check"):
                try:
                    await self.risk.verify_live(request, group["quote"].get("position_mode"))
                    group["recovery_risk_check"] = False
                except Exception:
                    group["error"] = "Exchange risk configuration cannot be verified; existing fills remain protected"
            if group.get("exposure_verified") and not unknown and not group.get("recovery_risk_check"):
                group["recovering"] = False
                await self._save(group)
        if (
            not group["prepared"]
            and not group["entry_closed"]
            and group["price_available"]
            and not unknown
            and not group.get("recovering")
            and group.get("exposure_verified")
        ):
            # A live preflight is repeated after an interruption; it must itself be
            # idempotent and refuse changes that would affect an existing position.
            prepared_quote = self._quote_aliases(await self.risk.prepare_live(request))
            if (
                prepared_quote["leg_quantities"] != group["quote"]["leg_quantities"]
                or prepared_quote["entry_type"] != group["quote"]["entry_type"]
            ):
                group["status"] = "FAILED"
                group["close_reason"] = "REQUOTE_REQUIRED"
                group["error"] = "The executable quote changed before submission; review a fresh card"
                return
            group["prepared"] = True
            group["quote"] = prepared_quote
            await self._save(group)
        if self.clock() >= request.entry_expires_at:
            group["entry_closed"] = True
        for leg in group["legs"]:
            if group["entry_closed"] or leg["entry_closed"]:
                await self._cancel_entries(group, leg)
            elif (
                group["prepared"]
                and group["price_available"]
                and not unknown
                and not group.get("recovering")
                and group.get("exposure_verified")
            ):
                prepared = next(
                    (order for order in leg["orders"] if order["status"] == "PREPARED" and order["position_action"] == "OPEN"),
                    None,
                )
                if prepared is not None:
                    await self._submit(group, prepared)
                elif not leg["orders"]:
                    await self._new_order(group, leg, leg["amount"], "OPEN", "ENTRY")
                else:
                    continue
                if leg["orders"][-1]["status"] in UNKNOWN_ORDERS:
                    unknown = True
                if leg["orders"][-1]["status"] == "FAILED":
                    group["entry_closed"] = True
        # Barriers use actual fills immediately, including a partially filled entry.
        for leg in group["legs"]:
            reason = leg.get("close_reason") or (self._barrier(group, leg) if group["price_available"] else None)
            if reason:
                await self._close_leg(group, leg, reason)
        await self._apply_command(group)
        await self._verify_position(group)
        if group["status"] == "DETACHED":
            group["closed_at"] = self.clock()
            return
        values = [self._values(group, leg) for leg in group["legs"]]
        unfinished = [order for leg in group["legs"] for order in self._unfinished(leg)]
        if (
            group.get("exposure_verified")
            and not unfinished
            and not any(value["remaining"] for value in values)
            and not any(not leg["orders"] and not group["entry_closed"] for leg in group["legs"])
        ):
            # Configuration checks suspend new risk, not a proven completed exit.
            # Keep the recorded configuration error while releasing the flat group.
            group["recovering"] = False
        if (
            any(order["status"] in UNKNOWN_ORDERS for order in unfinished)
            or group.get("recovering")
            or not group.get("exposure_verified")
        ):
            group["status"] = "RECONCILING"
        elif any(order["position_action"] == "CLOSE" for order in unfinished):
            group["status"] = "CLOSING"
        elif any(value["remaining"] for value in values):
            group["status"] = "OPEN" if group["price_available"] else "RECONCILING"
        elif unfinished or any(not leg["orders"] and not group["entry_closed"] for leg in group["legs"]):
            group["status"] = "PENDING"
        else:
            group["status"] = "CLOSED"
            group["closed_at"] = self.clock()
            reasons = [leg.get("close_reason") for leg in group["legs"] if leg.get("close_reason")]
            group["close_reason"] = (
                reasons[-1] if reasons else ("MANUAL_CLOSE" if any(v["opened"] for v in values) else "CANCELED")
            )

    def view(self, group):
        values = [self._values(group, leg) for leg in group["legs"]]
        opened = sum(value["opened"] for value in values)
        entry_quote = sum(value["entry_quote"] for value in values)
        quote = group["quote"]
        remaining = sum(v["remaining"] for v in values)
        liquidation = quote.get("liquidation_price")
        liquidation_kind = quote.get("liquidation_price_kind", "estimate")
        if opened:
            snapshot = group.get("position_snapshot", {}) if group.get("risk_snapshot_available") and remaining else {}
            liquidation = snapshot.get("liquidation_price")
            liquidation_kind = "exchange" if liquidation else "unavailable"
        return {
            **quote,
            "id": group["id"],
            "execution_id": group["id"],
            "status": group["status"],
            "current_price": group["current_price"],
            "price_available": group.get("price_available", True),
            "entry_price": str(entry_quote / opened) if opened else quote["entry_price"],
            "filled_amount": str(opened),
            "remaining_amount": str(remaining),
            "pnl": str(sum(v["pnl"] for v in values)),
            "fees": str(sum(v["fees"] for v in values)),
            "fills_complete": all(
                order.get("fills_complete", False) or decimal(order["filled_base"]) == 0
                for leg in group["legs"]
                for order in leg["orders"]
            ),
            "exchange_remaining_amount": (
                group.get("position_snapshot", {}).get("amount") if group.get("risk_snapshot_available") else None
            ),
            "exposure_verified": group.get("exposure_verified", False),
            "pnl_complete": group.get("exposure_verified", False)
            and (not remaining or group.get("price_available", True))
            and all(
                order.get("fees_complete", True) and order["status"] not in UNKNOWN_ORDERS
                for leg in group["legs"]
                for order in leg["orders"]
            ),
            "liquidation_price": liquidation,
            "liquidation_price_kind": liquidation_kind,
            "risk_snapshot_available": group.get("risk_snapshot_available", False),
            "settlement_pending": group.get("settlement_pending", False),
            "available_actions": self._available_actions(group),
            "close_reason": group.get("close_reason"),
            "error": group.get("error"),
            "entry_closed": group["entry_closed"],
            "entry_expires_at": group["request"]["entry_expires_at"],
            "operation": group["operations"].get(group.get("operation_id")),
            "legs": [
                {
                    "id": leg["id"],
                    "number": leg["number"],
                    "tp_price": leg["tp_price"],
                    "level_id": f"{group['id']}:{leg['number']}",
                    "remaining_amount": str(value["remaining"]),
                    "filled_amount": str(value["opened"]),
                    "pnl": str(value["pnl"]),
                    "close_reason": leg["close_reason"],
                    "trailing_stop": leg["trailing_stop"],
                    "orders": [dict(order) for order in leg["orders"]],
                }
                for leg, value in zip(group["legs"], values)
            ],
        }

    def _available_actions(self, group):
        if group["status"] in TERMINAL_GROUPS:
            return []
        operation = group["operations"].get(group.get("operation_id"))
        if operation and operation["status"] == "PENDING":
            return ["close"] if operation["action"] in ("cancel_entry", "reduce_half") else []
        actions = ["close"]
        if not group["entry_closed"] and any(self._unfinished(leg, "OPEN") or not leg["orders"] for leg in group["legs"]):
            actions.insert(0, "cancel_entry")
        unknown = any(order["status"] in UNKNOWN_ORDERS for leg in group["legs"] for order in self._unfinished(leg))
        if not unknown:
            actions.append("detach")
            if (
                group.get("price_available", True)
                and not group.get("recovering")
                and group.get("exposure_verified")
                and any(self._values(group, leg)["remaining"] for leg in group["legs"])
            ):
                actions.append("reduce_half")
        return actions

    def _register_views(self, group):
        service = self.executor_service
        if (
            service is None
            or (group["status"] in TERMINAL_GROUPS and group.get("history_exported"))
        ):
            return
        for leg in group["legs"]:
            request = group["request"]
            config = ManagedPositionConfig(
                id=leg["id"],
                timestamp=group["created_at"],
                controller_id="trade_cards",
                connector_name=request["connector_name"],
                trading_pair=request["trading_pair"],
                side=1 if request["side"] == "long" else 2,
                amount=leg["amount"],
                level_id=f"{group['id']}:{leg['number']}",
                group_id=group["id"],
            )
            view = ManagedPositionExecutor(config=config, group=group, leg=leg)
            view.set_terminal_reason()
            service._active_executors[leg["id"]] = view
            service._executor_metadata[leg["id"]] = {
                "account_name": request["account_name"],
                "connector_name": request["connector_name"],
                "trading_pair": request["trading_pair"],
                "executor_type": "managed_position_executor",
                "controller_id": "trade_cards",
                "created_at": datetime.fromtimestamp(group["created_at"], timezone.utc),
                "config": config.model_dump(mode="json"),
            }

    async def _restore(self, group):
        if group["status"] in ("CLOSED", "FAILED") and "exposure_verified" not in group:
            # A historical fully reconciled ledger proves its own completion. The
            # symbol may now belong to a new group, so do not query that exposure.
            group["exposure_verified"] = all(
                not self._unfinished(leg) and self._values(group, leg)["remaining"] == 0 for leg in group["legs"]
            )
        if group["status"] not in TERMINAL_GROUPS:
            submitted = any(
                order.get("submission_attempted", order["status"] != "PREPARED")
                for leg in group["legs"]
                for order in leg["orders"]
            )
            group["status"] = "RECONCILING"
            group["recovering"] = True
            group["recovery_risk_check"] = submitted
            group["exposure_verified"] = False
            if not submitted:
                # Configuration permission expires across process lifetimes. With
                # no attempted order it is safe to run the idle-symbol preflight.
                group["prepared"] = False
            for leg in group["legs"]:
                for order in leg["orders"]:
                    if (
                        order["status"] not in ("PREPARED", "FAILED")
                        and not (order["status"] == "CANCELED" and not order.get("submission_attempted", True))
                    ):
                        order["status"] = "UNKNOWN"
        await self._save(group)

    async def start(self):
        if self._task is not None and not self._task.done():
            return
        try:
            group_ids = await self.store.active_ids()
        except Exception:
            logger.exception("Managed group discovery unavailable; supervisor will retry")
            group_ids = []
        for group_id in group_ids:
            worker = self._workers.get(group_id)
            if worker is not None and not worker.done():
                continue
            self._recover_pending.add(group_id)
            try:
                async with self._lock(group_id):
                    group = await self.store.load(group_id)
                    await self._restore(group)
                    self._groups[group_id] = group
                    self._recover_pending.discard(group_id)
            except Exception:
                logger.exception("Managed group %s recovery checkpoint unavailable; worker will retry", group_id)
        self._task = asyncio.create_task(self._run(), name="managed-position-groups")

    async def _run(self):
        """Supervise independent groups; one stalled broker cannot suspend another."""
        while True:
            try:
                discovered = set(await self.store.active_ids())
                for group_id in discovered | set(self._groups) | self._recover_pending:
                    task = self._workers.get(group_id)
                    if task is None or task.done():
                        if group_id not in self._groups or task is not None:
                            # A canceled/failed worker is a recovery boundary even
                            # if its last in-memory copy remains in the registry.
                            self._recover_pending.add(group_id)
                        self._workers[group_id] = asyncio.create_task(
                            self._run_group(group_id), name=f"managed-position-{group_id}"
                        )
            except Exception:
                logger.exception("Managed group supervisor discovery failed; retrying")
            await asyncio.sleep(self.interval)

    async def _run_group(self, group_id):
        while True:
            try:
                async with self._lock(group_id):
                    # Never keep executing a copy whose last checkpoint failed.
                    group = await self.store.load(group_id)
                    if group is None or not needs_attention(group):
                        self._groups.pop(group_id, None)
                        self._workers.pop(group_id, None)
                        return
                    if group_id in self._recover_pending:
                        await self._restore(group)
                        self._recover_pending.discard(group_id)
                    self._groups[group_id] = group
                    await self._tick_safely(group)
            except Exception:
                self._groups.pop(group_id, None)
                self._recover_pending.add(group_id)
                logger.exception("Managed group %s checkpoint unavailable; retrying from durable state", group_id)
            await asyncio.sleep(self.interval)

    async def stop(self):
        tasks = [task for task in [self._task, *self._workers.values(), *self._fee_tasks.values()] if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self._workers.clear()
        self._fee_tasks.clear()
        # Shutdown suspends management without closing or detaching positions.
