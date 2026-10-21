# Managed position groups

Trade Cards use an API-owned `managed_position_executor`. Existing position, grid,
order, and LP executors keep their own contracts. A group owns two independently
accounted legs, one entry intent per leg, and every subsequent reduction order.
Live legs appear in native executor queries and performance under
`controller_id=trade_cards`, with `level_id=<card_id>:1` and `<card_id>:2`.
Dry-run groups remain in the group API and cards; simulated trades never enter
the real executor performance or position lists.

## HTTP contract

All routes use the API's existing HTTP Basic authentication and return objects
directly, without a `data` envelope. Decimal quantities and prices are strings.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/executors/position-groups/capabilities` | Protocol version, modes, connector and actions |
| POST | `/executors/position-groups/preview` | Read-only sizing and risk preview |
| PUT | `/executors/position-groups/{card_id}` | Reserve and create an idempotent group |
| GET | `/executors/position-groups/{card_id}` | Durable group and current command snapshot |
| POST | `/executors/position-groups/{card_id}/commands` | Idempotent management command |

Preview and PUT accept the same flat body:

```json
{
  "account_name": "master_account",
  "connector_name": "binance_perpetual",
  "trading_pair": "ETH-USDT",
  "side": "long",
  "entry_low": "99",
  "entry_high": "101",
  "stop_price": "95",
  "tp1_price": "110",
  "tp2_price": "120",
  "margin": "20",
  "leverage": 10,
  "mode": "dry_run",
  "entry_expires_at": 2000000000
}
```

`margin` is USDT collateral, not notional. Each leg rounds down to a valid exchange
quantity; the quoted combined margin never exceeds the request. Only Binance USDT
perpetuals, isolated margin, single-asset mode and 10x leverage are supported.
The account's position mode is preserved. Automatic margin addition must already
be disabled. Configuration is changed only for an empty symbol and is read back
before any live entry. A unique database reservation prevents two live groups
from configuring or entering the same account/connector/symbol concurrently.

Both modes read actual account balances, fee rates and risk brackets. Dry run
uses live mark prices and simulated fills, with an explicit 0.05% simulated fee;
it never changes leverage/margin settings or submits/cancels an exchange order.
Dry run still requires readable account credentials and valid risk data.

Preview includes `margin`, `notional`, `quantity`, `leg_quantities`,
`entry_method`, `entry_price`, `max_loss`, `profit_tp1`, `profit_tp2`,
`estimated_entry_fees`, and `liquidation_price_kind=estimate`. Target profits
represent each half position's estimated net proceeds; max loss includes estimated
opening and stop-exit fees. Liquidation estimates use authenticated maintenance
margin brackets. Filled live groups expose the exchange liquidation price; missing
risk snapshots return `liquidation_price=null` and `risk_snapshot_available=false`.

Group snapshots add `status`, `filled_amount`, `remaining_amount`, `pnl`, `fees`,
`pnl_complete`, `price_available`, `available_actions`, `close_reason`, `legs`,
`exposure_verified`, `exchange_remaining_amount`, and the latest `operation`.
`remaining_amount` is the order ledger's quantity; when it differs from a readable
exchange position, both quantities remain visible and `pnl_complete` is false.
Respect `available_actions` even during
`RECONCILING`; missing prices must not hide cancellation or full close.
Statuses are `PLACING`, `PENDING`, `OPEN`, `CLOSING`, `RECONCILING`, `CLOSED`,
`DETACHED`, and `FAILED`.

Commands have `{ "operation_id": "unique-command-id", "action": "reduce_half" }`.
Actions are `cancel_entry`, `reduce_half`, `close`, and `detach`. Reducing half uses
the current remaining filled position, after entry cancellation, and refuses
quantities or residuals below lot/minimum-notional rules. Full close can supersede
a pending cancel/half command; it waits for an uncertain existing close rather
than submitting overlapping reductions. Detach cancels entries, waits for all
outcomes, then stops protection while retaining the exchange position.
Partial entry fills can still expose `cancel_entry` while the group is `OPEN`.
A half reduction requires a fresh exchange quantity matching the reconciled
ledger after entry cancellations; a mismatch refuses the operation instead of
guessing how external trades should be allocated between the legs.

Reusing an ID and identical normalized body returns the existing snapshot. A
changed group body produces `409 GROUP_CONFLICT`; a busy symbol produces
`409 SYMBOL_BUSY` before any new group order. Initial risk refusal produces
`422 RISK_REJECTED`. A failure after durable reservation appears in the group's
state, so a client must reconcile that ID rather than invent a new one.
Command IDs are bound to their action. Persist IDs and request bodies before
making calls. A lost response or a single `404` is not proof that a trade failed.

## Protection and recovery

The service persists group, two legs and command journal atomically in
`managed_position_groups`, `managed_position_legs`, and
`managed_position_operations`. Row locks and revisions fence competing writers.
Order intents are committed before submission, including stable exchange client
IDs, cumulative base/quote fills, and individual validated trades (exchange trade
ID, quantity, quote quantity, price, commission asset/amount, and timestamp).
Incomplete trade pages remain explicitly incomplete; previously observed trades
are retained by their IDs. Confirmed cumulative fills are persisted without
waiting for trade-detail or fee requests. Separate settlement work reads at most
one page per order, from
trade ID zero initially and then after the highest persisted ID. Totals become
complete when the collected base/quote fills match the order's exchange totals.
The exchange's client-ID rules alone
are **not** an idempotency guarantee: an uncertain POST is queried by its original
ID and never blindly replayed, even after `NOT_FOUND`. Only an explicit broker
acknowledgement that POST was not attempted permits returning an intent to
`PREPARED`. A crash before that acknowledgement remains uncertain.

TTL cancels the unfilled entry remainder and keeps confirmed fills protected.
Canceled entries never reopen. Partial fills receive absolute stop/target
protection immediately. TP1 closes leg 1; reaching TP1 arms leg 2's stop at gross
entry breakeven, then trails by its original price-to-stop distance until TP2.
Stops observed during an uncertain entry are remembered through price rebounds.
The TP1 milestone is also persisted, so a delayed second-leg fill receives its
breakeven protection even when the price has already fallen back below TP1.
Unknown orders block additional exposure, while independently known fills remain
protected; an unknown close prevents another close against the same quantity.
A delayed market entry is canceled when its original zone is no longer executable.

Startup reloads groups, reconciles submitted order IDs and exchange exposure,
then resumes their controls. Definite rejections and locally canceled unsent
intents retain that evidence. An unsubmitted entry repeats its risk preflight;
groups with previously submitted orders recheck the fixed account settings
without changing them. Dry-run recovery uses its simulated ledger and never
calls the live account-configuration or order-write methods.
Exposure is checked during normal operation as well as recovery. Mismatches
suspend new entries and half reductions and require operator reconciliation;
known reduce-only exits remain possible when risk/price reads are unavailable.
Each group has an independent supervised worker. A database checkpoint failure
discards the working copy and retries from durable state after recovery; no order
is submitted using an uncommitted intent. Optional fee reads run outside the
position-control lock. Exchange I/O has explicit five-second per-request bounds;
an order timeout remains ambiguous and never authorizes a new client ID.
Shutdown drains control and settlement tasks without closing or detaching.
Protection runs in the API process: it cannot execute while the API or required
exchange order connection is unavailable. It is not an exchange-native stop order.

Delayed fee data is collected separately for partial and completed fills, with
bounded retries and exponential delay capped at five minutes. New cumulative
fills or progress through trade pages renew the retry budget. Closed trades stay
terminal while their cards continue reading the final accounting result.
Non-USDT or still-unavailable fees remain explicitly provisional
(`pnl_complete=false`). Terminal native history exports are acknowledged durably
and retried after restart, including corrected fee totals. Detached summaries book
only realized PnL and paid fees, not unrealized profits on the retained position.

## Dependency and validation

`environment.yml` pins the tested PyPI engine to `hummingbot==20260922`.
`utils/core_compatibility.py` validates the connector transport and executor read
surface at startup. The implementation does not patch site-packages or require a
separate core/client repository. The existing SQLAlchemy `create_tables` startup
path creates the three new tables; source changes and tests do not migrate a
running database. Back up and deploy separately under the maintenance workflow.

Focused offline validation:

```bash
conda run -n hummingbot-api pytest -q test/test_managed_position_groups.py \
  test/test_position_group_risk.py test/test_position_group_broker.py \
  test/test_core_compatibility.py test/test_executor_completion_before_creation_row.py \
  test/test_executor_deletion.py
```

The suite exercises durable IDs, SQLite transactions, TTL/partial fills,
cancel/fill races, current-position half reductions, uncertain submission,
protective exits with unavailable data, restart recovery, fee settlement and
native history export. Run the normal offline suite as well when changing shared
executor lifecycle. The normal verification above excludes live integration tests.
Exchange-side acceptance with real orders requires separate explicit authorization.
