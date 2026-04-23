"""
Seed the database from sample_events.json.

Strategy
--------
Pre-compute final state for every merchant, transaction, and
reconciliation record in Python (one pass over the sorted event list),
then bulk-upsert with ON CONFLICT DO UPDATE/NOTHING in batches of 500.

This is much faster than row-by-row ingest for the initial data load:
  ~10 k events seed in < 3 s on SQLite.

Idempotency
-----------
Running this script a second time is safe — all INSERT statements use
ON CONFLICT DO UPDATE so existing rows are refreshed to their correct
final state rather than raising an error.

Usage
-----
    python scripts/seed_db.py                       # default: sample_events.json
    python scripts/seed_db.py /path/to/events.json  # custom file
"""

import asyncio
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Ensure the project root is on sys.path when run directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db.session import AsyncSessionLocal, engine
from app.models.base import Base
from app.models.models import (
    Merchant,
    PaymentEvent,
    ReconciliationRecord,
    SettlementStatus,
    Transaction,
    TransactionStatus,
)

BATCH_SIZE = 500


def _parse_ts(ts_str: str) -> datetime:
    """Parse ISO 8601 string → timezone-aware datetime (SQLite needs real objects)."""
    return datetime.fromisoformat(ts_str)

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


# ---------------------------------------------------------------------------
# State pre-computation
# ---------------------------------------------------------------------------


def compute_final_states(
    events: list[dict],
) -> tuple[dict, dict, dict]:
    """
    Single-pass over chronologically sorted events to compute:
      merchants      {merchant_id: row_dict}
      transactions   {transaction_id: row_dict}
      recon          {transaction_id: row_dict}
    """
    merchants: dict[str, dict] = {}
    transactions: dict[str, dict] = {}
    recon: dict[str, dict] = {}

    # Sort ascending by timestamp so state advances in the right direction
    sorted_events = sorted(events, key=lambda e: e["timestamp"])

    for ev in sorted_events:
        mid = ev["merchant_id"]
        tid = ev["transaction_id"]
        etype = ev["event_type"]
        new_status = _EVENT_TO_STATUS.get(etype, etype)

        # ---- merchant ----
        merchants[mid] = {
            "merchant_id": mid,
            "merchant_name": ev["merchant_name"],
        }

        # ---- transaction ----
        if tid not in transactions:
            transactions[tid] = {
                "transaction_id": tid,
                "merchant_id": mid,
                "amount": float(ev["amount"]),
                "currency": ev.get("currency", "INR"),
                "current_status": new_status,
            }
        else:
            cur = transactions[tid]["current_status"]
            if _STATUS_RANK.get(new_status, -1) > _STATUS_RANK.get(cur, -1):
                transactions[tid]["current_status"] = new_status

        # ---- reconciliation ----
        if tid not in recon:
            settlement_status = (
                SettlementStatus.not_applicable.value
                if new_status == TransactionStatus.failed.value
                else SettlementStatus.pending.value
            )
            recon[tid] = {
                "transaction_id": tid,
                "payment_status": new_status,
                "settlement_status": settlement_status,
                "discrepancy_flag": False,
                "discrepancy_reason": None,
                "settled_at": None,
            }
        else:
            r = recon[tid]
            r["payment_status"] = transactions[tid]["current_status"]

            if etype == "settled":
                if r["payment_status"] == TransactionStatus.failed.value:
                    r["discrepancy_flag"] = True
                    r["discrepancy_reason"] = "Settlement received for a failed payment."
                else:
                    r["settlement_status"] = SettlementStatus.settled.value
                    r["settled_at"] = _parse_ts(ev["timestamp"])

            elif etype == "payment_failed":
                if r["settlement_status"] == SettlementStatus.settled.value:
                    r["discrepancy_flag"] = True
                    r["discrepancy_reason"] = (
                        "Payment marked failed after settlement was already recorded."
                    )
                else:
                    r["settlement_status"] = SettlementStatus.not_applicable.value

            elif etype == "payment_processed":
                if r["payment_status"] == TransactionStatus.settled.value:
                    r["discrepancy_flag"] = True
                    r["discrepancy_reason"] = (
                        "payment_processed event received after transaction was already settled."
                    )

    # Final pass: flag "stuck" transactions (processed but settlement still pending)
    for tid, r in recon.items():
        if (
            not r["discrepancy_flag"]
            and transactions[tid]["current_status"] == TransactionStatus.processed.value
            and r["settlement_status"] == SettlementStatus.pending.value
        ):
            r["discrepancy_flag"] = True
            r["discrepancy_reason"] = "Payment processed but settlement is still pending."

    return merchants, transactions, recon


