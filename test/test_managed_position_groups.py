"""Managed groups exercise real state transitions with an isolated exchange double."""

import asyncio
import copy
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from database.position_groups import ManagedPositionGroup, ManagedPositionLeg, ManagedPositionOperation
from models.position_groups import PositionGroupCommand, PositionGroupRequest
from services.executors.groups import PositionGroupService
from services.executors.managed_position import ManagedPositionConfig, ManagedPositionExecutor, leg_accounting
from services.executors.store import MemoryPositionGroupStore, PositionGroupStore, RevisionConflict


def request(**changes):
    return PositionGroupRequest(
        **(
            {
                "trading_pair": "ETH-USDT",
                "side": "long",
                "entry_low": "99",
                "entry_high": "101",
                "stop_price": "90",
                "tp1_price": "110",
                "tp2_price": "120",
                "margin": "20",
                "mode": "live",
                "entry_expires_at": 1000,
            }
            | changes
        )
    )


class Risk:
    def __init__(self):
        self.prepared = 0

    async def quote(self, req):
        return {
            "total_quantity": "2",
            "leg_quantities": ["1", "1"],
            "entry_type": "MARKET",
            "entry_price": "100",
            "current_price": "100",
            "margin": "20",
            "notional": "200",
            "max_loss": "20",
            "liquidation_price": "89",
            "expected_profit_tp1": "10",
            "expected_profit_tp2": "20",
            "min_base_amount_increment": "0.1",
            "min_order_size": "0.1",
            "min_notional_size": "5",
            "position_mode": "ONEWAY",
        }

    async def prepare_live(self, req):
        self.prepared += 1
        return await self.quote(req)

    async def verify_live(self, req, position_mode):
        assert position_mode == "ONEWAY"


class Broker:
    def __init__(self):
        self.mark = Decimal("100")
        self.orders = {}
        self.submits = []
        self.cancels = []
        self.entry_fill = Decimal("1")
        self.unknown_once = False
        self.unknown_always = False
        self.fail_leg = None
        self.unknown_ids = set()
        self.unknown_submit_number = None
        self.price_failed = False
        self.fees_complete = True

    async def price(self, req):
        if self.price_failed:
            raise TimeoutError("No fresh price")
        return self.mark

    async def position_amount(self, req):
        return sum(
            Decimal(self.orders[o["client_order_id"]]["filled_base"]) * (1 if o["position_action"] == "OPEN" else -1)
            for o in self.submits
        )

    async def position_snapshot(self, req):
        return {"amount": str(await self.position_amount(req)), "liquidation_price": "89", "liquidation_price_kind": "exchange"}

    async def submit(self, req, order):
        self.submits.append(dict(order))
        count = len(self.submits)
        fill = Decimal(order["amount"]) if order["position_action"] == "CLOSE" else min(self.entry_fill, Decimal(order["amount"]))
        status = "FILLED" if fill == Decimal(order["amount"]) else "PARTIALLY_FILLED" if fill else "OPEN"
        if count == self.fail_leg:
            fill, status = Decimal(0), "FAILED"
        outcome = {
            "status": status,
            "filled_base": str(fill),
            "filled_quote": str(fill * self.mark),
            "fees_quote": "0",
            "fees_complete": self.fees_complete,
        }
        self.orders[order["client_order_id"]] = outcome
        if count == self.unknown_submit_number:
            self.unknown_ids.add(order["client_order_id"])
            raise TimeoutError("Response lost")
        if self.unknown_once or self.unknown_always:
            self.unknown_once = False
            raise TimeoutError("Response lost after the exchange accepted the order")
        return outcome

    async def reconcile(self, req, order):
        if self.unknown_always or order["client_order_id"] in self.unknown_ids:
            raise TimeoutError()
        return self.orders.get(order["client_order_id"], {"status": "NOT_FOUND"})

    async def settle_fees(self, req, order):
        return await self.reconcile(req, order)

    async def cancel(self, req, order):
        self.cancels.append(order["client_order_id"])
        if order["client_order_id"] in self.unknown_ids:
            raise TimeoutError("Cancel outcome unavailable")
        result = dict(self.orders[order["client_order_id"]])
        if result["status"] != "FILLED":
            result["status"] = "CANCELED"
        self.orders[order["client_order_id"]] = result
        return result


@pytest.fixture
def setup():
    store, risk, broker = MemoryPositionGroupStore(), Risk(), Broker()
    clock = SimpleNamespace(now=100)
    service = PositionGroupService(store, risk, broker, clock=lambda: clock.now)
    return SimpleNamespace(store=store, risk=risk, broker=broker, service=service, clock=clock)


async def tick(setup, group_id="card"):
    group = await setup.store.load(group_id)
    await setup.service._tick_safely(group)
    return await setup.service.get(group_id)


@pytest.mark.asyncio
async def test_create_and_command_ids_are_durable_and_payload_bound(setup):
    results = await asyncio.gather(*(setup.service.create("card", request()) for _ in range(3)))
    assert all(result["status"] == "OPEN" for result in results)
    assert len(setup.broker.submits) == 2
    with pytest.raises(HTTPException) as error:
        await setup.service.create("card", request(margin="50"))
    assert error.value.status_code == 409
    command = PositionGroupCommand(operation_id="half1", action="reduce_half")
    await setup.service.command("card", command)
    await setup.service.command("card", command)
    assert len(setup.broker.submits) == 4
    assert (await tick(setup))["remaining_amount"] == "1.0"


