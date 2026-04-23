#!/usr/bin/env python3
"""
run.py — Setu Payment Lifecycle Service
========================================
Self-contained bootstrap. Requires only:
  • Python 3.11+  (stdlib only to start)
  • pip            (comes with Python)
  • sample_events.json in the same folder as this script

Usage:
    python3 run.py            # installs deps, seeds DB, starts server on :8000
    python3 run.py --test     # installs deps, seeds DB, runs test suite, exits
    python3 run.py --port 9000

What it does:
  1. Creates a virtual environment (.venv/) if one doesn't exist
  2. pip-installs all required packages into it
  3. Writes all application source files to ./setu_app/
  4. Seeds setu_payments.db from sample_events.json
  5. Starts uvicorn  (or runs pytest with --test)
"""

import os
import subprocess
import sys
import textwrap
import venv
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
APP_DIR = ROOT / "setu_app"
SAMPLE_EVENTS = ROOT / "sample_events.json"

REQUIREMENTS = [
    "fastapi==0.115.5",
    "uvicorn[standard]==0.32.1",
    "sqlalchemy[asyncio]==2.0.36",
    "greenlet>=3.0.0",
    "aiosqlite==0.20.0",
    "pydantic==2.10.3",
    "httpx==0.27.2",
    "pytest==8.3.4",
    "pytest-asyncio==0.24.0",
    "anyio==4.7.0",
]

# ---------------------------------------------------------------------------
# Source files (written into setu_app/)
# ---------------------------------------------------------------------------

FILES: dict[str, str] = {}

FILES["setu_app/__init__.py"] = ""

# ── models ──────────────────────────────────────────────────────────────────
FILES["setu_app/models.py"] = '''
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
'''

# ── db session ───────────────────────────────────────────────────────────────
FILES["setu_app/db.py"] = '''
import os
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./setu_payments.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_async_engine(DATABASE_URL, echo=False, connect_args=_connect_args)

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession,
    expire_on_commit=False, autoflush=False, autocommit=False,
)


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
'''

# ── schemas ──────────────────────────────────────────────────────────────────
FILES["setu_app/schemas.py"] = '''
from __future__ import annotations
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator
from setu_app.models import EventType  # noqa


class EventIngestionRequest(BaseModel):
    event_id:       str      = Field(..., description="Globally unique event identifier")
    event_type:     EventType
    transaction_id: str
    merchant_id:    str
    merchant_name:  str
    amount:         float    = Field(..., gt=0)
    currency:       str      = Field(default="INR", max_length=8)
    timestamp:      datetime

    @field_validator("currency")
    @classmethod
    def _upper(cls, v): return v.upper()

    model_config = ConfigDict(use_enum_values=True)


class EventIngestionResponse(BaseModel):
    status: str; message: str; event_id: str; transaction_id: str; is_duplicate: bool = False


class MerchantOut(BaseModel):
    merchant_id: str; merchant_name: str
    model_config = ConfigDict(from_attributes=True)


class PaymentEventOut(BaseModel):
    event_id: str; event_type: str; timestamp: datetime
    amount: float; currency: str; received_at: datetime
    model_config = ConfigDict(from_attributes=True)


class ReconciliationOut(BaseModel):
    payment_status: str; settlement_status: str
    discrepancy_flag: bool; discrepancy_reason: Optional[str] = None
    settled_at: Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)


class TransactionOut(BaseModel):
    transaction_id: str; merchant_id: str; amount: float; currency: str
    current_status: str; created_at: datetime; updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


class TransactionDetailOut(BaseModel):
    transaction_id: str; merchant_id: str; amount: float; currency: str
    current_status: str; created_at: datetime; updated_at: datetime
    merchant: Optional[MerchantOut] = None
    events: list[PaymentEventOut] = []
    reconciliation: Optional[ReconciliationOut] = None
    model_config = ConfigDict(from_attributes=True)


class PaginatedTransactions(BaseModel):
    total: int; page: int; page_size: int; items: list[TransactionOut]


class ReconciliationSummaryItem(BaseModel):
    merchant_id: str; merchant_name: Optional[str] = None
    date: Optional[str] = None; status: str
    transaction_count: int; total_amount: float


class ReconciliationSummaryResponse(BaseModel):
    group_by: str; total_transactions: int; items: list[ReconciliationSummaryItem]


class DiscrepancyItem(BaseModel):
    transaction_id: str; merchant_id: str; amount: float; currency: str
    payment_status: str; settlement_status: str; discrepancy_reason: str; created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class DiscrepancyResponse(BaseModel):
    total: int; page: int; page_size: int; items: list[DiscrepancyItem]
'''

