"""
ORM Models — schema for the Setu Payment Lifecycle Service.

Design notes
------------
merchants
    Normalised lookup table.  merchant_name is kept here so that every
    event row does not need to repeat it.  Upserted on every ingest call
    so the name stays current without a separate admin API.

transactions
    One row per transaction_id.  current_status is a *denormalised*
    snapshot of the latest payment state.  This makes GET /transactions
    ?status=settled a single index scan (ix_transactions_current_status)
    rather than a correlated sub-query over payment_events.

payment_events
    Append-only event log.  The UNIQUE constraint on event_id is the
    primary idempotency mechanism: an IntegrityError on duplicate insert
    is caught in the service layer and turned into a no-op.

reconciliation_records
    One row per transaction.  Tracks settlement state independently from
    payment state so discrepancy detection is a WHERE clause, not a scan.
    discrepancy_flag is written at ingest time so GET /reconciliation
    /discrepancies never does a full-table computation.
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EventType(str, enum.Enum):
    payment_initiated = "payment_initiated"
    payment_processed = "payment_processed"
    payment_failed = "payment_failed"
    settled = "settled"


class TransactionStatus(str, enum.Enum):
    initiated = "initiated"
    processed = "processed"
    failed = "failed"
    settled = "settled"


class SettlementStatus(str, enum.Enum):
    pending = "pending"
    settled = "settled"
    not_applicable = "not_applicable"  # terminal: payment_failed


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Merchant(Base):
    __tablename__ = "merchants"

    merchant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="merchant", lazy="noload"
    )


class Transaction(Base):
    __tablename__ = "transactions"

    transaction_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("merchants.merchant_id"), nullable=False
    )
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="INR")
    current_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TransactionStatus.initiated.value
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    merchant: Mapped["Merchant"] = relationship(back_populates="transactions", lazy="selectin")
    events: Mapped[list["PaymentEvent"]] = relationship(
        back_populates="transaction",
        order_by="PaymentEvent.timestamp",
        lazy="selectin",
    )
    reconciliation: Mapped["ReconciliationRecord"] = relationship(
        back_populates="transaction", uselist=False, lazy="selectin"
    )

    __table_args__ = (
        Index("ix_transactions_merchant_id", "merchant_id"),
        Index("ix_transactions_current_status", "current_status"),
        Index("ix_transactions_created_at", "created_at"),
        # Composite index for the most common combined filter
        Index("ix_transactions_merchant_status", "merchant_id", "current_status"),
    )


class PaymentEvent(Base):
    """Append-only event log. event_id UNIQUE = idempotency guard."""

    __tablename__ = "payment_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    transaction_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("transactions.transaction_id"), nullable=False
    )
    merchant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="INR")
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_payload: Mapped[str] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    transaction: Mapped["Transaction"] = relationship(back_populates="events")

    __table_args__ = (
        UniqueConstraint("event_id", name="uq_payment_events_event_id"),
        Index("ix_payment_events_transaction_id", "transaction_id"),
        Index("ix_payment_events_timestamp", "timestamp"),
    )


class ReconciliationRecord(Base):
    """One row per transaction. discrepancy_flag written at ingest time."""

    __tablename__ = "reconciliation_records"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    transaction_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("transactions.transaction_id"), nullable=False, unique=True
    )
    payment_status: Mapped[str] = mapped_column(String(32), nullable=False)
    settlement_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SettlementStatus.pending.value
    )
    discrepancy_flag: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    discrepancy_reason: Mapped[str] = mapped_column(Text, nullable=True)
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    transaction: Mapped["Transaction"] = relationship(back_populates="reconciliation")

    __table_args__ = (
        Index("ix_reconciliation_transaction_id", "transaction_id"),
        Index("ix_reconciliation_discrepancy_flag", "discrepancy_flag"),
    )
