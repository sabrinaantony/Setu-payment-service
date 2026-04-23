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
        raise HTTPException(404, f"Transaction '{transaction_id}' not found.")
    return result