# ── event service ─────────────────────────────────────────────────────────────
FILES["setu_app/event_service.py"] = '''
"""
Event ingestion service.

Idempotency  — UNIQUE(event_id) at DB level; IntegrityError → no-op.
State machine — status only advances by rank (initiated<processed/failed<settled).
Discrepancy  — flag written at ingest so discrepancy query is a cheap WHERE.
"""
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from setu_app.models import (Merchant, PaymentEvent, ReconciliationRecord,
                              SettlementStatus, Transaction, TransactionStatus)
from setu_app.schemas import EventIngestionRequest, EventIngestionResponse

_RANK = {
    TransactionStatus.initiated.value: 0,
    TransactionStatus.processed.value: 1,
    TransactionStatus.failed.value:    1,
    TransactionStatus.settled.value:   2,
}
_EV_TO_STATUS = {
    "payment_initiated": TransactionStatus.initiated.value,
    "payment_processed": TransactionStatus.processed.value,
    "payment_failed":    TransactionStatus.failed.value,
    "settled":           TransactionStatus.settled.value,
}


async def ingest_event(db: AsyncSession, p: EventIngestionRequest) -> EventIngestionResponse:
    new_status = _EV_TO_STATUS.get(p.event_type, p.event_type)

    # 1. Upsert merchant
    merchant = await db.get(Merchant, p.merchant_id)
    if merchant is None:
        db.add(Merchant(merchant_id=p.merchant_id, merchant_name=p.merchant_name))
    else:
        merchant.merchant_name = p.merchant_name

    # 2. Upsert transaction — capture status BEFORE advancing
    transaction = await db.get(Transaction, p.transaction_id)
    if transaction is None:
        transaction = Transaction(
            transaction_id=p.transaction_id, merchant_id=p.merchant_id,
            amount=float(p.amount), currency=p.currency, current_status=new_status,
        )
        db.add(transaction)
        previous_status = None
    else:
        previous_status = transaction.current_status
        if _RANK.get(new_status, -1) > _RANK.get(transaction.current_status, -1):
            transaction.current_status = new_status

    # 3. Insert event — UNIQUE(event_id) is the idempotency guard
    db.add(PaymentEvent(
        event_id=p.event_id, event_type=p.event_type,
        transaction_id=p.transaction_id, merchant_id=p.merchant_id,
        amount=float(p.amount), currency=p.currency, timestamp=p.timestamp,
        raw_payload=json.dumps(p.model_dump(mode="json")),
    ))

    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return EventIngestionResponse(
            status="duplicate",
            message="Event already processed; no state changes applied.",
            event_id=p.event_id, transaction_id=p.transaction_id, is_duplicate=True,
        )

    # 4. Upsert reconciliation + detect discrepancies
    recon = await db.scalar(
        select(ReconciliationRecord).where(ReconciliationRecord.transaction_id == p.transaction_id)
    )
    disc_flag, disc_reason = False, None

    if recon is None:
        s_status = (SettlementStatus.not_applicable.value
                    if new_status == TransactionStatus.failed.value
                    else SettlementStatus.pending.value)
        db.add(ReconciliationRecord(
            transaction_id=p.transaction_id,
            payment_status=new_status, settlement_status=s_status,
        ))
    else:
        recon.payment_status = transaction.current_status

        if p.event_type == "settled":
            status_before = previous_status or recon.payment_status
            if status_before == TransactionStatus.failed.value:
                disc_flag, disc_reason = True, "Settlement received for a failed payment."
            else:
                recon.settlement_status = SettlementStatus.settled.value
                recon.settled_at = datetime.now(timezone.utc)

        elif p.event_type == "payment_failed":
            if recon.settlement_status == SettlementStatus.settled.value:
                disc_flag = True
                disc_reason = "Payment marked failed after settlement was already recorded."
            else:
                recon.settlement_status = SettlementStatus.not_applicable.value

        elif p.event_type == "payment_processed":
            if recon.payment_status == TransactionStatus.settled.value:
                disc_flag = True
                disc_reason = "payment_processed received after transaction already settled."

        recon.discrepancy_flag, recon.discrepancy_reason = disc_flag, disc_reason

    await db.flush()
    return EventIngestionResponse(
        status="accepted", message="Event ingested successfully.",
        event_id=p.event_id, transaction_id=p.transaction_id, is_duplicate=False,
    )
'''

# ── transaction service ───────────────────────────────────────────────────────
FILES["setu_app/transaction_service.py"] = '''
"""All filtering, pagination, and sorting done in SQL — never in Python."""
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from setu_app.models import Transaction
from setu_app.schemas import PaginatedTransactions, TransactionDetailOut, TransactionOut

_SORT = {"created_at": Transaction.created_at,
         "updated_at": Transaction.updated_at,
         "amount":     Transaction.amount}


async def list_transactions(
    db: AsyncSession, merchant_id=None, status=None,
    date_from=None, date_to=None,
    sort_by="created_at", sort_order="desc",
    page=1, page_size=20,
) -> PaginatedTransactions:
    q = select(Transaction)
    if merchant_id: q = q.where(Transaction.merchant_id    == merchant_id)
    if status:      q = q.where(Transaction.current_status == status)
    if date_from:   q = q.where(Transaction.created_at     >= date_from)
    if date_to:     q = q.where(Transaction.created_at     <= date_to)

    total = await db.scalar(select(func.count()).select_from(q.subquery())) or 0

    col = _SORT[sort_by]
    q = q.order_by(col.desc() if sort_order == "desc" else col.asc())
    q = q.offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(q)).scalars().all()

    return PaginatedTransactions(
        total=total, page=page, page_size=page_size,
        items=[TransactionOut.model_validate(t) for t in rows],
    )


async def get_transaction_detail(db: AsyncSession, transaction_id: str):
    q = (
        select(Transaction)
        .options(selectinload(Transaction.merchant),
                 selectinload(Transaction.events),
                 selectinload(Transaction.reconciliation))
        .where(Transaction.transaction_id == transaction_id)
    )
    txn = (await db.execute(q)).scalar_one_or_none()
    return None if txn is None else TransactionDetailOut.model_validate(txn)
'''

