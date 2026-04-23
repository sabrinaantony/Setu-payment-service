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
