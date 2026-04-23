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