# ── reconciliation service ────────────────────────────────────────────────────
FILES["setu_app/reconciliation_service.py"] = '''
"""
Summary  — pure SQL GROUP BY across merchant / date / status.
Discrepancies — indexed WHERE discrepancy_flag=TRUE plus two live SQL checks.
"""
from typing import Literal, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from setu_app.models import (Merchant, ReconciliationRecord,
                              SettlementStatus, Transaction, TransactionStatus)
from setu_app.schemas import (DiscrepancyItem, DiscrepancyResponse,
                               ReconciliationSummaryItem, ReconciliationSummaryResponse)


async def get_reconciliation_summary(
    db: AsyncSession,
    group_by: Literal["merchant", "date", "status"] = "merchant",
    merchant_id: Optional[str] = None,
) -> ReconciliationSummaryResponse:

    base = (
        select(Transaction.merchant_id, Merchant.merchant_name,
               Transaction.current_status, Transaction.amount, Transaction.created_at)
        .join(Merchant, Merchant.merchant_id == Transaction.merchant_id)
    )
    if merchant_id:
        base = base.where(Transaction.merchant_id == merchant_id)
    sub = base.subquery()

    if group_by == "merchant":
        agg = (
            select(sub.c.merchant_id, sub.c.merchant_name,
                   sub.c.current_status.label("status"),
                   func.count().label("n"), func.sum(sub.c.amount).label("total"))
            .group_by(sub.c.merchant_id, sub.c.merchant_name, sub.c.current_status)
            .order_by(sub.c.merchant_id, sub.c.current_status)
        )
        rows = (await db.execute(agg)).all()
        items = [ReconciliationSummaryItem(
            merchant_id=r.merchant_id, merchant_name=r.merchant_name,
            status=r.status, transaction_count=r.n, total_amount=float(r.total or 0))
            for r in rows]

    elif group_by == "date":
        de = func.date(sub.c.created_at)
        agg = (
            select(sub.c.merchant_id, sub.c.merchant_name, de.label("dt"),
                   sub.c.current_status.label("status"),
                   func.count().label("n"), func.sum(sub.c.amount).label("total"))
            .group_by(sub.c.merchant_id, sub.c.merchant_name, de, sub.c.current_status)
            .order_by(de.desc(), sub.c.merchant_id)
        )
        rows = (await db.execute(agg)).all()
        items = [ReconciliationSummaryItem(
            merchant_id=r.merchant_id, merchant_name=r.merchant_name,
            date=str(r.dt), status=r.status,
            transaction_count=r.n, total_amount=float(r.total or 0))
            for r in rows]

    else:  # status
        agg = (
            select(sub.c.current_status.label("status"),
                   sub.c.merchant_id, sub.c.merchant_name,
                   func.count().label("n"), func.sum(sub.c.amount).label("total"))
            .group_by(sub.c.current_status, sub.c.merchant_id, sub.c.merchant_name)
            .order_by(sub.c.current_status, sub.c.merchant_id)
        )
        rows = (await db.execute(agg)).all()
        items = [ReconciliationSummaryItem(
            merchant_id=r.merchant_id, merchant_name=r.merchant_name,
            status=r.status, transaction_count=r.n, total_amount=float(r.total or 0))
            for r in rows]

    return ReconciliationSummaryResponse(
        group_by=group_by,
        total_transactions=sum(i.transaction_count for i in items),
        items=items,
    )


async def get_discrepancies(
    db: AsyncSession, merchant_id: Optional[str] = None,
    page: int = 1, page_size: int = 50,
) -> DiscrepancyResponse:

    q = (
        select(Transaction.transaction_id, Transaction.merchant_id,
               Transaction.amount, Transaction.currency,
               ReconciliationRecord.payment_status, ReconciliationRecord.settlement_status,
               ReconciliationRecord.discrepancy_reason, Transaction.created_at)
        .join(ReconciliationRecord,
              ReconciliationRecord.transaction_id == Transaction.transaction_id)
        .where(or_(
            ReconciliationRecord.discrepancy_flag == True,       # noqa: E712
            ((ReconciliationRecord.payment_status  == TransactionStatus.processed.value) &
             (ReconciliationRecord.settlement_status == SettlementStatus.pending.value)),
            ((ReconciliationRecord.payment_status  == TransactionStatus.failed.value) &
             (ReconciliationRecord.settlement_status == SettlementStatus.settled.value)),
        ))
        .order_by(Transaction.created_at.desc())
    )
    if merchant_id:
        q = q.where(Transaction.merchant_id == merchant_id)

    total = await db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows  = (await db.execute(q.offset((page - 1) * page_size).limit(page_size))).all()

    def _reason(pay, settle):
        if pay == TransactionStatus.processed.value and settle == SettlementStatus.pending.value:
            return "Payment processed but settlement is still pending."
        if pay == TransactionStatus.failed.value and settle == SettlementStatus.settled.value:
            return "Payment failed but settlement was recorded."
        return "State inconsistency detected."

    items = [DiscrepancyItem(
        transaction_id=r.transaction_id, merchant_id=r.merchant_id,
        amount=float(r.amount), currency=r.currency,
        payment_status=r.payment_status, settlement_status=r.settlement_status,
        discrepancy_reason=r.discrepancy_reason or _reason(r.payment_status, r.settlement_status),
        created_at=r.created_at,
    ) for r in rows]

    return DiscrepancyResponse(total=total, page=page, page_size=page_size, items=items)
'''

