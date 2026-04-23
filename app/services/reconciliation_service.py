"""
Reconciliation service.

Summary
-------
Pure SQL GROUP BY across three dimensions (merchant / date / status).
func.date() works on both SQLite and PostgreSQL — SQLite returns a
string, PostgreSQL returns a date; both serialise fine to YYYY-MM-DD.

Discrepancies
-------------
Primary source: reconciliation_records.discrepancy_flag = TRUE
  (written at ingest time, O(1) per event, indexed).

Additional live checks (catches edge cases the flag may have missed):
  • payment_status = 'processed' AND settlement_status = 'pending'
    → processed but never settled (stuck flow)
  • payment_status = 'failed'  AND settlement_status = 'settled'
    → settled after failure (data integrity issue)

These two extra OR arms are cheap because both columns are indexed.
"""

from typing import Literal, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    Merchant,
    ReconciliationRecord,
    SettlementStatus,
    Transaction,
    TransactionStatus,
)
from app.schemas.schemas import (
    DiscrepancyItem,
    DiscrepancyResponse,
    ReconciliationSummaryItem,
    ReconciliationSummaryResponse,
)


async def get_reconciliation_summary(
    db: AsyncSession,
    group_by: Literal["merchant", "date", "status"] = "merchant",
    merchant_id: Optional[str] = None,
) -> ReconciliationSummaryResponse:

    base = (
        select(
            Transaction.merchant_id,
            Merchant.merchant_name,
            Transaction.current_status,
            Transaction.amount,
            Transaction.created_at,
        )
        .join(Merchant, Merchant.merchant_id == Transaction.merchant_id)
    )
    if merchant_id:
        base = base.where(Transaction.merchant_id == merchant_id)

    sub = base.subquery()

    if group_by == "merchant":
        agg = (
            select(
                sub.c.merchant_id,
                sub.c.merchant_name,
                sub.c.current_status.label("status"),
                func.count().label("n"),
                func.sum(sub.c.amount).label("total"),
            )
            .group_by(sub.c.merchant_id, sub.c.merchant_name, sub.c.current_status)
            .order_by(sub.c.merchant_id, sub.c.current_status)
        )
        rows = (await db.execute(agg)).all()
        items = [
            ReconciliationSummaryItem(
                merchant_id=r.merchant_id,
                merchant_name=r.merchant_name,
                status=r.status,
                transaction_count=r.n,
                total_amount=float(r.total or 0),
            )
            for r in rows
        ]

    elif group_by == "date":
        date_expr = func.date(sub.c.created_at)
        agg = (
            select(
                sub.c.merchant_id,
                sub.c.merchant_name,
                date_expr.label("dt"),
                sub.c.current_status.label("status"),
                func.count().label("n"),
                func.sum(sub.c.amount).label("total"),
            )
            .group_by(sub.c.merchant_id, sub.c.merchant_name, date_expr, sub.c.current_status)
            .order_by(date_expr.desc(), sub.c.merchant_id)
        )
        rows = (await db.execute(agg)).all()
        items = [
            ReconciliationSummaryItem(
                merchant_id=r.merchant_id,
                merchant_name=r.merchant_name,
                date=str(r.dt),
                status=r.status,
                transaction_count=r.n,
                total_amount=float(r.total or 0),
            )
            for r in rows
        ]

    else:  # status
        agg = (
            select(
                sub.c.current_status.label("status"),
                sub.c.merchant_id,
                sub.c.merchant_name,
                func.count().label("n"),
                func.sum(sub.c.amount).label("total"),
            )
            .group_by(sub.c.current_status, sub.c.merchant_id, sub.c.merchant_name)
            .order_by(sub.c.current_status, sub.c.merchant_id)
        )
        rows = (await db.execute(agg)).all()
        items = [
            ReconciliationSummaryItem(
                merchant_id=r.merchant_id,
                merchant_name=r.merchant_name,
                status=r.status,
                transaction_count=r.n,
                total_amount=float(r.total or 0),
            )
            for r in rows
        ]

    return ReconciliationSummaryResponse(
        group_by=group_by,
        total_transactions=sum(i.transaction_count for i in items),
        items=items,
    )


async def get_discrepancies(
    db: AsyncSession,
    merchant_id: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
) -> DiscrepancyResponse:

    query = (
        select(
            Transaction.transaction_id,
            Transaction.merchant_id,
            Transaction.amount,
            Transaction.currency,
            ReconciliationRecord.payment_status,
            ReconciliationRecord.settlement_status,
            ReconciliationRecord.discrepancy_reason,
            Transaction.created_at,
        )
        .join(ReconciliationRecord, ReconciliationRecord.transaction_id == Transaction.transaction_id)
        .where(
            or_(
                # Flagged at ingest time
                ReconciliationRecord.discrepancy_flag == True,  # noqa: E712
                # Processed but never settled (stuck)
                (
                    (ReconciliationRecord.payment_status == TransactionStatus.processed.value)
                    & (ReconciliationRecord.settlement_status == SettlementStatus.pending.value)
                ),
                # Failed but settlement recorded
                (
                    (ReconciliationRecord.payment_status == TransactionStatus.failed.value)
                    & (ReconciliationRecord.settlement_status == SettlementStatus.settled.value)
                ),
            )
        )
        .order_by(Transaction.created_at.desc())
    )

    if merchant_id:
        query = query.where(Transaction.merchant_id == merchant_id)

    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    query = query.offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(query)).all()

    items = [
        DiscrepancyItem(
            transaction_id=r.transaction_id,
            merchant_id=r.merchant_id,
            amount=float(r.amount),
            currency=r.currency,
            payment_status=r.payment_status,
            settlement_status=r.settlement_status,
            discrepancy_reason=r.discrepancy_reason or _infer_reason(r.payment_status, r.settlement_status),
            created_at=r.created_at,
        )
        for r in rows
    ]
    return DiscrepancyResponse(total=total, page=page, page_size=page_size, items=items)


def _infer_reason(payment_status: str, settlement_status: str) -> str:
    if payment_status == TransactionStatus.processed.value and settlement_status == SettlementStatus.pending.value:
        return "Payment processed but settlement is still pending."
    if payment_status == TransactionStatus.failed.value and settlement_status == SettlementStatus.settled.value:
        return "Payment failed but settlement was recorded."
    return "State inconsistency detected."
