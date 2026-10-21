"""Durable identities and checkpoints for managed position groups only."""

from sqlalchemy import Boolean, Column, ForeignKey, Integer, String, Text, UniqueConstraint

from database.models import Base


class ManagedPositionGroup(Base):
    __tablename__ = "managed_position_groups"
    group_id = Column(String(64), primary_key=True)
    request_hash = Column(String(64), nullable=False)
    # A live symbol has one ledger owner, even across concurrent API workers.
    # Terminal groups release the nullable UNIQUE reservation.
    active_position_key = Column(String(64), nullable=True, unique=True)
    needs_attention = Column(Boolean, nullable=False, default=True, index=True)
    status = Column(String(32), nullable=False, index=True)
    payload = Column(Text, nullable=False)
    revision = Column(Integer, nullable=False, default=0)


class ManagedPositionLeg(Base):
    __tablename__ = "managed_position_legs"
    executor_id = Column(String(64), primary_key=True)
    group_id = Column(String(64), ForeignKey("managed_position_groups.group_id"), nullable=False, index=True)
    number = Column(Integer, nullable=False)
    checkpoint = Column(Text, nullable=False)
    __table_args__ = (UniqueConstraint("group_id", "number"),)


class ManagedPositionOperation(Base):
    __tablename__ = "managed_position_operations"
    group_id = Column(String(64), ForeignKey("managed_position_groups.group_id"), primary_key=True)
    operation_id = Column(String(64), primary_key=True)
    request_hash = Column(String(64), nullable=False)
    checkpoint = Column(Text, nullable=False)
