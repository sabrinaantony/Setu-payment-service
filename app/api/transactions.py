from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.schemas import PaginatedTransactions, TransactionDetailOut
from app.services.transaction_service import get_transaction_detail, list_transactions

router = APIRouter()

_VALID_STATUSES = {"initiated", "processed", "failed", "settled"}
_VALID_SORT = {"created_at", "updated_at", "amount"}


@router.get(
    "",
    response_model=PaginatedTransactions,
    summary="List transactions",
    description=(
        "Paginated list with optional filters on `merchant_id`, `status`, and date range. "
        "Sort by `created_at` (default), `updated_at`, or `amount`."
    ),
)
async def list_txns(
    merchant_id: Optional[str] = Query(None, description="Filter by merchant ID"),
    status: Optional[str] = Query(
        None, description="Filter by status: initiated | processed | failed | settled"
    ),
    date_from: Optional[datetime] = Query(None, description="Lower bound on created_at (ISO 8601)"),
    date_to: Optional[datetime] = Query(None, description="Upper bound on created_at (ISO 8601)"),
    sort_by: str = Query("created_at", description="Sort field: created_at | updated_at | amount"),
    sort_order: str = Query("desc", description="Sort direction: asc | desc"),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(20, ge=1, le=200, description="Items per page (max 200)"),
    db: AsyncSession = Depends(get_db),
) -> PaginatedTransactions:
    if status and status not in _VALID_STATUSES:
        raise HTTPException(422, f"Invalid status '{status}'. Valid: {sorted(_VALID_STATUSES)}")
    if sort_by not in _VALID_SORT:
        raise HTTPException(422, f"Invalid sort_by '{sort_by}'. Valid: {sorted(_VALID_SORT)}")
    if sort_order not in {"asc", "desc"}:
        raise HTTPException(422, "sort_order must be 'asc' or 'desc'")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be earlier than date_to")

    return await list_transactions(
        db,
        merchant_id=merchant_id,
        status=status,
        date_from=date_from,
        date_to=date_to,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{transaction_id}",
    response_model=TransactionDetailOut,
    summary="Fetch transaction details",
    description=(
        "Returns full transaction info including merchant, complete event history "
        "(ordered by timestamp), and reconciliation status."
    ),
    responses={404: {"description": "Transaction not found"}},
)
async def get_txn(
    transaction_id: str,
    db: AsyncSession = Depends(get_db),
) -> TransactionDetailOut:
    result = await get_transaction_detail(db, transaction_id)
    if result is None:
        raise HTTPException(404, f"Transaction '{transaction_id}' not found.")
    return result
