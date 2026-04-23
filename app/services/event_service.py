"""
Event ingestion service.

Idempotency
-----------
payment_events.event_id has a DB-level UNIQUE constraint.
We attempt INSERT, catch IntegrityError on duplicate, rollback the
savepoint, and return is_duplicate=True.  Because the constraint is
enforced at the database level this is safe under concurrent requests.

State machine
-------------
Status advancement is only allowed forward (by rank):
  initiated(0) → processed(1) | failed(1) → settled(2)

Out-of-order or duplicate events that arrive at the same or lower rank
do not overwrite current_status.  The event is still stored (history),
but the transaction state is unchanged.

Discrepancy detection
---------------------
Written at ingest time into reconciliation_records.discrepancy_flag so
GET /reconciliation/discrepancies is a cheap indexed WHERE, not a scan.

Cases flagged:
  • settled event received for a payment_failed transaction
  • payment_failed event after settlement already recorded
  • payment_processed event after transaction already settled
"""

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    Merchant,
    PaymentEvent,
    ReconciliationRecord,
    SettlementStatus,
    Transaction,
    TransactionStatus,
)
from app.schemas.schemas import EventIngestionRequest, EventIngestionResponse

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STATUS_RANK: dict[str, int] = {
    TransactionStatus.initiated.value: 0,
    TransactionStatus.processed.value: 1,
    TransactionStatus.failed.value: 1,
    TransactionStatus.settled.value: 2,
}

_EVENT_TO_STATUS: dict[str, str] = {
    "payment_initiated": TransactionStatus.initiated.value,
    "payment_processed": TransactionStatus.processed.value,
    "payment_failed": TransactionStatus.failed.value,
    "settled": TransactionStatus.settled.value,
}


def _should_advance(current: str, incoming: str) -> bool:
    return _STATUS_RANK.get(incoming, -1) > _STATUS_RANK.get(current, -1)


# ---------------------------------------------------------------------------
# Main service function
# ---------------------------------------------------------------------------


async def ingest_event(
    db: AsyncSession,
    payload: EventIngestionRequest,
) -> EventIngestionResponse:
    new_status = _EVENT_TO_STATUS.get(payload.event_type, payload.event_type)

    # 1. Upsert merchant (name may change between events — always refresh)
    merchant = await db.get(Merchant, payload.merchant_id)
    if merchant is None:
        merchant = Merchant(
            merchant_id=payload.merchant_id,
            merchant_name=payload.merchant_name,
        )
        db.add(merchant)
    else:
        merchant.merchant_name = payload.merchant_name

    # 2. Upsert transaction (advance status only forward)
    transaction = await db.get(Transaction, payload.transaction_id)
    if transaction is None:
        transaction = Transaction(
            transaction_id=payload.transaction_id,
            merchant_id=payload.merchant_id,
            amount=float(payload.amount),
            currency=payload.currency,
            current_status=new_status,
        )
        db.add(transaction)
        previous_status: str | None = None
    else:
        previous_status = transaction.current_status
        if _should_advance(transaction.current_status, new_status):
            transaction.current_status = new_status

    # 3. Insert event — UNIQUE(event_id) is the idempotency guard
    event = PaymentEvent(
        event_id=payload.event_id,
        event_type=payload.event_type,
        transaction_id=payload.transaction_id,
        merchant_id=payload.merchant_id,
        amount=float(payload.amount),
        currency=payload.currency,
        timestamp=payload.timestamp,
        raw_payload=json.dumps(payload.model_dump(mode="json")),
    )
    db.add(event)

    try:
        await db.flush()
    except IntegrityError:
        # Duplicate event_id — roll back and return without any side effects
        await db.rollback()
        return EventIngestionResponse(
            status="duplicate",
            message="Event already processed; no state changes applied.",
            event_id=payload.event_id,
            transaction_id=payload.transaction_id,
            is_duplicate=True,
        )

    # 4. Upsert reconciliation record and detect discrepancies
    recon: ReconciliationRecord | None = await db.scalar(
        select(ReconciliationRecord).where(
            ReconciliationRecord.transaction_id == payload.transaction_id
        )
    )

    discrepancy_flag = False
    discrepancy_reason: str | None = None

    if recon is None:
        settlement_status = (
            SettlementStatus.not_applicable.value
            if new_status == TransactionStatus.failed.value
            else SettlementStatus.pending.value
        )
        recon = ReconciliationRecord(
            transaction_id=payload.transaction_id,
            payment_status=new_status,
            settlement_status=settlement_status,
        )
        db.add(recon)
    else:
        # Sync payment_status snapshot to final transaction status
        recon.payment_status = transaction.current_status

        if payload.event_type == "settled":
            # Use the status *before* this event was applied to detect settle-on-fail
            status_before_this_event = previous_status or recon.payment_status
            if status_before_this_event == TransactionStatus.failed.value:
                discrepancy_flag = True
                discrepancy_reason = "Settlement received for a failed payment."
            else:
                recon.settlement_status = SettlementStatus.settled.value
                recon.settled_at = datetime.now(timezone.utc)

        elif payload.event_type == "payment_failed":
            if recon.settlement_status == SettlementStatus.settled.value:
                discrepancy_flag = True
                discrepancy_reason = "Payment marked failed after settlement was already recorded."
            else:
                recon.settlement_status = SettlementStatus.not_applicable.value

        elif payload.event_type == "payment_processed":
            if recon.payment_status == TransactionStatus.settled.value:
                discrepancy_flag = True
                discrepancy_reason = (
                    "payment_processed event received after transaction was already settled."
                )

        recon.discrepancy_flag = discrepancy_flag
        recon.discrepancy_reason = discrepancy_reason

    await db.flush()

    return EventIngestionResponse(
        status="accepted",
        message="Event ingested successfully.",
        event_id=payload.event_id,
        transaction_id=payload.transaction_id,
        is_duplicate=False,
    )
