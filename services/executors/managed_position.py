"""One managed leg's durable fill accounting and native executor read interface.

The group service owns the control loop and serializes both legs' commands. This
class deliberately does not inherit PositionExecutor's single-close-order model.
"""

from decimal import Decimal
from types import SimpleNamespace
from typing import Literal

from hummingbot.core.data_type.common import TradeType
from hummingbot.strategy_v2.executors.data_types import ExecutorConfigBase
from hummingbot.strategy_v2.executors.executor_base import ExecutorBase
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo

ZERO = Decimal("0")
TERMINAL_ORDERS = {"FILLED", "CANCELED", "FAILED"}


def decimal(value):
    return Decimal(str(value))


def leg_accounting(leg, side, mark):
    entries = [order for order in leg["orders"] if order["position_action"] == "OPEN"]
    exits = [order for order in leg["orders"] if order["position_action"] == "CLOSE"]
    opened = sum((decimal(order.get("filled_base", "0")) for order in entries), ZERO)
    closed = sum((decimal(order.get("filled_base", "0")) for order in exits), ZERO)
    entry_quote = sum((decimal(order.get("filled_quote", "0")) for order in entries), ZERO)
    exit_quote = sum((decimal(order.get("filled_quote", "0")) for order in exits), ZERO)
    fees = sum((decimal(order.get("fees_quote", "0")) for order in leg["orders"]), ZERO)
    if closed > opened:
        raise ValueError("Exit fills exceed the leg's entry fills")
    average = entry_quote / opened if opened else ZERO
    remaining = opened - closed
    direction = Decimal(1 if side == "long" else -1)
    realized = direction * (exit_quote - closed * average)
    unrealized = direction * (decimal(mark) - average) * remaining
    return {
        "opened": opened,
        "closed": closed,
        "remaining": remaining,
        "average": average,
        "entry_quote": entry_quote,
        "exit_quote": exit_quote,
        "fees": fees,
        "realized": realized,
        "unrealized": unrealized,
        "pnl": realized + unrealized - fees,
    }


class ManagedPositionConfig(ExecutorConfigBase):
    type: Literal["managed_position_executor"] = "managed_position_executor"
    connector_name: str
    trading_pair: str
    side: TradeType
    amount: Decimal
    level_id: str
    group_id: str


class ManagedExecutorInfo(ExecutorInfo):
    """Extend the external discriminated config union without patching the wheel."""

    config: ManagedPositionConfig


class ManagedPositionExecutor(ExecutorBase):
    """Read-compatible executor view; no independent task can race the group loop."""

    def __init__(self, config, group, leg):
        super().__init__(strategy=SimpleNamespace(connectors={}), connectors=[], config=config)
        self.group = group
        self.leg = leg
        self._status = RunnableStatus.RUNNING

    @property
    def status(self):
        return RunnableStatus.TERMINATED if self.group["status"] in ("CLOSED", "DETACHED", "FAILED") else RunnableStatus.RUNNING

    @property
    def is_closed(self):
        return self.status == RunnableStatus.TERMINATED

    @property
    def is_active(self):
        return not self.is_closed

    @property
    def values(self):
        return leg_accounting(self.leg, self.group["request"]["side"], self.group["current_price"])

    @property
    def is_trading(self):
        return self.is_active and self.values["remaining"] > 0

    @property
    def filled_amount_quote(self):
        return self.values["entry_quote"] + self.values["exit_quote"]

    def get_net_pnl_quote(self):
        values = self.values
        # Detaching ends management, not the exchange position. Do not book the
        # remaining mark-to-market profit as a realized executor result.
        return values["realized"] - values["fees"] if self.group["status"] == "DETACHED" else values["pnl"]

    def get_net_pnl_pct(self):
        values = self.values
        return self.get_net_pnl_quote() / values["entry_quote"] if values["entry_quote"] else ZERO

    def get_cum_fees_quote(self):
        return self.values["fees"]

    def get_custom_info(self):
        values = self.values
        return {
            "group_id": self.group["id"],
            "level_id": self.config.level_id,
            "pnl_complete": all(order.get("fees_complete", True) for order in self.leg["orders"]),
            "management_status": self.group["status"],
            "remaining_amount": str(values["remaining"]),
            "current_position_average_price": str(values["average"]),
            "current_price": self.group["current_price"],
            "order_ids": [o["client_order_id"] for o in self.leg["orders"]],
        }

    @property
    def executor_info(self):
        return ManagedExecutorInfo(
            id=self.config.id,
            timestamp=self.config.timestamp,
            type=self.config.type,
            status=self.status,
            close_type=self.close_type,
            close_timestamp=self.close_timestamp,
            config=self.config,
            net_pnl_pct=self.net_pnl_pct,
            net_pnl_quote=self.net_pnl_quote,
            cum_fees_quote=self.cum_fees_quote,
            filled_amount_quote=self.filled_amount_quote,
            is_active=self.is_active,
            is_trading=self.is_trading,
            custom_info=self.get_custom_info(),
            controller_id=self.config.controller_id,
        )

    def start(self):
        # PositionGroupService starts and recovers the one authoritative loop.
        self._status = RunnableStatus.RUNNING

    def stop(self):
        # Shutdown suspends management; the durable group is recovered on startup.
        pass

    def early_stop(self, keep_position=False):
        raise RuntimeError("Managed positions must use position-group commands")

    def set_terminal_reason(self):
        if not self.is_closed:
            self.close_type = None
            self.close_timestamp = None
            return
        reason = "FAILED" if self.group["status"] == "FAILED" else self.leg.get("close_reason") or self.group.get("close_reason")
        self.close_type = {
            "STOP_LOSS": CloseType.STOP_LOSS,
            "TAKE_PROFIT": CloseType.TAKE_PROFIT,
            "TRAILING_STOP": CloseType.TRAILING_STOP,
            "FAILED": CloseType.FAILED,
        }.get(reason, CloseType.EARLY_STOP)
        self.close_timestamp = self.group.get("closed_at")