# ── API routers ───────────────────────────────────────────────────────────────
FILES["setu_app/api_events.py"] = '''
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from setu_app.db import get_db
from setu_app.event_service import ingest_event
from setu_app.schemas import EventIngestionRequest, EventIngestionResponse

router = APIRouter()

@router.post("", response_model=EventIngestionResponse,
             summary="Ingest a payment lifecycle event",
             description=(
                 "Idempotent. Submitting the same event_id twice returns "
                 "is_duplicate=true without mutating any state. "
                 "Valid event_type values: payment_initiated, payment_processed, "
                 "payment_failed, settled."))
async def post_event(payload: EventIngestionRequest, db: AsyncSession = Depends(get_db)):
    return await ingest_event(db, payload)
'''

FILES["setu_app/api_transactions.py"] = '''
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from setu_app.db import get_db
from setu_app.transaction_service import get_transaction_detail, list_transactions
from setu_app.schemas import PaginatedTransactions, TransactionDetailOut

router = APIRouter()
_STATUSES = {"initiated", "processed", "failed", "settled"}
_SORT     = {"created_at", "updated_at", "amount"}


@router.get("", response_model=PaginatedTransactions,
            summary="List transactions",
            description="Paginated list. Filter by merchant_id, status, date range. "
                        "Sort by created_at (default), updated_at, or amount.")
async def list_txns(
    merchant_id: Optional[str]      = Query(None),
    status:      Optional[str]      = Query(None, description="initiated|processed|failed|settled"),
    date_from:   Optional[datetime] = Query(None, description="ISO 8601 lower bound on created_at"),
    date_to:     Optional[datetime] = Query(None, description="ISO 8601 upper bound on created_at"),
    sort_by:     str                = Query("created_at", description="created_at|updated_at|amount"),
    sort_order:  str                = Query("desc",       description="asc|desc"),
    page:        int                = Query(1,   ge=1),
    page_size:   int                = Query(20,  ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    if status     and status    not in _STATUSES: raise HTTPException(422, f"Invalid status '{status}'")
    if sort_by    not in _SORT:                   raise HTTPException(422, f"Invalid sort_by '{sort_by}'")
    if sort_order not in {"asc","desc"}:          raise HTTPException(422, "sort_order must be asc or desc")
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be before date_to")
    return await list_transactions(db, merchant_id=merchant_id, status=status,
                                   date_from=date_from, date_to=date_to,
                                   sort_by=sort_by, sort_order=sort_order,
                                   page=page, page_size=page_size)


@router.get("/{transaction_id}", response_model=TransactionDetailOut,
            summary="Fetch transaction details",
            description="Returns merchant info, full event history (ordered by timestamp), "
                        "and reconciliation status. 404 if not found.")
async def get_txn(transaction_id: str, db: AsyncSession = Depends(get_db)):
    result = await get_transaction_detail(db, transaction_id)
    if result is None:
        raise HTTPException(404, f"Transaction \'{transaction_id}\' not found.")
    return result
'''

FILES["setu_app/api_reconciliation.py"] = '''
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from setu_app.db import get_db
from setu_app.reconciliation_service import get_discrepancies, get_reconciliation_summary
from setu_app.schemas import DiscrepancyResponse, ReconciliationSummaryResponse

router = APIRouter()


@router.get("/summary", response_model=ReconciliationSummaryResponse,
            summary="Reconciliation summary",
            description="Aggregated transaction counts and totals. "
                        "group_by: merchant (default) | date | status.")
async def summary(
    group_by:    str           = Query("merchant", description="merchant|date|status"),
    merchant_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    if group_by not in {"merchant","date","status"}:
        raise HTTPException(422, f"Invalid group_by \'{group_by}\'. Valid: merchant, date, status")
    return await get_reconciliation_summary(db, group_by=group_by, merchant_id=merchant_id)


@router.get("/discrepancies", response_model=DiscrepancyResponse,
            summary="Reconciliation discrepancies",
            description="Transactions where payment and settlement states are inconsistent: "
                        "processed-but-never-settled, settled-after-failure, or flagged at ingest.")
async def discrepancies(
    merchant_id: Optional[str] = Query(None),
    page:        int           = Query(1,  ge=1),
    page_size:   int           = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    return await get_discrepancies(db, merchant_id=merchant_id, page=page, page_size=page_size)
'''

# ── main FastAPI app ──────────────────────────────────────────────────────────
FILES["setu_app/main.py"] = '''
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from setu_app.db import engine
from setu_app.models import Base
from setu_app.api_events import router as events_router
from setu_app.api_transactions import router as transactions_router
from setu_app.api_reconciliation import router as reconciliation_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(
    title="Setu Payment Lifecycle Service",
    description="Ingest payment lifecycle events, manage transaction state, and report reconciliation.",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(events_router,        prefix="/events",          tags=["Events"])
app.include_router(transactions_router,  prefix="/transactions",    tags=["Transactions"])
app.include_router(reconciliation_router,prefix="/reconciliation",  tags=["Reconciliation"])


@app.get("/", tags=["Health"])
async def root():
    return {"service": "Setu Payment Lifecycle Service", "status": "healthy", "version": "1.0.0", "docs": "/docs"}


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "healthy"}
'''