@pytest.mark.asyncio
async def test_unknown_post_is_reconciled_without_replaying_the_entry(setup):
    setup.broker.unknown_once = True
    result = await setup.service.create("card", request())
    assert result["status"] == "RECONCILING"
    assert len(setup.broker.submits) == 1
    result = await tick(setup)
    assert result["status"] == "OPEN"
    assert len(setup.broker.submits) == 2
    assert len({o["client_order_id"] for o in setup.broker.submits}) == 2


@pytest.mark.asyncio
async def test_unknown_exchange_result_stays_frozen(setup):
    setup.broker.unknown_always = True
    await setup.service.create("card", request())
    for _ in range(3):
        assert (await tick(setup))["status"] == "RECONCILING"
    assert len(setup.broker.submits) == 1


@pytest.mark.asyncio
async def test_ttl_keeps_partial_fills_managed_and_never_reopens_entries(setup):
    setup.broker.entry_fill = Decimal("0.4")
    result = await setup.service.create("card", request())
    assert result["remaining_amount"] == "0.8"
    setup.clock.now = 1001
    result = await tick(setup)
    assert result["status"] == "OPEN"
    assert result["remaining_amount"] == "0.8"
    assert len(setup.broker.cancels) == 2
    await tick(setup)
    assert len(setup.broker.submits) == 2
    setup.broker.mark = Decimal("89")
    result = await tick(setup)
    assert result["status"] == "CLOSED"
    assert result["close_reason"] == "STOP_LOSS"
    assert all(o["amount"] == "0.4" for o in setup.broker.submits[2:])


@pytest.mark.asyncio
async def test_partial_entry_is_protected_before_ttl(setup):
    setup.broker.entry_fill = Decimal("0.4")
    await setup.service.create("card", request())
    setup.broker.mark = Decimal("89")
    assert (await tick(setup))["status"] == "CLOSED"
    assert len(setup.broker.cancels) == 2
    assert len(setup.broker.submits) == 4


@pytest.mark.asyncio
async def test_half_after_tp1_reduces_half_of_the_remaining_leg(setup):
    await setup.service.create("card", request())
    setup.broker.mark = Decimal("110")
    result = await tick(setup)
    assert result["remaining_amount"] == "1"
    result = await setup.service.command("card", PositionGroupCommand(operation_id="half", action="reduce_half"))
    assert result["remaining_amount"] == "0.5"
    assert setup.broker.submits[-1]["amount"] == "0.5"
    assert result["legs"][1]["trailing_stop"] == "100"


@pytest.mark.asyncio
async def test_second_leg_failure_retains_protected_first_leg(setup):
    setup.broker.fail_leg = 2
    result = await setup.service.create("card", request())
    assert result["status"] == "OPEN"
    assert result["remaining_amount"] == "1"
    setup.broker.mark = Decimal("89")
    assert (await tick(setup))["remaining_amount"] == "0"
    assert len([o for o in setup.broker.submits if o["position_action"] == "OPEN"]) == 2


@pytest.mark.asyncio
async def test_cancel_racing_fill_keeps_the_filled_position(setup):
    setup.broker.entry_fill = Decimal(0)
    await setup.service.create("card", request())
    first = setup.broker.submits[0]["client_order_id"]
    setup.broker.orders[first] = {"status": "FILLED", "filled_base": "1", "filled_quote": "100", "fees_quote": "0"}
    result = await setup.service.command("card", PositionGroupCommand(operation_id="cancel", action="cancel_entry"))
    assert result["status"] == "OPEN"
    assert result["remaining_amount"] == "1"


@pytest.mark.asyncio
async def test_detach_retains_exposure_and_does_not_close(setup):
    await setup.service.create("card", request())
    result = await setup.service.command("card", PositionGroupCommand(operation_id="detach", action="detach"))
    assert result["status"] == "DETACHED"
    assert result["remaining_amount"] == "2"
    assert len(setup.broker.submits) == 2


@pytest.mark.asyncio
async def test_dry_run_never_calls_live_mutations(setup):
    result = await setup.service.create("card", request(mode="dry_run"))
    assert result["status"] == "OPEN"
    await setup.service.command("card", PositionGroupCommand(operation_id="close", action="close"))
    assert setup.broker.submits == []
    assert setup.broker.cancels == []
    assert setup.risk.prepared == 0


@pytest.mark.asyncio
async def test_restart_reconciles_existing_orders_and_preserves_trailing(setup):
    await setup.service.create("card", request())
    setup.broker.mark = Decimal("115")
    await tick(setup)
    service = PositionGroupService(setup.store, setup.risk, setup.broker, clock=lambda: setup.clock.now)
    await service.start()
    await service.stop()
    group = await setup.store.load("card")
    await service._tick_safely(group)
    assert len(setup.broker.submits) == 3
    assert (await service.get("card"))["legs"][1]["trailing_stop"] == "105"
    setup.broker.mark = Decimal("104")
    await service._tick_safely(group)
    assert (await service.get("card"))["status"] == "CLOSED"


