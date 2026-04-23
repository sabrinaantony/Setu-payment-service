"""
Transaction query service.

All filtering, pagination, and sorting is pushed into SQL.
A separate COUNT(*) query determines the total before pagination so
we never pull rows into Python just to count them.
"""

from datetime import datetime
from typing import Literal, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.models import Transaction
from app.schemas.schemas import PaginatedTransactions, TransactionDetailOut, TransactionOut

_SORTABLE = {
    "created_at": Transaction.created_at,
    "updated_at": Transaction.updated_at,
    "amount": Transaction.amount,
}


async def list_transactions(
    db: AsyncSession,
    merchant_id: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
    page: int = 1,
    page_size: int = 20,
) -> PaginatedTransactions:
    query = select(Transaction)

    # --- Filters (all in SQL) ---
    if merchant_id:
        query = query.where(Transaction.merchant_id == merchant_id)
    if status:
        query = query.where(Transaction.current_status == status)
    if date_from:
        query = query.where(Transaction.created_at >= date_from)
    if date_to:
        query = query.where(Transaction.created_at <= date_to)

    # --- Count before pagination ---
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0

    # --- Sort ---
    sort_col = _SORTABLE[sort_by]
    sort_col = sort_col.desc() if sort_order == "desc" else sort_col.asc()
    query = query.order_by(sort_col)

    # --- Pagination ---
    query = query.offset((page - 1) * page_size).limit(page_size)

    rows = (await db.execute(query)).scalars().all()
    return PaginatedTransactions(
        total=total,
        page=page,
        page_size=page_size,
        items=[TransactionOut.model_validate(t) for t in rows],
    )


async def get_transaction_detail(
    db: AsyncSession,
    transaction_id: str,
) -> Optional[TransactionDetailOut]:
    """
    Fetch full transaction detail with merchant, ordered event history,
    and reconciliation record — all in a single round-trip via eager loading.
    """
    query = (
        select(Transaction)
        .options(
            selectinload(Transaction.merchant),
            selectinload(Transaction.events),
            selectinload(Transaction.reconciliation),
        )
        .where(Transaction.transaction_id == transaction_id)
    )
    txn = (await db.execute(query)).scalar_one_or_none()
    if txn is None:
        return None
    return TransactionDetailOut.model_validate(txn)