# ── seed script ───────────────────────────────────────────────────────────────
FILES["setu_app/seed.py"] = '''
"""
Bulk-seed from sample_events.json using ON CONFLICT DO UPDATE/NOTHING.
Safe to run multiple times (idempotent). Completes in ~3s for 10k events.
"""
import asyncio, json, time
from datetime import datetime, timezone
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from setu_app.db import AsyncSessionLocal, engine
from setu_app.models import (Base, Merchant, PaymentEvent,
                              ReconciliationRecord, SettlementStatus,
                              Transaction, TransactionStatus)

BATCH = 500
_RANK = {"initiated":0,"processed":1,"failed":1,"settled":2}
_EV   = {"payment_initiated":"initiated","payment_processed":"processed",
          "payment_failed":"failed","settled":"settled"}


def _parse(ts): return datetime.fromisoformat(ts)


def compute_state(events):
    merchants, txns, recon = {}, {}, {}
    for ev in sorted(events, key=lambda e: e["timestamp"]):
        mid, tid, etype = ev["merchant_id"], ev["transaction_id"], ev["event_type"]
        ns = _EV.get(etype, etype)
        merchants[mid] = {"merchant_id": mid, "merchant_name": ev["merchant_name"]}
        if tid not in txns:
            txns[tid] = {"transaction_id":tid,"merchant_id":mid,
                         "amount":float(ev["amount"]),"currency":ev.get("currency","INR"),
                         "current_status":ns}
        elif _RANK.get(ns,-1) > _RANK.get(txns[tid]["current_status"],-1):
            txns[tid]["current_status"] = ns

        if tid not in recon:
            ss = (SettlementStatus.not_applicable.value
                  if ns == TransactionStatus.failed.value else SettlementStatus.pending.value)
            recon[tid] = {"transaction_id":tid,"payment_status":ns,
                          "settlement_status":ss,"discrepancy_flag":False,
                          "discrepancy_reason":None,"settled_at":None}
        else:
            r = recon[tid]
            r["payment_status"] = txns[tid]["current_status"]
            if etype == "settled":
                if r["payment_status"] == TransactionStatus.failed.value:
                    r["discrepancy_flag"]  = True
                    r["discrepancy_reason"]= "Settlement received for a failed payment."
                else:
                    r["settlement_status"] = SettlementStatus.settled.value
                    r["settled_at"]        = _parse(ev["timestamp"])
            elif etype == "payment_failed":
                if r["settlement_status"] == SettlementStatus.settled.value:
                    r["discrepancy_flag"]  = True
                    r["discrepancy_reason"]= "Payment marked failed after settlement recorded."
                else:
                    r["settlement_status"] = SettlementStatus.not_applicable.value

    # Flag stuck: processed but settlement still pending
    for tid, r in recon.items():
        if (not r["discrepancy_flag"]
                and txns[tid]["current_status"] == TransactionStatus.processed.value
                and r["settlement_status"] == SettlementStatus.pending.value):
            r["discrepancy_flag"]  = True
            r["discrepancy_reason"]= "Payment processed but settlement is still pending."

    return merchants, txns, recon


async def _upsert(db, model, rows, pk, update_cols):
    for i in range(0, len(rows), BATCH):
        stmt = sqlite_insert(model).values(rows[i:i+BATCH])
        stmt = stmt.on_conflict_do_update(
            index_elements=[pk],
            set_={c: getattr(stmt.excluded, c) for c in update_cols})
        await db.execute(stmt)
    await db.commit()


async def seed(path: str):
    print(f"Loading {path} …")
    with open(path) as f: events = json.load(f)
    total = len(events)
    unique_ids = len({e["event_id"] for e in events})
    print(f"  {total} events  |  {unique_ids} unique  |  {total-unique_ids} duplicates")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    t0 = time.perf_counter()
    merchants, txns, recon = compute_state(events)
    print(f"  State computed in {time.perf_counter()-t0:.2f}s — "
          f"{len(merchants)} merchants, {len(txns)} transactions")

    async with AsyncSessionLocal() as db:
        await _upsert(db, Merchant, list(merchants.values()),
                      "merchant_id", ["merchant_name"])
        print(f"  ✓ Merchants ({len(merchants)})")

        await _upsert(db, Transaction, list(txns.values()),
                      "transaction_id", ["current_status"])
        print(f"  ✓ Transactions ({len(txns)})")

        seen, evs = set(), []
        for ev in events:
            if ev["event_id"] not in seen:
                seen.add(ev["event_id"])
                evs.append({"event_id":ev["event_id"],"event_type":ev["event_type"],
                             "transaction_id":ev["transaction_id"],"merchant_id":ev["merchant_id"],
                             "amount":float(ev["amount"]),"currency":ev.get("currency","INR"),
                             "timestamp":_parse(ev["timestamp"]),"raw_payload":json.dumps(ev)})
        for i in range(0, len(evs), BATCH):
            stmt = sqlite_insert(PaymentEvent).values(evs[i:i+BATCH])
            stmt = stmt.on_conflict_do_nothing(index_elements=["event_id"])
            await db.execute(stmt)
        await db.commit()
        print(f"  ✓ Events ({len(evs)} inserted, {total-len(evs)} duplicates skipped)")

        disc = sum(1 for r in recon.values() if r["discrepancy_flag"])
        await _upsert(db, ReconciliationRecord, list(recon.values()), "transaction_id",
                      ["payment_status","settlement_status","discrepancy_flag",
                       "discrepancy_reason","settled_at"])
        print(f"  ✓ Reconciliation ({len(recon)} records, {disc} discrepancies flagged)")

    print(f"Seed done in {time.perf_counter()-t0:.2f}s ✓")
'''