def test_multiple_exit_fills_use_their_actual_prices_and_fees():
    leg = {
        "orders": [
            {"position_action": "OPEN", "filled_base": "2", "filled_quote": "200", "fees_quote": "0.1"},
            {"position_action": "CLOSE", "filled_base": "1", "filled_quote": "110", "fees_quote": "0.1"},
            {"position_action": "CLOSE", "filled_base": "0.5", "filled_quote": "60", "fees_quote": "0.05"},
        ]
    }
    values = leg_accounting(leg, "long", "105")
    assert values["remaining"] == Decimal("0.5")
    assert values["realized"] == Decimal("20")
    assert values["pnl"] == Decimal("22.25")


def test_individual_trade_ledger_keeps_prior_rows_when_a_refresh_is_incomplete():
    order = {"amount": "1", "status": "OPEN", "filled_base": "0", "filled_quote": "0", "fees_quote": "0"}
    first = {
        "trade_id": "1",
        "base_amount": "0.4",
        "quote_amount": "40",
        "price": "100",
        "commission_asset": "USDT",
        "commission": "0.02",
        "time": 1000,
    }
    second = {
        "trade_id": "2",
        "base_amount": "0.6",
        "quote_amount": "60",
        "price": "100",
        "commission_asset": "USDT",
        "commission": "0.03",
        "time": 1001,
    }
    PositionGroupService._merge_order(
        order,
        {
            "status": "PARTIALLY_FILLED",
            "filled_base": "0.4",
            "filled_quote": "40",
            "fees_quote": "0.02",
            "fills": [first],
            "fills_complete": True,
        },
    )
    PositionGroupService._merge_order(
        order,
        {
            "status": "FILLED",
            "filled_base": "1",
            "filled_quote": "100",
            "fees_quote": "0.05",
            "fills": [second],
            "fills_complete": False,
        },
    )
    assert order["fills"] == [first, second]
    assert not order["fills_complete"]
    PositionGroupService._merge_order(order, {"status": "FILLED", "fills": [], "fills_complete": False})
    assert order["fills"] == [first, second]


@pytest.mark.asyncio
async def test_executor_view_serializes_as_a_native_executor(setup):
    await setup.service.create("card", request())
    group = await setup.store.load("card")
    leg = group["legs"][0]
    config = ManagedPositionConfig(
        id=leg["id"],
        timestamp=100,
        connector_name="binance_perpetual",
        trading_pair="ETH-USDT",
        side=1,
        amount="1",
        group_id="card",
        level_id="card:1",
    )
    executor = ManagedPositionExecutor(config=config, group=group, leg=leg)
    assert executor.executor_info.model_dump()["type"] == "managed_position_executor"
    assert executor.is_trading


@pytest.mark.asyncio
async def test_unknown_second_entry_does_not_disable_the_first_legs_stop(setup):
    setup.broker.unknown_submit_number = 2
    result = await setup.service.create("card", request())
    assert result["status"] == "RECONCILING"
    setup.broker.mark = Decimal("89")
    result = await tick(setup)
    assert result["status"] == "RECONCILING"
    assert result["legs"][0]["remaining_amount"] == "0"
    assert len(setup.broker.submits) == 3  # The known leg's protective close.
    setup.broker.unknown_ids.clear()
    result = await tick(setup)
    assert result["status"] == "CLOSED"
    assert len(setup.broker.submits) == 4


@pytest.mark.asyncio
async def test_unknown_first_entry_recovery_cannot_open_second_leg_past_the_stop(setup):
    setup.broker.unknown_once = True
    await setup.service.create("card", request())
    setup.broker.mark = Decimal("89")
    result = await tick(setup)
    assert result["status"] == "CLOSED"
    assert [order["position_action"] for order in setup.broker.submits] == ["OPEN", "CLOSE"]


@pytest.mark.asyncio
async def test_stop_breach_is_remembered_until_an_unknown_fill_is_visible(setup):
    setup.broker.unknown_submit_number = 1
    await setup.service.create("card", request())
    setup.broker.mark = Decimal("89")
    assert (await tick(setup))["status"] == "RECONCILING"
    setup.broker.mark = Decimal("100")
    setup.broker.unknown_ids.clear()
    result = await tick(setup)
    assert result["status"] == "CLOSED"
    assert result["close_reason"] == "STOP_LOSS"
    assert [order["position_action"] for order in setup.broker.submits] == ["OPEN", "CLOSE"]


@pytest.mark.asyncio
async def test_failed_recovery_risk_snapshot_does_not_block_known_stop_exits(setup):
    await setup.service.create("card", request())
    restarted = PositionGroupService(setup.store, setup.risk, setup.broker, clock=lambda: setup.clock.now)
    await restarted.start()
    await restarted.stop()
    setup.broker.position_amount = AsyncMock(side_effect=TimeoutError())
    setup.broker.mark = Decimal("89")
    group = await setup.store.load("card")
    await restarted._tick_safely(group)
    result = await restarted.get("card")
    assert result["status"] == "RECONCILING"
    assert Decimal(result["remaining_amount"]) == 0
    assert len(setup.broker.submits) == 4


