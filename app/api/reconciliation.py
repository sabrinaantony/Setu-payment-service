from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.schemas import DiscrepancyResponse, ReconciliationSummaryResponse
from app.services.reconciliation_service import get_discrepancies, get_reconciliation_summary

router = APIRouter()


@router.get(
    "/summary",
    response_model=ReconciliationSummaryResponse,
    summary="Reconciliation summary",
    description=(
        "Aggregated transaction counts and totals grouped by `merchant` (default), "
        "`date`, or `status`. Optionally filter to one merchant with `merchant_id`."
    ),
)
async def summary(
    group_by: str = Query("merchant", description="merchant | date | status"),
    merchant_id: Optional[str] = Query(None, description="Filter to a specific merchant"),
    db: AsyncSession = Depends(get_db),
) -> ReconciliationSummaryResponse:
    if group_by not in {"merchant", "date", "status"}:
        raise HTTPException(422, f"Invalid group_by '{group_by}'. Valid: merchant, date, status")
    return await get_reconciliation_summary(db, group_by=group_by, merchant_id=merchant_id)


@router.get(
    "/discrepancies",
    response_model=DiscrepancyResponse,
    summary="Reconciliation discrepancies",
    description=(
        "Transactions where payment state and settlement state are inconsistent. "
        "Includes: processed-but-never-settled, settled-after-failure, and "
        "any state conflict flagged during ingestion."
    ),
)
async def discrepancies(
    merchant_id: Optional[str] = Query(None, description="Filter to a specific merchant"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> DiscrepancyResponse:
    return await get_discrepancies(db, merchant_id=merchant_id, page=page, page_size=page_size)
