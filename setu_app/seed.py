"""
Bulk-seed from sample_events.json using ON CONFLICT DO UPDATE/NOTHING.
Safe to run multiple times (idempotent). Completes in ~3s for 10k events.
"""
import asyncio, json, time
from datetime import datetime, timezone
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from setu_app.db import AsyncSessionLocal, engine
from setu_app.models import (Base, Merchant, PaymentEvent,
                              ReconciliationRecord, SettlementStatus,
                              Transaction, TransactionStatus)

BATCH = 500
_RANK = {"initiated":0,"processed":1,"failed":1,"settled":2}
_EV   = {"payment_initiated":"initiated","payment_processed":"processed",
          "payment_failed":"failed","settled":"settled"}


def _parse(ts): return datetime.fromisoformat(ts)


def compute_state(events):
    merchants, txns, recon = {}, {}, {}
    for ev in sorted(events, key=lambda e: e["timestamp"]):
        mid, tid, etype = ev["merchant_id"], ev["transaction_id"], ev["event_type"]
        ns = _EV.get(etype, etype)
        merchants[mid] = {"merchant_id": mid, "merchant_name": ev["merchant_name"]}
        if tid not in txns:
            txns[tid] = {"transaction_id":tid,"merchant_id":mid,
                         "amount":float(ev["amount"]),"currency":ev.get("currency","INR"),
                         "current_status":ns}
        elif _RANK.get(ns,-1) > _RANK.get(txns[tid]["current_status"],-1):
            txns[tid]["current_status"] = ns

        if tid not in recon:
            ss = (SettlementStatus.not_applicable.value
                  if ns == TransactionStatus.failed.value else SettlementStatus.pending.value)
            recon[tid] = {"transaction_id":tid,"payment_status":ns,
                          "settlement_status":ss,"discrepancy_flag":False,
                          "discrepancy_reason":None,"settled_at":None}
        else:
            r = recon[tid]
            r["payment_status"] = txns[tid]["current_status"]
            if etype == "settled":
                if r["payment_status"] == TransactionStatus.failed.value:
                    r["discrepancy_flag"]  = True
                    r["discrepancy_reason"]= "Settlement received for a failed payment."
                else:
                    r["settlement_status"] = SettlementStatus.settled.value
                    r["settled_at"]        = _parse(ev["timestamp"])
            elif etype == "payment_failed":
                if r["settlement_status"] == SettlementStatus.settled.value:
                    r["discrepancy_flag"]  = True
                    r["discrepancy_reason"]= "Payment marked failed after settlement recorded."
                else:
                    r["settlement_status"] = SettlementStatus.not_applicable.value

    # Flag stuck: processed but settlement still pending
    for tid, r in recon.items():
        if (not r["discrepancy_flag"]
                and txns[tid]["current_status"] == TransactionStatus.processed.value
                and r["settlement_status"] == SettlementStatus.pending.value):
            r["discrepancy_flag"]  = True
            r["discrepancy_reason"]= "Payment processed but settlement is still pending."

    return merchants, txns, recon


async def _upsert(db, model, rows, pk, update_cols):
    for i in range(0, len(rows), BATCH):
        stmt = sqlite_insert(model).values(rows[i:i+BATCH])
        stmt = stmt.on_conflict_do_update(
            index_elements=[pk],
            set_={c: getattr(stmt.excluded, c) for c in update_cols})
        await db.execute(stmt)
    await db.commit()


async def seed(path: str):
    print(f"Loading {path} …")
    with open(path) as f: events = json.load(f)
    total = len(events)
    unique_ids = len({e["event_id"] for e in events})
    print(f"  {total} events  |  {unique_ids} unique  |  {total-unique_ids} duplicates")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    t0 = time.perf_counter()
    merchants, txns, recon = compute_state(events)
    print(f"  State computed in {time.perf_counter()-t0:.2f}s — "
          f"{len(merchants)} merchants, {len(txns)} transactions")

    async with AsyncSessionLocal() as db:
        await _upsert(db, Merchant, list(merchants.values()),
                      "merchant_id", ["merchant_name"])
        print(f"  ✓ Merchants ({len(merchants)})")

        await _upsert(db, Transaction, list(txns.values()),
                      "transaction_id", ["current_status"])
        print(f"  ✓ Transactions ({len(txns)})")

        seen, evs = set(), []
        for ev in events:
            if ev["event_id"] not in seen:
                seen.add(ev["event_id"])
                evs.append({"event_id":ev["event_id"],"event_type":ev["event_type"],
                             "transaction_id":ev["transaction_id"],"merchant_id":ev["merchant_id"],
                             "amount":float(ev["amount"]),"currency":ev.get("currency","INR"),
                             "timestamp":_parse(ev["timestamp"]),"raw_payload":json.dumps(ev)})
        for i in range(0, len(evs), BATCH):
            stmt = sqlite_insert(PaymentEvent).values(evs[i:i+BATCH])
            stmt = stmt.on_conflict_do_nothing(index_elements=["event_id"])
            await db.execute(stmt)
        await db.commit()
        print(f"  ✓ Events ({len(evs)} inserted, {total-len(evs)} duplicates skipped)")

        disc = sum(1 for r in recon.values() if r["discrepancy_flag"])
        await _upsert(db, ReconciliationRecord, list(recon.values()), "transaction_id",
                      ["payment_status","settlement_status","discrepancy_flag",
                       "discrepancy_reason","settled_at"])
        print(f"  ✓ Reconciliation ({len(recon)} records, {disc} discrepancies flagged)")

    print(f"Seed done in {time.perf_counter()-t0:.2f}s ✓")