@pytest.mark.asyncio
async def test_explicit_not_submitted_close_retries_the_same_durable_intent(setup):
    from services.executors.broker import Broker as LiveBroker

    await setup.service.create("card", request())
    original_submit = setup.broker.submit
    # Exercise the real adapter's pre-POST failure response across the group boundary.
    live = LiveBroker(AsyncMock(side_effect=TimeoutError("Connector unavailable")))
    setup.broker.submit = live.submit
    setup.broker.mark = Decimal("89")
    result = await tick(setup)
    assert result["status"] == "CLOSING"
    group = await setup.store.load("card")
    ids = [leg["orders"][-1]["client_order_id"] for leg in group["legs"]]
    assert all(leg["orders"][-1]["status"] == "PREPARED" for leg in group["legs"])
    setup.broker.submit = original_submit
    assert (await tick(setup))["status"] == "CLOSED"
    assert [order["client_order_id"] for order in setup.broker.submits[2:]] == ids


@pytest.mark.asyncio
async def test_restart_preserves_definitive_rejection_and_can_retry_protective_close(setup):
    await setup.service.create("card", request())
    setup.broker.fail_leg = 3
    setup.broker.mark = Decimal("89")
    result = await tick(setup)
    assert result["remaining_amount"] == "1"
    failed = setup.broker.submits[2]["client_order_id"]
    del setup.broker.orders[failed]  # A rejected POST has no exchange order to query.
    restarted = PositionGroupService(setup.store, setup.risk, setup.broker, clock=lambda: setup.clock.now)
    await restarted.start()
    await restarted.stop()
    group = await setup.store.load("card")
    setup.broker.position_amount = AsyncMock(side_effect=[Decimal("1"), Decimal("0")])
    await restarted._tick_safely(group)
    assert (await restarted.get("card"))["status"] == "CLOSED"
    assert len(setup.broker.submits) == 5


@pytest.mark.asyncio
async def test_prepared_entry_after_restart_is_canceled_if_the_quote_left_its_zone(setup):
    setup.broker.unknown_once = True
    await setup.service.create("card", request())
    group = await setup.store.load("card")
    # Simulate the durable pre-submit boundary, before an exchange side effect.
    setup.broker.submits.clear()
    setup.broker.orders.clear()
    group["legs"][0]["orders"][0]["status"] = "PREPARED"
    await setup.store.save(group)
    setup.broker.mark = Decimal("105")
    result = await tick(setup)
    assert result["status"] == "CLOSED"
    assert setup.broker.submits == []


@pytest.mark.asyncio
async def test_known_partial_fill_is_protected_while_entry_cancel_is_unknown(setup):
    setup.broker.entry_fill = Decimal("0.4")
    await setup.service.create("card", request())
    setup.broker.unknown_ids = {order["client_order_id"] for order in setup.broker.submits}
    setup.broker.mark = Decimal("89")
    result = await tick(setup)
    assert result["status"] == "RECONCILING"
    assert result["remaining_amount"] == "0.0"
    assert len(setup.broker.submits) == 4
    await tick(setup)
    assert len(setup.broker.submits) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel_entry", "close", "detach"])
async def test_risk_reducing_commands_work_without_market_data(setup, action):
    setup.broker.entry_fill = Decimal("0.4")
    await setup.service.create("card", request())
    setup.broker.price_failed = True
    result = await setup.service.command("card", PositionGroupCommand(operation_id="manual", action=action))
    assert result["operation"]["status"] == "COMPLETED"
    assert not result["price_available"]
    assert len(setup.broker.cancels) == 2
    if action == "close":
        assert result["status"] == "CLOSED"
        assert Decimal(result["remaining_amount"]) == 0
    elif action == "detach":
        assert result["status"] == "DETACHED"
        assert Decimal(result["remaining_amount"]) == Decimal("0.8")


@pytest.mark.asyncio
async def test_small_half_reduction_is_refused_but_stop_protection_continues(setup):
    setup.broker.entry_fill = Decimal("0.1")
    setup.broker.fail_leg = 2
    await setup.service.create("card", request())
    result = await setup.service.command("card", PositionGroupCommand(operation_id="half", action="reduce_half"))
    assert result["operation"]["status"] == "FAILED"
    assert Decimal(result["remaining_amount"]) == Decimal("0.1")
    setup.broker.mark = Decimal("89")
    assert (await tick(setup))["status"] == "CLOSED"


@pytest.mark.asyncio
async def test_half_residual_must_meet_minimum_notional(setup):
    await setup.service.create("card", request())
    group = await setup.store.load("card")
    group["quote"]["min_notional_size"] = "60"
    await setup.store.save(group)
    result = await setup.service.command("card", PositionGroupCommand(operation_id="half", action="reduce_half"))
    assert result["operation"]["status"] == "FAILED"
    assert len(setup.broker.submits) == 2


