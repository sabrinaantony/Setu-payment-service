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