# ── tests ─────────────────────────────────────────────────────────────────────
FILES["setu_app/test_api.py"] = '''
"""32 integration tests — in-memory SQLite, no external deps."""
import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from setu_app.db import get_db
from setu_app.main import app
from setu_app.models import Base

_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(_URL, connect_args={"check_same_thread": False})
_Session = async_sessionmaker(_engine, class_=AsyncSession,
                               expire_on_commit=False, autoflush=False)

async def _override():
    async with _Session() as s:
        try:   yield s; await s.commit()
        except: await s.rollback(); raise

@pytest_asyncio.fixture(scope="session", autouse=True)
async def _tables():
    async with _engine.begin() as c: await c.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as c: await c.run_sync(Base.metadata.drop_all)

@pytest_asyncio.fixture
async def client():
    app.dependency_overrides[get_db] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()

_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

def _ev(etype="payment_initiated", tid=None, mid="m_test", mname="TestMerchant",
        amount=1000.0, eid=None, offset=0):
    return {"event_id": eid or str(uuid.uuid4()), "event_type": etype,
            "transaction_id": tid or str(uuid.uuid4()),
            "merchant_id": mid, "merchant_name": mname,
            "amount": amount, "currency": "INR",
            "timestamp": (_NOW + timedelta(seconds=offset)).isoformat()}

async def _post(client, p):
    r = await client.post("/events", json=p)
    assert r.status_code == 200, r.text
    return r.json()

# ─── POST /events ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_ingest_initiated(client):
    r = await _post(client, _ev())
    assert r["status"] == "accepted" and not r["is_duplicate"]

@pytest.mark.asyncio
async def test_full_happy_path(client):
    tid = str(uuid.uuid4())
    for i, et in enumerate(["payment_initiated","payment_processed","settled"]):
        await _post(client, _ev(et, tid=tid, offset=i*60))
    d = (await client.get(f"/transactions/{tid}")).json()
    assert d["current_status"] == "settled"
    assert len(d["events"]) == 3
    assert d["reconciliation"]["settlement_status"] == "settled"
    assert d["reconciliation"]["discrepancy_flag"] is False
    assert d["merchant"]["merchant_id"] == "m_test"

@pytest.mark.asyncio
async def test_idempotency_returns_duplicate_flag(client):
    p = _ev()
    r1 = await _post(client, p)
    r2 = (await client.post("/events", json=p)).json()
    assert not r1["is_duplicate"]
    assert r2["is_duplicate"] and r2["status"] == "duplicate"

@pytest.mark.asyncio
async def test_duplicate_does_not_add_event_row(client):
    tid, p = str(uuid.uuid4()), _ev(tid=str(uuid.uuid4()))
    p["transaction_id"] = tid
    await _post(client, p); await _post(client, p)
    assert len((await client.get(f"/transactions/{tid}")).json()["events"]) == 1

@pytest.mark.asyncio
async def test_duplicate_does_not_regress_status(client):
    tid = str(uuid.uuid4())
    init = _ev("payment_initiated", tid=tid, offset=0)
    proc = _ev("payment_processed", tid=tid, offset=10)
    await _post(client, init); await _post(client, proc); await _post(client, init)
    assert (await client.get(f"/transactions/{tid}")).json()["current_status"] == "processed"

@pytest.mark.asyncio
async def test_failed_sets_not_applicable(client):
    tid = str(uuid.uuid4())
    await _post(client, _ev("payment_initiated", tid=tid, offset=0))
    await _post(client, _ev("payment_failed",    tid=tid, offset=5))
    d = (await client.get(f"/transactions/{tid}")).json()
    assert d["current_status"] == "failed"
    assert d["reconciliation"]["settlement_status"] == "not_applicable"

@pytest.mark.asyncio
async def test_discrepancy_settle_on_fail(client):
    tid, mid = str(uuid.uuid4()), "m_disc"
    await _post(client, _ev("payment_initiated", tid=tid, mid=mid, offset=0))
    await _post(client, _ev("payment_failed",    tid=tid, mid=mid, offset=5))
    await _post(client, _ev("settled",           tid=tid, mid=mid, offset=10))
    recon = (await client.get(f"/transactions/{tid}")).json()["reconciliation"]
    assert recon["discrepancy_flag"] is True
    assert "failed" in recon["discrepancy_reason"].lower()

@pytest.mark.asyncio
async def test_missing_fields_422(client):
    assert (await client.post("/events", json={"event_type":"payment_initiated"})).status_code == 422

@pytest.mark.asyncio
async def test_invalid_event_type_422(client):
    p = _ev(); p["event_type"] = "payment_reversed"
    assert (await client.post("/events", json=p)).status_code == 422

@pytest.mark.asyncio
async def test_zero_amount_422(client):
    p = _ev(); p["amount"] = 0
    assert (await client.post("/events", json=p)).status_code == 422

@pytest.mark.asyncio
async def test_negative_amount_422(client):
    p = _ev(); p["amount"] = -100
    assert (await client.post("/events", json=p)).status_code == 422

# ─── GET /transactions ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_list_basic(client):
    mid = "m_list_" + str(uuid.uuid4())[:8]
    for _ in range(4): await _post(client, _ev(mid=mid))
    r = (await client.get("/transactions", params={"merchant_id": mid})).json()
    assert r["total"] == 4 and len(r["items"]) == 4

@pytest.mark.asyncio
async def test_list_filter_status(client):
    mid = "m_st_" + str(uuid.uuid4())[:8]; tid = str(uuid.uuid4())
    await _post(client, _ev("payment_initiated", tid=tid, mid=mid))
    await _post(client, _ev("payment_failed",    tid=tid, mid=mid))
    r = (await client.get("/transactions", params={"merchant_id":mid,"status":"failed"})).json()
    assert r["total"] == 1 and r["items"][0]["transaction_id"] == tid

@pytest.mark.asyncio
async def test_list_date_range(client):
    mid = "m_dr_" + str(uuid.uuid4())[:8]
    await _post(client, _ev(mid=mid))
    r = (await client.get("/transactions", params={"merchant_id":mid,
         "date_from":"2026-01-01T00:00:00+00:00","date_to":"2026-12-31T23:59:59+00:00"})).json()
    assert r["total"] >= 1

@pytest.mark.asyncio
async def test_list_pagination(client):
    mid = "m_pg_" + str(uuid.uuid4())[:8]
    for _ in range(5): await _post(client, _ev(mid=mid))
    r1 = (await client.get("/transactions", params={"merchant_id":mid,"page":1,"page_size":2})).json()
    r2 = (await client.get("/transactions", params={"merchant_id":mid,"page":2,"page_size":2})).json()
    r3 = (await client.get("/transactions", params={"merchant_id":mid,"page":3,"page_size":2})).json()
    assert len(r1["items"])==2 and len(r2["items"])==2 and len(r3["items"])==1

@pytest.mark.asyncio
async def test_list_sort_amount(client):
    mid = "m_so_" + str(uuid.uuid4())[:8]
    for a in [300,100,200]: await _post(client, _ev(mid=mid, amount=a))
    items = (await client.get("/transactions",
             params={"merchant_id":mid,"sort_by":"amount","sort_order":"asc"})).json()["items"]
    amounts = [i["amount"] for i in items]
    assert amounts == sorted(amounts)

@pytest.mark.asyncio
async def test_list_invalid_status_422(client):
    assert (await client.get("/transactions", params={"status":"refunded"})).status_code == 422

@pytest.mark.asyncio
async def test_list_invalid_sort_422(client):
    assert (await client.get("/transactions", params={"sort_by":"bad"})).status_code == 422

@pytest.mark.asyncio
async def test_list_date_inversion_422(client):
    r = await client.get("/transactions", params={
        "date_from":"2026-12-01T00:00:00+00:00","date_to":"2026-01-01T00:00:00+00:00"})
    assert r.status_code == 422

# ─── GET /transactions/{id} ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_detail_events_ordered(client):
    tid = str(uuid.uuid4())
    for i,et in enumerate(["payment_initiated","payment_processed","settled"]):
        await _post(client, _ev(et, tid=tid, offset=i*30))
    d = (await client.get(f"/transactions/{tid}")).json()
    assert [e["event_type"] for e in d["events"]] == ["payment_initiated","payment_processed","settled"]
    assert d["merchant"] is not None and d["reconciliation"] is not None

@pytest.mark.asyncio
async def test_detail_not_found(client):
    assert (await client.get(f"/transactions/{uuid.uuid4()}")).status_code == 404

# ─── GET /reconciliation/summary ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_summary_merchant(client):
    r = (await client.get("/reconciliation/summary", params={"group_by":"merchant"})).json()
    assert r["group_by"] == "merchant" and isinstance(r["items"], list)

@pytest.mark.asyncio
async def test_summary_date(client):
    r = (await client.get("/reconciliation/summary", params={"group_by":"date"})).json()
    assert r["group_by"] == "date"
    if r["items"]: assert r["items"][0]["date"] is not None

@pytest.mark.asyncio
async def test_summary_status(client):
    assert (await client.get("/reconciliation/summary", params={"group_by":"status"})).json()["group_by"] == "status"

@pytest.mark.asyncio
async def test_summary_merchant_filter(client):
    mid = "m_smf_" + str(uuid.uuid4())[:8]
    await _post(client, _ev(mid=mid))
    r = (await client.get("/reconciliation/summary",
         params={"group_by":"merchant","merchant_id":mid})).json()
    for item in r["items"]: assert item["merchant_id"] == mid

@pytest.mark.asyncio
async def test_summary_invalid_group_by_422(client):
    assert (await client.get("/reconciliation/summary", params={"group_by":"region"})).status_code == 422

# ─── GET /reconciliation/discrepancies ───────────────────────────────────────
@pytest.mark.asyncio
async def test_discrepancies_settle_on_fail(client):
    mid, tid = "m_d2_" + str(uuid.uuid4())[:8], str(uuid.uuid4())
    await _post(client, _ev("payment_initiated", tid=tid, mid=mid, offset=0))
    await _post(client, _ev("payment_failed",    tid=tid, mid=mid, offset=5))
    await _post(client, _ev("settled",           tid=tid, mid=mid, offset=10))
    data = (await client.get("/reconciliation/discrepancies")).json()
    assert tid in [i["transaction_id"] for i in data["items"]]

@pytest.mark.asyncio
async def test_discrepancies_stuck_processed(client):
    mid, tid = "m_stk_" + str(uuid.uuid4())[:8], str(uuid.uuid4())
    await _post(client, _ev("payment_initiated", tid=tid, mid=mid, offset=0))
    await _post(client, _ev("payment_processed", tid=tid, mid=mid, offset=5))
    data = (await client.get("/reconciliation/discrepancies",
             params={"merchant_id":mid})).json()
    assert data["total"] >= 1 and tid in [i["transaction_id"] for i in data["items"]]

@pytest.mark.asyncio
async def test_discrepancies_unknown_merchant_empty(client):
    r = (await client.get("/reconciliation/discrepancies",
         params={"merchant_id":"nonexistent_xyz"})).json()
    assert r["total"] == 0 and r["items"] == []

@pytest.mark.asyncio
async def test_discrepancies_pagination(client):
    r = (await client.get("/reconciliation/discrepancies",
         params={"page":1,"page_size":5})).json()
    assert len(r["items"]) <= 5 and r["page"] == 1

# ─── Health ───────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_health(client):
    assert (await client.get("/health")).json()["status"] == "healthy"

@pytest.mark.asyncio
async def test_root(client):
    assert "service" in (await client.get("/")).json()
'''