@pytest.mark.asyncio
async def test_close_waits_for_unknown_close_without_resubmitting(setup):
    await setup.service.create("card", request())
    setup.broker.unknown_submit_number = 3
    result = await setup.service.command("card", PositionGroupCommand(operation_id="close", action="close"))
    assert result["status"] == "RECONCILING"
    await tick(setup)
    assert len(setup.broker.submits) == 4
    setup.broker.unknown_ids.clear()
    assert (await tick(setup))["operation"]["status"] == "COMPLETED"
    assert len(setup.broker.submits) == 4


@pytest.mark.asyncio
async def test_restart_sends_prepared_close_but_does_not_reopen_expired_entry(setup):
    await setup.service.create("card", request())
    group = await setup.store.load("card")
    group["entry_closed"] = True
    group["legs"][0]["orders"].append(
        {
            "client_order_id": "tc-prepared-close",
            "side": "SELL",
            "amount": "0.5",
            "position_action": "CLOSE",
            "order_type": "MARKET",
            "price": None,
            "status": "PREPARED",
            "filled_base": "0",
            "filled_quote": "0",
            "fees_quote": "0",
            "reason": "REDUCE_HALF",
        }
    )
    await setup.store.save(group)
    restarted = PositionGroupService(setup.store, setup.risk, setup.broker, clock=lambda: setup.clock.now)
    await restarted.start()
    await restarted.stop()
    group = await setup.store.load("card")
    await restarted._tick_safely(group)
    assert setup.broker.submits[-1]["client_order_id"] == "tc-prepared-close"
    assert (await restarted.get("card"))["remaining_amount"] == "1.5"


@pytest.mark.asyncio
async def test_restart_unknown_order_keeps_confirmed_other_leg_protected(setup):
    await setup.service.create("card", request())
    setup.broker.unknown_ids = {setup.broker.submits[1]["client_order_id"]}
    restarted = PositionGroupService(setup.store, setup.risk, setup.broker, clock=lambda: setup.clock.now)
    await restarted.start()
    await restarted.stop()
    setup.broker.mark = Decimal("89")
    group = await setup.store.load("card")
    await restarted._tick_safely(group)
    assert (await restarted.get("card"))["legs"][0]["remaining_amount"] == "0"
    assert len(setup.broker.submits) == 4  # Both confirmed quantities can be protected.


@pytest.mark.asyncio
async def test_delayed_fees_settle_after_closed_without_another_trade(setup):
    setup.broker.fees_complete = False
    await setup.service.create("card", request())
    result = await setup.service.command("card", PositionGroupCommand(operation_id="close", action="close"))
    assert result["status"] == "CLOSED"
    assert result["settlement_pending"]
    assert not result["pnl_complete"]
    for outcome in setup.broker.orders.values():
        outcome.update(fees_complete=True, fees_quote="0.1")
    restarted = PositionGroupService(setup.store, setup.risk, setup.broker, clock=lambda: setup.clock.now)
    await restarted.start()
    await restarted.stop()
    setup.clock.now += 30
    group = await setup.store.load("card")
    await restarted._tick_safely(group)
    await restarted._settle_fees("card")
    result = await restarted.get("card")
    assert result["status"] == "CLOSED"
    assert result["pnl_complete"]
    assert result["fees"] == "0.4"
    assert not result["settlement_pending"]
    assert len(setup.broker.submits) == 4


@pytest.mark.asyncio
async def test_terminal_native_history_export_is_retried_and_dry_run_is_excluded(setup):
    native = SimpleNamespace(
        _active_executors={}, _executor_metadata={}, _persist_executor_completed=AsyncMock(return_value=False)
    )
    setup.service.executor_service = native
    await setup.service.create("paper", request(mode="dry_run"))
    assert not native._active_executors
    await setup.service.create("card", request())
    await setup.service.command("card", PositionGroupCommand(operation_id="close", action="close"))
    assert "card" in await setup.store.active_ids()
    native._persist_executor_completed.return_value = True
    await tick(setup)
    assert "card" not in await setup.store.active_ids()
    assert not native._active_executors


@pytest.mark.asyncio
async def test_one_live_group_per_symbol_even_with_different_card_ids(setup):
    await setup.service.create("card", request())
    with pytest.raises(HTTPException, match="SYMBOL_BUSY"):
        await setup.service.create("another", request())
    assert len(setup.broker.submits) == 2


@pytest.fixture
def sql_store():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    for model in (ManagedPositionGroup, ManagedPositionLeg, ManagedPositionOperation):
        model.__table__.create(engine)

    class Adapter:
        def __init__(self, session):
            self.session = session

        def add(self, row):
            self.session.add(row)

        async def execute(self, statement):
            return self.session.execute(statement)

        async def flush(self):
            self.session.flush()

    @asynccontextmanager
    async def session_context():
        with Session(engine) as session:
            try:
                yield Adapter(session)
                session.commit()
            except Exception:
                session.rollback()
                raise

    yield PositionGroupStore(SimpleNamespace(get_session_context=session_context))
    engine.dispose()


