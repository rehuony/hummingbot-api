"""The narrow, versioned contract for card-owned perpetual position groups."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PositionGroupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    account_name: str = Field(default="master_account", min_length=1, max_length=100)
    connector_name: Literal["binance_perpetual"] = "binance_perpetual"
    trading_pair: str = Field(pattern=r"^[A-Z0-9]+-USDT$")
    side: Literal["long", "short"]
    entry_low: Decimal = Field(gt=0)
    entry_high: Decimal = Field(gt=0)
    stop_price: Decimal = Field(gt=0)
    tp1_price: Decimal = Field(gt=0)
    tp2_price: Decimal = Field(gt=0)
    margin: Decimal = Field(gt=0)
    leverage: Literal[10] = 10
    # Omitted means live, as groups saved before the mode returned were all live.
    mode: Literal["live", "mock"] = "live"
    entry_expires_at: float = Field(gt=0)

    @model_validator(mode="before")
    @classmethod
    def legacy_mode(cls, data):
        if isinstance(data, dict) and data.get("mode") == "dry_run":
            return {**data, "mode": "mock"}
        return data

    @model_validator(mode="after")
    def price_order(self):
        if self.entry_low > self.entry_high:
            raise ValueError("entry_low must not exceed entry_high")
        if self.side == "long":
            valid = self.stop_price < self.entry_low <= self.entry_high < self.tp1_price < self.tp2_price
        else:
            valid = self.tp2_price < self.tp1_price < self.entry_low <= self.entry_high < self.stop_price
        if not valid:
            raise ValueError("stop, entry zone and targets must be ordered in the trade direction")
        return self


class PositionGroupCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    action: Literal["cancel_entry", "reduce_half", "close", "detach"]


def stored_mode(request: dict) -> str:
    """The mode of a persisted request, which may predate ``mock`` or carry none."""
    mode = request.get("mode", "live")
    return "mock" if mode == "dry_run" else mode