FILES["pytest.ini"] = '''[pytest]
asyncio_mode = auto
asyncio_default_fixture_loop_scope = session
testpaths = setu_app
python_files = test_*.py
'''

# ---------------------------------------------------------------------------
# Bootstrap helpers
# ---------------------------------------------------------------------------

def _banner(msg: str):
    print(f"\n{'='*60}\n  {msg}\n{'='*60}")


def _run(cmd: list[str], **kwargs):
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        sys.exit(result.returncode)
    return result


def _python_in_venv() -> str:
    if sys.platform == "win32":
        return str(VENV_DIR / "Scripts" / "python.exe")
    return str(VENV_DIR / "bin" / "python")


def _pip_in_venv() -> str:
    if sys.platform == "win32":
        return str(VENV_DIR / "Scripts" / "pip.exe")
    return str(VENV_DIR / "bin" / "pip")


def step_venv():
    _banner("Step 1 / 4 — Virtual environment")
    if not VENV_DIR.exists():
        print("Creating .venv …")
        venv.create(str(VENV_DIR), with_pip=True)
        print("  ✓ .venv created")
    else:
        print("  ✓ .venv already exists — skipping")


def step_install():
    _banner("Step 2 / 4 — Installing dependencies")
    _run([_pip_in_venv(), "install", "--quiet", "--upgrade", "pip"])
    _run([_pip_in_venv(), "install", "--quiet", "--upgrade"] + REQUIREMENTS)
    print("  ✓ All packages installed")