@pytest.mark.asyncio
async def test_sql_store_atomic_ledger_reservation_and_revision_fence(setup, sql_store):
    setup.service.store = sql_store
    await setup.service.create("card", request())
    original = await sql_store.load("card")
    stale = copy.deepcopy(original)
    original["legs"][0]["trailing_stop"] = "100"
    await sql_store.save(original)
    with pytest.raises(RevisionConflict):
        await sql_store.save(stale)
    with pytest.raises(HTTPException, match="SYMBOL_BUSY"):
        await setup.service.create("different", request())
    result = await setup.service.command("card", PositionGroupCommand(operation_id="half", action="reduce_half"))
    loaded = await sql_store.load("card")
    assert loaded["operations"]["half"]["status"] == "COMPLETED"
    assert len(loaded["legs"][0]["orders"]) == 2
    assert result["remaining_amount"] == "1.0"


@pytest.mark.asyncio
async def test_http_routes_share_the_group_idempotency_and_error_contract(setup):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from routers.position_groups import router

    app = FastAPI()
    app.state.position_group_service = setup.service
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/executors/position-groups/capabilities")).json()["protocol_version"] == 1
        body = request().model_dump(mode="json")
        assert (await client.post("/executors/position-groups/preview", json=body)).json()["quantity"] == "2"
        created = await client.put("/executors/position-groups/card", json=body)
        assert created.status_code == 200
        assert created.json()["status"] == "OPEN"
        assert (await client.put("/executors/position-groups/card", json=body)).json()["id"] == "card"
        refused = await client.put("/executors/position-groups/other", json=body)
        assert refused.status_code == 409
        assert refused.json()["detail"].startswith("SYMBOL_BUSY:")
        assert (await client.get("/executors/position-groups/absent")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("actual", ["0", "1", None])
async def test_exchange_mismatch_or_unavailable_blocks_half_without_inventing_fills(setup, actual):
    await setup.service.create("card", request())
    setup.broker.position_snapshot = (
        AsyncMock(return_value={"amount": actual, "liquidation_price": None})
        if actual is not None
        else AsyncMock(side_effect=TimeoutError())
    )
    observed = await tick(setup)
    assert observed["status"] == "RECONCILING"
    assert observed["remaining_amount"] == "2"
    assert not observed["pnl_complete"]
    assert "reduce_half" not in observed["available_actions"]
    result = await setup.service.command("card", PositionGroupCommand(operation_id="half-mismatch", action="reduce_half"))
    assert result["operation"]["status"] == "FAILED"
    assert len(setup.broker.submits) == 2
    assert result["remaining_amount"] == "2"


@pytest.mark.asyncio
async def test_tp1_milestone_protects_late_second_leg_fills_across_restart(setup):
    setup.broker.unknown_submit_number = 2
    await setup.service.create("card", request())
    setup.broker.mark = Decimal("111")
    result = await tick(setup)
    assert result["legs"][0]["close_reason"] == "TAKE_PROFIT"
    assert (await setup.store.load("card"))["tp1_reached"]
    restored = PositionGroupService(setup.store, setup.risk, setup.broker, clock=lambda: setup.clock.now)
    await restored.start()
    await restored.stop()
    setup.broker.unknown_ids.clear()
    setup.broker.mark = Decimal("99")
    await restored._tick_safely(await setup.store.load("card"))
    result = await restored.get("card")
    assert result["status"] == "CLOSED"
    assert result["legs"][1]["trailing_stop"] == "100"
    assert result["legs"][1]["close_reason"] == "TRAILING_STOP"


@pytest.mark.asyncio
async def test_restart_revalidates_prepared_but_unsubmitted_entry(setup):
    original_save = setup.store.save

    async def interrupt(group, *, create=False):
        await original_save(group, create=create)
        if group["prepared"] and not any(leg["orders"] for leg in group["legs"]):
            raise asyncio.CancelledError()

    setup.store.save = interrupt
    with pytest.raises(asyncio.CancelledError):
        await setup.service.create("card", request())
    setup.store.save = original_save
    assert not setup.broker.submits
    setup.risk.prepare_live = AsyncMock(side_effect=HTTPException(409, "Automatic margin addition is enabled"))
    restored = PositionGroupService(setup.store, setup.risk, setup.broker, clock=lambda: setup.clock.now)
    await restored.start()
    await restored.stop()
    await restored._tick_safely(await setup.store.load("card"))
    assert not setup.broker.submits
    assert (await restored.get("card"))["status"] == "FAILED"
    setup.risk.prepare_live.assert_awaited_once()


@pytest.mark.asyncio
async def test_existing_exposure_restart_verifies_configuration_without_reconfiguring(setup):
    await setup.service.create("card", request())
    setup.risk.prepare_live = AsyncMock(side_effect=AssertionError("Must not change risk settings with exposure"))
    setup.risk.verify_live = AsyncMock(side_effect=HTTPException(409, "Leverage changed"))
    restored = PositionGroupService(setup.store, setup.risk, setup.broker, clock=lambda: setup.clock.now)
    await restored.start()
    await restored.stop()
    await restored._tick_safely(await setup.store.load("card"))
    assert (await restored.get("card"))["status"] == "RECONCILING"
    setup.broker.mark = Decimal("89")
    await restored._tick_safely(await setup.store.load("card"))
    closed = await restored.get("card")
    assert closed["remaining_amount"] == "0" and closed["status"] == "CLOSED"
    assert "risk configuration" in closed["error"]
    await restored._tick_safely(await setup.store.load("card"))
    assert (await restored.get("card"))["error"] == closed["error"]
    setup.risk.prepare_live.assert_not_awaited()
    setup.risk.verify_live.assert_awaited()


@pytest.mark.asyncio
async def test_dry_run_restart_restores_open_state_and_half_without_live_writes(setup):
    await setup.service.create("card", request(mode="dry_run"))
    setup.risk.prepare_live = AsyncMock(side_effect=AssertionError("Live configuration forbidden"))
    setup.risk.verify_live = AsyncMock(side_effect=AssertionError("Live recovery forbidden"))
    setup.broker.submit = AsyncMock(side_effect=AssertionError("Live submission forbidden"))
    setup.broker.cancel = AsyncMock(side_effect=AssertionError("Live cancel forbidden"))
    restored = PositionGroupService(setup.store, setup.risk, setup.broker, clock=lambda: setup.clock.now)
    await restored.start()
    await restored.stop()
    await restored._tick_safely(await setup.store.load("card"))
    assert (await restored.get("card"))["status"] == "OPEN"
    result = await restored.command("card", PositionGroupCommand(operation_id="paper-half", action="reduce_half"))
    assert result["remaining_amount"] == "1.0"
    assert not (await setup.store.load("card"))["recovering"]
    setup.broker.submit.assert_not_awaited()
    setup.broker.cancel.assert_not_awaited()
    setup.risk.prepare_live.assert_not_awaited()
    setup.risk.verify_live.assert_not_awaited()


async def wait_for_status(service, group_id, status):
    async def wait():
        while (await service.get(group_id))["status"] != status:
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout=2)


