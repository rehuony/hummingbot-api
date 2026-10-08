"""Portfolio accounting uses full snapshots and exchange account equity."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("hummingbot")

from database import AccountRepository
from database.models import TokenState
from services.accounts_service import AccountsService


def row(value=1000):
    return {"token": "USDT", "units": value, "price": 1, "value": value, "available_units": value}


@pytest.mark.asyncio
@pytest.mark.parametrize("connector_name", ["binance_perpetual", "binance_perpetual_testnet"])
async def test_binance_equity_and_wallet_are_from_one_account_response(connector_name):
    service = AccountsService.__new__(AccountsService)
    connector = MagicMock()
    connector._update_balances = AsyncMock()
    connector._api_get = AsyncMock(return_value={"assets": [
        {"asset": "USDT", "walletBalance": "1000", "availableBalance": "600", "unrealizedProfit": "-200"},
        {"asset": "USDC", "walletBalance": "50", "availableBalance": "40", "unrealizedProfit": "5"},
        {"asset": "BUSD", "walletBalance": "0", "availableBalance": "0", "unrealizedProfit": "0"},
    ]})
    rows = await service._get_connector_tokens_info(connector, connector_name)
    assert rows == [
        {**row(), "available_units": 600, "unrealized_pnl": -200, "equity_value": 800},
        {**row(50), "token": "USDC", "available_units": 40, "unrealized_pnl": 5, "equity_value": 55},
    ]
    connector.get_all_balances.assert_not_called()
    connector.get_available_balance.assert_not_called()
    connector._api_get.assert_awaited_once()


@pytest.mark.asyncio
async def test_zero_wallet_with_floating_pnl_is_not_filtered_out():
    service = AccountsService.__new__(AccountsService)
    connector = MagicMock()
    connector._api_get = AsyncMock(return_value={"assets": [
        {"asset": "USDT", "walletBalance": "0", "availableBalance": "0", "unrealizedProfit": "-2"},
    ]})
    rows = await service._get_connector_tokens_info(connector, "binance_perpetual", skip_balance_refresh=True)
    assert len(rows) == 1
    assert rows[0]["value"] == 0
    assert rows[0]["equity_value"] == -2


@pytest.mark.asyncio
async def test_zero_balance_connector_is_persisted():
    service = AccountsService.__new__(AccountsService)
    service.accounts_state = {"master": {"binance": [], "binance_perpetual": [row()]}}
    service.db_manager = MagicMock()
    repository = MagicMock()
    repository.save_account_state = AsyncMock()
    with patch("services.accounts_service.AccountRepository", return_value=repository):
        await service.dump_account_state()
    calls = repository.save_account_state.await_args_list
    assert [(c.args[1], c.args[2]) for c in calls] == [("binance", []), ("binance_perpetual", [row()])]
    assert calls[0].args[3] == calls[1].args[3]


def service_with_failed_transfer_read():
    service = AccountsService.__new__(AccountsService)
    service.accounts_state = {"master": {"binance": [row()], "binance_perpetual": []}}
    service._connector_service = MagicMock()
    service._connector_service.get_all_trading_connectors.return_value = {
        "master": {"binance": object(), "binance_perpetual": object()}
    }
    service._connector_service.is_gateway_connector.return_value = False
    return service


@pytest.mark.asyncio
async def test_failed_refresh_does_not_publish_half_of_a_transfer():
    service = service_with_failed_transfer_read()
    original = deepcopy(service.accounts_state)
    service._get_connector_tokens_info = AsyncMock(side_effect=[RuntimeError("offline"), [row()]])
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        await service.update_account_state(skip_gateway=True)
    assert error.value.status_code == 502
    assert service.accounts_state == original


@pytest.mark.asyncio
async def test_periodic_refresh_does_not_persist_partial_data():
    service = service_with_failed_transfer_read()
    original = deepcopy(service.accounts_state)
    service.check_all_connectors = AsyncMock()
    service._refresh_and_get_tokens_info = AsyncMock(side_effect=[RuntimeError("offline"), [row()]])
    service._update_gateway_balances = AsyncMock()
    service.dump_account_state = AsyncMock()
    service.update_account_state_interval = 300
    with patch("services.accounts_service.asyncio.sleep", side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await service.update_account_state_loop()
    service.dump_account_state.assert_not_awaited()
    assert service.accounts_state == original


@pytest.mark.asyncio
async def test_snapshot_refresh_requests_strict_connector_errors():
    service = AccountsService.__new__(AccountsService)
    service._connector_service = SimpleNamespace(refresh_connector_state=AsyncMock(side_effect=RuntimeError("offline")))
    service._get_connector_tokens_info = AsyncMock()
    connector = object()
    with pytest.raises(RuntimeError, match="offline"):
        await service._refresh_and_get_tokens_info(connector, "binance", "master")
    service._connector_service.refresh_connector_state.assert_awaited_once_with(connector, "binance", "master", strict=True)
    service._get_connector_tokens_info.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("equity", [None, 800])
async def test_snapshot_serialization_preserves_equity_and_legacy_unknowns(equity):
    session = MagicMock()
    session.flush = AsyncMock()
    repository = AccountRepository(session)
    balance = row()
    if equity is not None:
        balance.update(unrealized_pnl=-200, equity_value=equity)
    await repository.save_account_state("master", "binance_perpetual", [balance])
    saved = next(call.args[0] for call in session.add.call_args_list if isinstance(call.args[0], TokenState))
    serialized = repository._token_state_to_dict(saved)
    assert serialized["value"] == 1000
    assert serialized["equity_value"] == equity
    assert serialized["unrealized_pnl"] == (-200 if equity is not None else None)


@pytest.mark.asyncio
async def test_history_milliseconds_are_parsed_in_utc():
    from models.trading import PortfolioHistoryFilterRequest
    from routers.portfolio import get_portfolio_history
    service = SimpleNamespace(load_account_state_history=AsyncMock(return_value=([], None, False)))
    await get_portfolio_history(PortfolioHistoryFilterRequest(start_time=1791350000000), service)
    since = service.load_account_state_history.await_args.kwargs["start_time"]
    assert since == datetime.fromtimestamp(1791350000, timezone.utc)


@pytest.mark.asyncio
async def test_filtered_balance_reads_do_not_remove_other_wallets():
    from models.trading import PortfolioStateFilterRequest
    from routers.portfolio import get_portfolio_state
    state = {"master": {"binance": [row(100)], "binance_perpetual": [row(900)]}}
    original = deepcopy(state)
    service = SimpleNamespace(get_accounts_state=lambda: state)
    result = await get_portfolio_state(PortfolioStateFilterRequest(connector_names=["binance"]), service)
    assert result == {"master": {"binance": [row(100)]}}
    assert state == original


@pytest.mark.asyncio
async def test_equity_schema_migrations_preserve_unknown_historical_values():
    from database.connection import AsyncDatabaseManager
    connection = SimpleNamespace(execute=AsyncMock())
    connection.execute.return_value.fetchone = MagicMock(return_value=None)
    manager = AsyncDatabaseManager.__new__(AsyncDatabaseManager)
    await manager._run_migrations(connection)
    sql = [str(c.args[0]) for c in connection.execute.await_args_list]
    for column in ("equity_value", "unrealized_pnl"):
        assert f"ALTER TABLE token_states ADD COLUMN {column} NUMERIC(30,18)" in sql
    assert not any("UPDATE token_states" in statement or "DEFAULT 0" in statement for statement in sql if "token_states" in statement)