def step_write_sources():
    _banner("Step 3 / 4 — Writing application source files")
    for rel_path, content in FILES.items():
        dest = ROOT / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(textwrap.dedent(content).lstrip("\n"))
    print(f"  ✓ {len(FILES)} files written to {APP_DIR}/")


def step_seed():
    _banner("Step 4 / 4 — Seeding database")
    if not SAMPLE_EVENTS.exists():
        print(f"  ✗  sample_events.json not found at {SAMPLE_EVENTS}")
        print("     Place sample_events.json in the same folder as run.py and try again.")
        sys.exit(1)

    _run(
        [_python_in_venv(), "-c",
         f"import asyncio, sys; sys.path.insert(0, '{ROOT}'); "
         f"from setu_app.seed import seed; asyncio.run(seed('{SAMPLE_EVENTS}'))"],
        cwd=ROOT,
    )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run_tests():
    step_venv()
    step_install()
    step_write_sources()
    step_seed()
    _banner("Running test suite (32 tests)")
    _run(
        [_python_in_venv(), "-m", "pytest", "setu_app/test_api.py", "-v", "--tb=short"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


def run_server(port: int = 8000):
    step_venv()
    step_install()
    step_write_sources()
    step_seed()

    _banner(f"Starting server on http://localhost:{port}")
    print(f"  Swagger UI  →  http://localhost:{port}/docs")
    print(f"  ReDoc       →  http://localhost:{port}/redoc")
    print(f"  Press Ctrl+C to stop\n")

    os.execv(
        _python_in_venv(),
        [_python_in_venv(), "-m", "uvicorn", "setu_app.main:app",
         "--host", "0.0.0.0", "--port", str(port), "--reload"],
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Setu Payment Lifecycle Service bootstrap")
    parser.add_argument("--test",  action="store_true", help="Run test suite and exit")
    parser.add_argument("--port",  type=int, default=8000, help="Server port (default: 8000)")
    args = parser.parse_args()

    if args.test:
        run_tests()
    else:
        run_server(port=args.port)