@pytest.mark.asyncio
async def test_worker_survives_failed_checkpoint_and_reload_then_recovers_stop(setup):
    await setup.service.create("card", request())
    setup.service.interval = 0.005
    await setup.service.start()
    save, load = setup.store.save, setup.store.load
    failed = asyncio.Event()
    unavailable = False
    fail_once = True

    async def fail_save(group, *, create=False):
        nonlocal unavailable, fail_once
        if fail_once:
            fail_once = False
            unavailable = True
            failed.set()
            raise RuntimeError("database unavailable")
        if unavailable:
            raise RuntimeError("database unavailable")
        await save(group, create=create)

    async def fail_load(group_id):
        if unavailable:
            raise RuntimeError("database unavailable")
        return await load(group_id)

    setup.store.save, setup.store.load = fail_save, fail_load
    setup.broker.mark = Decimal("89")
    try:
        await asyncio.wait_for(failed.wait(), 1)
        await asyncio.sleep(0.02)
        assert not setup.service._task.done()
        assert not setup.service._workers["card"].done()
        unavailable = False
        await wait_for_status(setup.service, "card", "CLOSED")
        assert len(setup.broker.submits) == 4
    finally:
        unavailable = False
        await setup.service.stop()
    assert not setup.service._workers
    assert not setup.service._fee_tasks


@pytest.mark.asyncio
async def test_slow_group_does_not_delay_another_groups_stop():
    brokers = {"ETH-USDT": Broker(), "BTC-USDT": Broker()}
    blocked, entered = asyncio.Event(), asyncio.Event()
    slow = False

    class RoutedBroker:
        async def price(self, req):
            if slow and req.trading_pair == "ETH-USDT":
                entered.set()
                await blocked.wait()
            return await brokers[req.trading_pair].price(req)

        def __getattr__(self, name):
            async def call(req, *args):
                return await getattr(brokers[req.trading_pair], name)(req, *args)

            return call

    service = PositionGroupService(MemoryPositionGroupStore(), Risk(), RoutedBroker(), clock=lambda: 100, interval=0.005)
    await service.create("slow", request())
    await service.create("fast", request(trading_pair="BTC-USDT"))
    slow = True
    brokers["BTC-USDT"].mark = Decimal("89")
    await service.start()
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await wait_for_status(service, "fast", "CLOSED")
        assert not blocked.is_set()
        assert not service._workers["slow"].done()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_slow_fee_read_does_not_hold_position_lock_or_delay_stop(setup):
    setup.broker.fees_complete = False
    await setup.service.create("card", request())
    blocked, entered = asyncio.Event(), asyncio.Event()

    async def slow_fees(req, order):
        entered.set()
        await blocked.wait()
        return await setup.broker.reconcile(req, order)

    setup.broker.settle_fees = slow_fees
    setup.service.interval = 0.005
    await setup.service.start()
    try:
        await asyncio.wait_for(entered.wait(), 1)
        setup.broker.mark = Decimal("89")
        await wait_for_status(setup.service, "card", "CLOSED")
        assert not blocked.is_set()
        assert len(setup.broker.submits) == 4
    finally:
        await setup.service.stop()


