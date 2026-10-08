"""History deletion is terminal-only, atomic, and leaves exposure records intact."""
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from database.models import ExecutorOrder, ExecutorPerformanceSnapshot, ExecutorRecord, PositionHoldRecord
from database.repositories.executor_repository import ExecutorRepository
from services.executor_service import ExecutorService


@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def enforce_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    models = (ExecutorRecord, ExecutorOrder, ExecutorPerformanceSnapshot, PositionHoldRecord)
    for model in models:
        model.__table__.create(engine)

    @asynccontextmanager
    async def session_context():
        with Session(engine) as session:
            async def execute(statement):
                return session.execute(statement)
            try:
                yield SimpleNamespace(execute=execute)
                session.commit()
            except Exception:
                session.rollback()
                raise

    def insert(executor_id="finished", **overrides):
        identity = dict(executor_id=executor_id, executor_type="order_executor",
                        account_name="master", connector_name="binance_perpetual",
                        trading_pair="BTC-USDT", status="TERMINATED")
        with Session(engine) as session:
            session.add(ExecutorRecord(**(identity | overrides)))
            session.flush()
            session.add(ExecutorOrder(executor_id=executor_id, client_order_id="order-1",
                                      order_type="open", trade_type="BUY", amount=1))
            session.add(ExecutorPerformanceSnapshot(**identity))
            session.add(PositionHoldRecord(account_name="master", connector_name="binance_perpetual",
                                           trading_pair="BTC-USDT", controller_id=executor_id,
                                           executor_ids=json.dumps([executor_id]), buy_amount_base=1))
            session.commit()

    def counts():
        with Session(engine) as session:
            return tuple(session.query(model).count() for model in models)

    service = ExecutorService.__new__(ExecutorService)
    service._active_executors = {}
    service._executor_metadata = {}
    service.db_manager = SimpleNamespace(get_session_context=session_context)
    yield SimpleNamespace(insert=insert, counts=counts, service=service)
    engine.dispose()


@pytest.mark.asyncio
async def test_delete_removes_only_target_history_and_preserves_holds(db):
    db.insert()
    db.insert("other")
    assert await db.service.delete_executor("finished") == {"executor_id": "finished", "deleted": True}
    assert db.counts() == (1, 1, 1, 2)
    with pytest.raises(HTTPException) as error:
        await db.service.delete_executor("finished")
    assert error.value.status_code == 404
    assert db.counts() == (1, 1, 1, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["RUNNING", "NOT_STARTED", "SHUTTING_DOWN"])
async def test_nonterminal_record_cannot_be_deleted(db, status):
    db.insert(status=status)
    with pytest.raises(HTTPException) as error:
        await db.service.delete_executor("finished")
    assert error.value.status_code == 409
    assert db.counts() == (1, 1, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["_active_executors", "_executor_metadata"])
async def test_running_or_completing_in_memory_cannot_be_deleted(db, state):
    db.insert()
    getattr(db.service, state)["finished"] = object()
    with pytest.raises(HTTPException) as error:
        await db.service.delete_executor("finished")
    assert error.value.status_code == 409
    assert db.counts() == (1, 1, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("close_type,final_state", [
    ("FAILED", '{"position_address":"on-chain"}'),
    ("SYSTEM_CLEANUP", None),
    ("POSITION_HOLD", '{"orphaned_position":true}'),
    ("POSITION_HOLD", '{"hold_reason":"close failed"}'),
    ("FAILED", "malformed"),
    ("FAILED", "[]"),
])
async def test_lp_exposure_cannot_lose_its_recovery_record(db, close_type, final_state):
    db.insert(executor_type="lp_executor", close_type=close_type, final_state=final_state)
    with pytest.raises(HTTPException) as error:
        await db.service.delete_executor("finished")
    assert error.value.status_code == 409
    assert db.counts() == (1, 1, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("final_state", ['{}', '{"position_address":"recovered", "orphan_resolved":true}'])
async def test_closed_or_resolved_lp_can_be_deleted(db, final_state):
    db.insert(executor_type="lp_executor", close_type="FAILED", final_state=final_state)
    await db.service.delete_executor("finished")
    assert db.counts() == (0, 0, 0, 1)


@pytest.mark.asyncio
async def test_failed_delete_rolls_back_children_and_parent(db, monkeypatch):
    db.insert()
    original = ExecutorRepository.delete_executor

    async def fail_after_delete(self, executor_id):
        await original(self, executor_id)
        raise RuntimeError("storage failed")

    monkeypatch.setattr(ExecutorRepository, "delete_executor", fail_after_delete)
    with pytest.raises(RuntimeError):
        await db.service.delete_executor("finished")
    assert db.counts() == (1, 1, 1, 1)


@pytest.mark.asyncio
async def test_route_preserves_conflict_and_sanitizes_database_errors():
    from routers.executors import delete_executor

    service = SimpleNamespace(delete_executor=AsyncMock(side_effect=HTTPException(409, "Still running")))
    with pytest.raises(HTTPException) as error:
        await delete_executor("running", service)
    assert error.value.status_code == 409
    service.delete_executor.side_effect = RuntimeError("private database address")
    with pytest.raises(HTTPException) as error:
        await delete_executor("running", service)
    assert error.value.status_code == 500
    assert "private" not in error.value.detail