# ---------------------------------------------------------------------------
# Bulk insert helpers
# ---------------------------------------------------------------------------


async def _bulk_upsert(db, model, rows: list[dict], conflict_col: str, update_cols: list[str]):
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        stmt = sqlite_insert(model).values(batch)
        update_dict = {col: getattr(stmt.excluded, col) for col in update_cols}
        stmt = stmt.on_conflict_do_update(
            index_elements=[conflict_col],
            set_=update_dict,
        )
        await db.execute(stmt)
    await db.commit()


async def _bulk_insert_events(db, rows: list[dict]):
    """Insert events with ON CONFLICT DO NOTHING (idempotency)."""
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        stmt = sqlite_insert(PaymentEvent).values(batch)
        stmt = stmt.on_conflict_do_nothing(index_elements=["event_id"])
        await db.execute(stmt)
    await db.commit()


# ---------------------------------------------------------------------------
# Main seed function
# ---------------------------------------------------------------------------


async def seed(filepath: str):
    print(f"\nLoading events from {filepath} …")
    with open(filepath) as f:
        events = json.load(f)

    total_events = len(events)
    unique_event_ids = len({e["event_id"] for e in events})
    duplicates = total_events - unique_event_ids
    print(f"  {total_events} events  |  {unique_event_ids} unique  |  {duplicates} duplicates")

    # Ensure tables exist
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("  Tables created / verified.")

    # Pre-compute
    t0 = time.perf_counter()
    merchants, transactions, recon = compute_final_states(events)
    t1 = time.perf_counter()
    print(
        f"  State pre-computed in {t1 - t0:.2f}s — "
        f"{len(merchants)} merchants, {len(transactions)} transactions, "
        f"{len(recon)} recon records."
    )

    async with AsyncSessionLocal() as db:

        # Merchants
        await _bulk_upsert(
            db, Merchant, list(merchants.values()),
            conflict_col="merchant_id",
            update_cols=["merchant_name"],
        )
        print(f"  ✓ Merchants  ({len(merchants)})")

        # Transactions
        await _bulk_upsert(
            db, Transaction, list(transactions.values()),
            conflict_col="transaction_id",
            update_cols=["current_status"],
        )
        print(f"  ✓ Transactions  ({len(transactions)})")

        # Payment events — deduplicate in Python before bulk insert
        seen: set[str] = set()
        unique_events: list[dict] = []
        for ev in events:
            if ev["event_id"] not in seen:
                seen.add(ev["event_id"])
                unique_events.append(
                    {
                        "event_id": ev["event_id"],
                        "event_type": ev["event_type"],
                        "transaction_id": ev["transaction_id"],
                        "merchant_id": ev["merchant_id"],
                        "amount": float(ev["amount"]),
                        "currency": ev.get("currency", "INR"),
                        "timestamp": _parse_ts(ev["timestamp"]),
                        "raw_payload": json.dumps(ev),
                    }
                )
        await _bulk_insert_events(db, unique_events)
        print(f"  ✓ Payment events  ({len(unique_events)} inserted, {duplicates} skipped)")

        # Reconciliation records
        await _bulk_upsert(
            db, ReconciliationRecord, list(recon.values()),
            conflict_col="transaction_id",
            update_cols=[
                "payment_status", "settlement_status",
                "discrepancy_flag", "discrepancy_reason", "settled_at",
            ],
        )
        discrepancy_count = sum(1 for r in recon.values() if r["discrepancy_flag"])
        print(
            f"  ✓ Reconciliation records  "
            f"({len(recon)} total, {discrepancy_count} discrepancies flagged)"
        )

    elapsed = time.perf_counter() - t0
    print(f"\nSeed complete in {elapsed:.2f}s ✓\n")

    # Summary breakdown
    from collections import Counter
    type_counts = Counter(e["event_type"] for e in events)
    status_counts = Counter(t["current_status"] for t in transactions.values())
    print("Event type breakdown:")
    for k, v in sorted(type_counts.items()):
        print(f"  {k:<30} {v}")
    print("Transaction status breakdown:")
    for k, v in sorted(status_counts.items()):
        print(f"  {k:<30} {v}")


if __name__ == "__main__":
    filepath = sys.argv[1] if len(sys.argv) > 1 else "sample_events.json"
    asyncio.run(seed(filepath))
