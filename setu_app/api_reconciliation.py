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
        raise HTTPException(422, f"Invalid group_by '{group_by}'. Valid: merchant, date, status")
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
