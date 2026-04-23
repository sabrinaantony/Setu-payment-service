"""
ORM Models

Schema decisions
----------------
merchants          — normalised lookup; merchant_name stored here
transactions       — current_status is denormalised for fast index scans
payment_events     — append-only log; UNIQUE(event_id) = idempotency key
reconciliation_records — one row per txn; discrepancy_flag written at ingest
"""
import enum
from datetime import datetime, timezone

from sqlalchemy import (Boolean, DateTime, ForeignKey, Index, Numeric,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class EventType(str, enum.Enum):
    payment_initiated = "payment_initiated"
    payment_processed = "payment_processed"
    payment_failed    = "payment_failed"
    settled           = "settled"


class TransactionStatus(str, enum.Enum):
    initiated = "initiated"
    processed = "processed"
    failed    = "failed"
    settled   = "settled"


class SettlementStatus(str, enum.Enum):
    pending        = "pending"
    settled        = "settled"
    not_applicable = "not_applicable"


class Merchant(Base):
    __tablename__ = "merchants"
    merchant_id:   Mapped[str]      = mapped_column(String(64), primary_key=True)
    merchant_name: Mapped[str]      = mapped_column(String(255), nullable=False)
    created_at:    Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at:    Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="merchant", lazy="noload")


class Transaction(Base):
    __tablename__ = "transactions"
    transaction_id: Mapped[str]   = mapped_column(String(64), primary_key=True)
    merchant_id:    Mapped[str]   = mapped_column(String(64), ForeignKey("merchants.merchant_id"), nullable=False)
    amount:         Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    currency:       Mapped[str]   = mapped_column(String(8), nullable=False, default="INR")
    current_status: Mapped[str]   = mapped_column(String(32), nullable=False, default="initiated")
    created_at:     Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at:     Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    merchant:       Mapped["Merchant"]              = relationship(back_populates="transactions", lazy="selectin")
    events:         Mapped[list["PaymentEvent"]]    = relationship(back_populates="transaction",
                                                                   order_by="PaymentEvent.timestamp",
                                                                   lazy="selectin")
    reconciliation: Mapped["ReconciliationRecord"]  = relationship(back_populates="transaction",
                                                                    uselist=False, lazy="selectin")
    __table_args__ = (
        Index("ix_txn_merchant",        "merchant_id"),
        Index("ix_txn_status",          "current_status"),
        Index("ix_txn_created",         "created_at"),
        Index("ix_txn_merchant_status", "merchant_id", "current_status"),
    )


class PaymentEvent(Base):
    __tablename__ = "payment_events"
    id:             Mapped[int]      = mapped_column(primary_key=True, autoincrement=True)
    event_id:       Mapped[str]      = mapped_column(String(64), nullable=False)
    event_type:     Mapped[str]      = mapped_column(String(32), nullable=False)
    transaction_id: Mapped[str]      = mapped_column(String(64), ForeignKey("transactions.transaction_id"), nullable=False)
    merchant_id:    Mapped[str]      = mapped_column(String(64), nullable=False)
    amount:         Mapped[float]    = mapped_column(Numeric(14, 2), nullable=False)
    currency:       Mapped[str]      = mapped_column(String(8), nullable=False, default="INR")
    timestamp:      Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_payload:    Mapped[str]      = mapped_column(Text, nullable=True)
    received_at:    Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    transaction: Mapped["Transaction"] = relationship(back_populates="events")

    __table_args__ = (
        UniqueConstraint("event_id", name="uq_payment_events_event_id"),
        Index("ix_pe_transaction_id", "transaction_id"),
        Index("ix_pe_timestamp",      "timestamp"),
    )


class ReconciliationRecord(Base):
    __tablename__ = "reconciliation_records"
    id:                 Mapped[int]      = mapped_column(primary_key=True, autoincrement=True)
    transaction_id:     Mapped[str]      = mapped_column(String(64), ForeignKey("transactions.transaction_id"),
                                                          nullable=False, unique=True)
    payment_status:     Mapped[str]      = mapped_column(String(32), nullable=False)
    settlement_status:  Mapped[str]      = mapped_column(String(32), nullable=False, default="pending")
    discrepancy_flag:   Mapped[bool]     = mapped_column(Boolean, default=False, nullable=False)
    discrepancy_reason: Mapped[str]      = mapped_column(Text, nullable=True)
    settled_at:         Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:         Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at:         Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    transaction: Mapped["Transaction"] = relationship(back_populates="reconciliation")

    __table_args__ = (
        Index("ix_recon_txn_id",    "transaction_id"),
        Index("ix_recon_disc_flag", "discrepancy_flag"),
    )