@pytest.mark.asyncio
async def test_fee_snapshot_cannot_overwrite_new_partial_fills_and_resets_budget(setup):
    setup.broker.entry_fill = Decimal("0.5")
    setup.broker.fees_complete = False
    await setup.service.create("card", request())
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_fees(req, order):
        entered.set()
        await release.wait()
        return {**order, "fees_complete": True, "fees_quote": "0.1"}

    setup.broker.settle_fees = slow_fees
    task = asyncio.create_task(setup.service._settle_fees("card"))
    await asyncio.wait_for(entered.wait(), 1)
    first_id = setup.broker.submits[0]["client_order_id"]
    setup.broker.orders[first_id].update(status="FILLED", filled_base="1", filled_quote="100")
    await tick(setup)
    release.set()
    await task
    group = await setup.store.load("card")
    first = group["legs"][0]["orders"][0]
    assert first["filled_base"] == "1"
    assert first["filled_quote"] == "100"
    assert first["fee_attempts"] == 0
    assert not first["fees_complete"]


@pytest.mark.asyncio
async def test_historical_closed_group_does_not_require_new_symbol_exposure_read(setup):
    await setup.service.create("card", request())
    await setup.service.command("card", PositionGroupCommand(operation_id="close", action="close"))
    group = await setup.store.load("card")
    group.pop("exposure_verified")
    group["history_exported"] = False
    await setup.store.save(group)
    setup.broker.position_snapshot = AsyncMock(side_effect=AssertionError("Historical completion is independent of new exposure"))
    restored = PositionGroupService(setup.store, setup.risk, setup.broker, clock=lambda: setup.clock.now)
    await restored.start()
    await restored.stop()
    result = await restored.get("card")
    assert result["status"] == "CLOSED" and result["pnl_complete"]
    setup.broker.position_snapshot.assert_not_awaited()


def test_fee_pagination_progress_resets_failure_budget():
    order = {"amount": "1", "filled_base": "1", "filled_quote": "100", "fees_quote": "0", "fills": [], "fee_attempts": 12}
    PositionGroupService._merge_order(
        order,
        {
            "status": "FILLED",
            "filled_base": "1",
            "filled_quote": "100",
            "fees_quote": "0.01",
            "fees_complete": False,
            "fills_complete": False,
            "fills": [{"trade_id": "1", "base_amount": "0.5"}],
        },
    )
    assert order["fee_attempts"] == 0
    assert order["fee_next_at"] == 0


@pytest.mark.asyncio
async def test_fee_completion_after_history_export_is_exported_again(setup):
    native = SimpleNamespace(
        _active_executors={}, _executor_metadata={}, _persist_executor_completed=AsyncMock(return_value=True)
    )
    setup.service.executor_service = native
    setup.broker.fees_complete = False
    await setup.service.create("card", request())
    await setup.service.command("card", PositionGroupCommand(operation_id="close", action="close"))
    assert (await setup.store.load("card"))["history_exported"]
    assert not native._active_executors
    for outcome in setup.broker.orders.values():
        outcome.update(fees_complete=True, fees_quote="0.1")
    await setup.service._settle_fees("card")
    assert not (await setup.store.load("card"))["history_exported"]
    assert "card" in await setup.store.active_ids()
    await tick(setup)
    assert (await setup.store.load("card"))["history_exported"]
    assert native._persist_executor_completed.await_count == 4
    assert (await setup.service.get("card"))["fees"] == "0.4"


@pytest.mark.asyncio
async def test_supervisor_recovers_canceled_worker_without_replaying_unknown_orders(setup):
    await setup.service.create("card", request())
    setup.risk.verify_live = AsyncMock()
    setup.service.interval = 0.005
    await setup.service.start()
    entered, release = asyncio.Event(), asyncio.Event()
    try:
        await wait_for_status(setup.service, "card", "OPEN")
        assert setup.risk.verify_live.await_count == 1
        price = setup.broker.price

        async def blocked_price(req):
            entered.set()
            await release.wait()
            return await price(req)

        setup.broker.price = blocked_price
        await asyncio.wait_for(entered.wait(), 1)
        old_worker = setup.service._workers["card"]
        old_worker.cancel()
        await asyncio.gather(old_worker, return_exceptions=True)
        setup.broker.unknown_ids.add(setup.broker.submits[1]["client_order_id"])
        release.set()

        async def resumed():
            while setup.risk.verify_live.await_count < 2:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(resumed(), 1)
        assert setup.service._workers["card"] is not old_worker
        assert (await setup.service.get("card"))["status"] == "RECONCILING"
        assert len(setup.broker.submits) == 2
    finally:
        await setup.service.stop()


@pytest.mark.asyncio
async def test_restarting_supervisor_does_not_restore_over_an_active_worker(setup):
    await setup.service.create("card", request())
    setup.service.interval = 0.005
    await setup.service.start()
    try:
        await wait_for_status(setup.service, "card", "OPEN")
        worker = setup.service._workers["card"]
        setup.service._task.cancel()
        await asyncio.gather(setup.service._task, return_exceptions=True)
        setup.service._restore = AsyncMock(side_effect=AssertionError("Active worker retains ownership"))
        await setup.service.start()
        assert setup.service._workers["card"] is worker
        setup.service._restore.assert_not_awaited()
        setup.broker.mark = Decimal("89")
        await wait_for_status(setup.service, "card", "CLOSED")
    finally:
        await setup.service.stop()
