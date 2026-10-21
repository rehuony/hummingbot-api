"""Transactional checkpoints. No exchange operation may precede its intent commit."""

import copy
import hashlib
import json

from sqlalchemy import select

from database.position_groups import ManagedPositionGroup, ManagedPositionLeg, ManagedPositionOperation


class RevisionConflict(RuntimeError):
    pass


def active_position_key(group):
    request = group["request"]
    if request["mode"] != "live" or group["status"] in ("CLOSED", "DETACHED", "FAILED"):
        return None
    identity = [request[key] for key in ("account_name", "connector_name", "trading_pair")]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


class PositionGroupStore:
    def __init__(self, db_manager):
        self.db = db_manager

    async def load(self, group_id):
        async with self.db.get_session_context() as session:
            record = (
                await session.execute(select(ManagedPositionGroup).where(ManagedPositionGroup.group_id == group_id))
            ).scalar_one_or_none()
            if record is None:
                return None
            result = json.loads(record.payload)
            result["revision"] = record.revision
            result["legs"] = [
                json.loads(row.checkpoint)
                for row in (
                    await session.execute(
                        select(ManagedPositionLeg)
                        .where(ManagedPositionLeg.group_id == group_id)
                        .order_by(ManagedPositionLeg.number)
                    )
                ).scalars()
            ]
            result["operations"] = {
                row.operation_id: json.loads(row.checkpoint)
                for row in (
                    await session.execute(select(ManagedPositionOperation).where(ManagedPositionOperation.group_id == group_id))
                ).scalars()
            }
            return result

    async def active_ids(self):
        async with self.db.get_session_context() as session:
            return list(
                (
                    await session.execute(
                        select(ManagedPositionGroup.group_id).where(ManagedPositionGroup.needs_attention.is_(True))
                    )
                ).scalars()
            )

    async def save(self, group, *, create=False):
        """Commit group, both legs and operation journal together, using a revision fence."""
        payload = {key: value for key, value in group.items() if key not in ("legs", "operations", "revision")}
        async with self.db.get_session_context() as session:
            row = (
                await session.execute(
                    select(ManagedPositionGroup).where(ManagedPositionGroup.group_id == group["id"]).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                if not create:
                    raise RevisionConflict("Group disappeared")
                row = ManagedPositionGroup(group_id=group["id"], request_hash=group["request_hash"], revision=0)
                session.add(row)
            elif create or row.revision != group["revision"]:
                raise RevisionConflict("Concurrent group update")
            row.status = group["status"]
            row.active_position_key = active_position_key(group)
            row.needs_attention = needs_attention(group)
            row.payload = json.dumps(payload, sort_keys=True)
            revision = row.revision + 1
            row.revision = revision
            await session.flush()
            for leg in group["legs"]:
                stored = (
                    await session.execute(select(ManagedPositionLeg).where(ManagedPositionLeg.executor_id == leg["id"]))
                ).scalar_one_or_none()
                if stored is None:
                    stored = ManagedPositionLeg(executor_id=leg["id"], group_id=group["id"], number=leg["number"])
                    session.add(stored)
                stored.checkpoint = json.dumps(leg, sort_keys=True)
            for operation_id, operation in group["operations"].items():
                stored = (
                    await session.execute(
                        select(ManagedPositionOperation).where(
                            ManagedPositionOperation.group_id == group["id"],
                            ManagedPositionOperation.operation_id == operation_id,
                        )
                    )
                ).scalar_one_or_none()
                if stored is None:
                    stored = ManagedPositionOperation(
                        group_id=group["id"], operation_id=operation_id, request_hash=operation["request_hash"]
                    )
                    session.add(stored)
                stored.checkpoint = json.dumps(operation, sort_keys=True)
            await session.flush()
        group["revision"] = revision


class MemoryPositionGroupStore:
    """The same revision contract, for deterministic offline tests."""

    def __init__(self):
        self.groups = {}

    async def load(self, group_id):
        return copy.deepcopy(self.groups.get(group_id))

    async def active_ids(self):
        return [key for key, value in self.groups.items() if needs_attention(value)]

    async def save(self, group, *, create=False):
        previous = self.groups.get(group["id"])
        if previous is not None and (create or previous["revision"] != group["revision"]):
            raise RevisionConflict("Concurrent group update")
        if previous is None and not create:
            raise RevisionConflict("Group disappeared")
        key = active_position_key(group)
        if key and any(other_id != group["id"] and active_position_key(other) == key for other_id, other in self.groups.items()):
            raise RevisionConflict("Another live group owns this symbol")
        group["revision"] = group.get("revision", 0) + 1
        self.groups[group["id"]] = copy.deepcopy(group)


def needs_attention(group):
    return (
        group["status"] not in ("CLOSED", "DETACHED", "FAILED")
        or group.get("settlement_pending", False)
        or not group.get("history_exported", False)
    )
