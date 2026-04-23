from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.schemas import EventIngestionRequest, EventIngestionResponse
from app.services.event_service import ingest_event

router = APIRouter()


@router.post(
    "",
    response_model=EventIngestionResponse,
    status_code=status.HTTP_200_OK,
    summary="Ingest a payment lifecycle event",
    description=(
        "Idempotent endpoint: submitting the same `event_id` twice returns "
        "`is_duplicate: true` with no state changes. "
        "Accepted `event_type` values: `payment_initiated`, `payment_processed`, "
        "`payment_failed`, `settled`."
    ),
)
async def post_event(
    payload: EventIngestionRequest,
    db: AsyncSession = Depends(get_db),
) -> EventIngestionResponse:
    return await ingest_event(db, payload)
