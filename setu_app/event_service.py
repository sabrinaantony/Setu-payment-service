"""
Event ingestion service.

Idempotency  — UNIQUE(event_id) at DB level; IntegrityError → no-op.
State machine — status only advances by rank (initiated<processed/failed<settled).
Discrepancy  — flag written at ingest so discrepancy query is a cheap WHERE.
"""
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from setu_app.models import (Merchant, PaymentEvent, ReconciliationRecord,
                              SettlementStatus, Transaction, TransactionStatus)
from setu_app.schemas import EventIngestionRequest, EventIngestionResponse

_RANK = {
    TransactionStatus.initiated.value: 0,
    TransactionStatus.processed.value: 1,
    TransactionStatus.failed.value:    1,
    TransactionStatus.settled.value:   2,
}
_EV_TO_STATUS = {
    "payment_initiated": TransactionStatus.initiated.value,
    "payment_processed": TransactionStatus.processed.value,
    "payment_failed":    TransactionStatus.failed.value,
    "settled":           TransactionStatus.settled.value,
}


async def ingest_event(db: AsyncSession, p: EventIngestionRequest) -> EventIngestionResponse:
    new_status = _EV_TO_STATUS.get(p.event_type, p.event_type)

    # 1. Upsert merchant
    merchant = await db.get(Merchant, p.merchant_id)
    if merchant is None:
        db.add(Merchant(merchant_id=p.merchant_id, merchant_name=p.merchant_name))
    else:
        merchant.merchant_name = p.merchant_name

    # 2. Upsert transaction — capture status BEFORE advancing
    transaction = await db.get(Transaction, p.transaction_id)
    if transaction is None:
        transaction = Transaction(
            transaction_id=p.transaction_id, merchant_id=p.merchant_id,
            amount=float(p.amount), currency=p.currency, current_status=new_status,
        )
        db.add(transaction)
        previous_status = None
    else:
        previous_status = transaction.current_status
        if _RANK.get(new_status, -1) > _RANK.get(transaction.current_status, -1):
            transaction.current_status = new_status

    # 3. Insert event — UNIQUE(event_id) is the idempotency guard
    db.add(PaymentEvent(
        event_id=p.event_id, event_type=p.event_type,
        transaction_id=p.transaction_id, merchant_id=p.merchant_id,
        amount=float(p.amount), currency=p.currency, timestamp=p.timestamp,
        raw_payload=json.dumps(p.model_dump(mode="json")),
    ))

    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return EventIngestionResponse(
            status="duplicate",
            message="Event already processed; no state changes applied.",
            event_id=p.event_id, transaction_id=p.transaction_id, is_duplicate=True,
        )

    # 4. Upsert reconciliation + detect discrepancies
    recon = await db.scalar(
        select(ReconciliationRecord).where(ReconciliationRecord.transaction_id == p.transaction_id)
    )
    disc_flag, disc_reason = False, None

    if recon is None:
        s_status = (SettlementStatus.not_applicable.value
                    if new_status == TransactionStatus.failed.value
                    else SettlementStatus.pending.value)
        db.add(ReconciliationRecord(
            transaction_id=p.transaction_id,
            payment_status=new_status, settlement_status=s_status,
        ))
    else:
        recon.payment_status = transaction.current_status

        if p.event_type == "settled":
            status_before = previous_status or recon.payment_status
            if status_before == TransactionStatus.failed.value:
                disc_flag, disc_reason = True, "Settlement received for a failed payment."
            else:
                recon.settlement_status = SettlementStatus.settled.value
                recon.settled_at = datetime.now(timezone.utc)

        elif p.event_type == "payment_failed":
            if recon.settlement_status == SettlementStatus.settled.value:
                disc_flag = True
                disc_reason = "Payment marked failed after settlement was already recorded."
            else:
                recon.settlement_status = SettlementStatus.not_applicable.value

        elif p.event_type == "payment_processed":
            if recon.payment_status == TransactionStatus.settled.value:
                disc_flag = True
                disc_reason = "payment_processed received after transaction already settled."

        recon.discrepancy_flag, recon.discrepancy_reason = disc_flag, disc_reason

    await db.flush()
    return EventIngestionResponse(
        status="accepted", message="Event ingested successfully.",
        event_id=p.event_id, transaction_id=p.transaction_id, is_duplicate=False,
    )
