"""32 integration tests — in-memory SQLite, no external deps."""
import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from setu_app.db import get_db
from setu_app.main import app
from setu_app.models import Base

_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(_URL, connect_args={"check_same_thread": False})
_Session = async_sessionmaker(_engine, class_=AsyncSession,
                               expire_on_commit=False, autoflush=False)

async def _override():
    async with _Session() as s:
        try:   yield s; await s.commit()
        except: await s.rollback(); raise

@pytest_asyncio.fixture(scope="session", autouse=True)
async def _tables():
    async with _engine.begin() as c: await c.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as c: await c.run_sync(Base.metadata.drop_all)

@pytest_asyncio.fixture
async def client():
    app.dependency_overrides[get_db] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()

_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

def _ev(etype="payment_initiated", tid=None, mid="m_test", mname="TestMerchant",
        amount=1000.0, eid=None, offset=0):
    return {"event_id": eid or str(uuid.uuid4()), "event_type": etype,
            "transaction_id": tid or str(uuid.uuid4()),
            "merchant_id": mid, "merchant_name": mname,
            "amount": amount, "currency": "INR",
            "timestamp": (_NOW + timedelta(seconds=offset)).isoformat()}

async def _post(client, p):
    r = await client.post("/events", json=p)
    assert r.status_code == 200, r.text
    return r.json()

# ─── POST /events ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_ingest_initiated(client):
    r = await _post(client, _ev())
    assert r["status"] == "accepted" and not r["is_duplicate"]

@pytest.mark.asyncio
async def test_full_happy_path(client):
    tid = str(uuid.uuid4())
    for i, et in enumerate(["payment_initiated","payment_processed","settled"]):
        await _post(client, _ev(et, tid=tid, offset=i*60))
    d = (await client.get(f"/transactions/{tid}")).json()
    assert d["current_status"] == "settled"
    assert len(d["events"]) == 3
    assert d["reconciliation"]["settlement_status"] == "settled"
    assert d["reconciliation"]["discrepancy_flag"] is False
    assert d["merchant"]["merchant_id"] == "m_test"

@pytest.mark.asyncio
async def test_idempotency_returns_duplicate_flag(client):
    p = _ev()
    r1 = await _post(client, p)
    r2 = (await client.post("/events", json=p)).json()
    assert not r1["is_duplicate"]
    assert r2["is_duplicate"] and r2["status"] == "duplicate"

@pytest.mark.asyncio
async def test_duplicate_does_not_add_event_row(client):
    tid, p = str(uuid.uuid4()), _ev(tid=str(uuid.uuid4()))
    p["transaction_id"] = tid
    await _post(client, p); await _post(client, p)
    assert len((await client.get(f"/transactions/{tid}")).json()["events"]) == 1

@pytest.mark.asyncio
async def test_duplicate_does_not_regress_status(client):
    tid = str(uuid.uuid4())
    init = _ev("payment_initiated", tid=tid, offset=0)
    proc = _ev("payment_processed", tid=tid, offset=10)
    await _post(client, init); await _post(client, proc); await _post(client, init)
    assert (await client.get(f"/transactions/{tid}")).json()["current_status"] == "processed"

@pytest.mark.asyncio
async def test_failed_sets_not_applicable(client):
    tid = str(uuid.uuid4())
    await _post(client, _ev("payment_initiated", tid=tid, offset=0))
    await _post(client, _ev("payment_failed",    tid=tid, offset=5))
    d = (await client.get(f"/transactions/{tid}")).json()
    assert d["current_status"] == "failed"
    assert d["reconciliation"]["settlement_status"] == "not_applicable"

@pytest.mark.asyncio
async def test_discrepancy_settle_on_fail(client):
    tid, mid = str(uuid.uuid4()), "m_disc"
    await _post(client, _ev("payment_initiated", tid=tid, mid=mid, offset=0))
    await _post(client, _ev("payment_failed",    tid=tid, mid=mid, offset=5))
    await _post(client, _ev("settled",           tid=tid, mid=mid, offset=10))
    recon = (await client.get(f"/transactions/{tid}")).json()["reconciliation"]
    assert recon["discrepancy_flag"] is True
    assert "failed" in recon["discrepancy_reason"].lower()

@pytest.mark.asyncio
async def test_missing_fields_422(client):
    assert (await client.post("/events", json={"event_type":"payment_initiated"})).status_code == 422

@pytest.mark.asyncio
async def test_invalid_event_type_422(client):
    p = _ev(); p["event_type"] = "payment_reversed"
    assert (await client.post("/events", json=p)).status_code == 422

@pytest.mark.asyncio
async def test_zero_amount_422(client):
    p = _ev(); p["amount"] = 0
    assert (await client.post("/events", json=p)).status_code == 422

@pytest.mark.asyncio
async def test_negative_amount_422(client):
    p = _ev(); p["amount"] = -100
    assert (await client.post("/events", json=p)).status_code == 422

# ─── GET /transactions ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_list_basic(client):
    mid = "m_list_" + str(uuid.uuid4())[:8]
    for _ in range(4): await _post(client, _ev(mid=mid))
    r = (await client.get("/transactions", params={"merchant_id": mid})).json()
    assert r["total"] == 4 and len(r["items"]) == 4

@pytest.mark.asyncio
async def test_list_filter_status(client):
    mid = "m_st_" + str(uuid.uuid4())[:8]; tid = str(uuid.uuid4())
    await _post(client, _ev("payment_initiated", tid=tid, mid=mid))
    await _post(client, _ev("payment_failed",    tid=tid, mid=mid))
    r = (await client.get("/transactions", params={"merchant_id":mid,"status":"failed"})).json()
    assert r["total"] == 1 and r["items"][0]["transaction_id"] == tid

@pytest.mark.asyncio
async def test_list_date_range(client):
    mid = "m_dr_" + str(uuid.uuid4())[:8]
    await _post(client, _ev(mid=mid))
    r = (await client.get("/transactions", params={"merchant_id":mid,
         "date_from":"2026-01-01T00:00:00+00:00","date_to":"2026-12-31T23:59:59+00:00"})).json()
    assert r["total"] >= 1

@pytest.mark.asyncio
async def test_list_pagination(client):
    mid = "m_pg_" + str(uuid.uuid4())[:8]
    for _ in range(5): await _post(client, _ev(mid=mid))
    r1 = (await client.get("/transactions", params={"merchant_id":mid,"page":1,"page_size":2})).json()
    r2 = (await client.get("/transactions", params={"merchant_id":mid,"page":2,"page_size":2})).json()
    r3 = (await client.get("/transactions", params={"merchant_id":mid,"page":3,"page_size":2})).json()
    assert len(r1["items"])==2 and len(r2["items"])==2 and len(r3["items"])==1

@pytest.mark.asyncio
async def test_list_sort_amount(client):
    mid = "m_so_" + str(uuid.uuid4())[:8]
    for a in [300,100,200]: await _post(client, _ev(mid=mid, amount=a))
    items = (await client.get("/transactions",
             params={"merchant_id":mid,"sort_by":"amount","sort_order":"asc"})).json()["items"]
    amounts = [i["amount"] for i in items]
    assert amounts == sorted(amounts)

@pytest.mark.asyncio
async def test_list_invalid_status_422(client):
    assert (await client.get("/transactions", params={"status":"refunded"})).status_code == 422

@pytest.mark.asyncio
async def test_list_invalid_sort_422(client):
    assert (await client.get("/transactions", params={"sort_by":"bad"})).status_code == 422

@pytest.mark.asyncio
async def test_list_date_inversion_422(client):
    r = await client.get("/transactions", params={
        "date_from":"2026-12-01T00:00:00+00:00","date_to":"2026-01-01T00:00:00+00:00"})
    assert r.status_code == 422

# ─── GET /transactions/{id} ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_detail_events_ordered(client):
    tid = str(uuid.uuid4())
    for i,et in enumerate(["payment_initiated","payment_processed","settled"]):
        await _post(client, _ev(et, tid=tid, offset=i*30))
    d = (await client.get(f"/transactions/{tid}")).json()
    assert [e["event_type"] for e in d["events"]] == ["payment_initiated","payment_processed","settled"]
    assert d["merchant"] is not None and d["reconciliation"] is not None

@pytest.mark.asyncio
async def test_detail_not_found(client):
    assert (await client.get(f"/transactions/{uuid.uuid4()}")).status_code == 404

# ─── GET /reconciliation/summary ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_summary_merchant(client):
    r = (await client.get("/reconciliation/summary", params={"group_by":"merchant"})).json()
    assert r["group_by"] == "merchant" and isinstance(r["items"], list)

@pytest.mark.asyncio
async def test_summary_date(client):
    r = (await client.get("/reconciliation/summary", params={"group_by":"date"})).json()
    assert r["group_by"] == "date"
    if r["items"]: assert r["items"][0]["date"] is not None

@pytest.mark.asyncio
async def test_summary_status(client):
    assert (await client.get("/reconciliation/summary", params={"group_by":"status"})).json()["group_by"] == "status"

@pytest.mark.asyncio
async def test_summary_merchant_filter(client):
    mid = "m_smf_" + str(uuid.uuid4())[:8]
    await _post(client, _ev(mid=mid))
    r = (await client.get("/reconciliation/summary",
         params={"group_by":"merchant","merchant_id":mid})).json()
    for item in r["items"]: assert item["merchant_id"] == mid

@pytest.mark.asyncio
async def test_summary_invalid_group_by_422(client):
    assert (await client.get("/reconciliation/summary", params={"group_by":"region"})).status_code == 422

# ─── GET /reconciliation/discrepancies ───────────────────────────────────────
@pytest.mark.asyncio
async def test_discrepancies_settle_on_fail(client):
    mid, tid = "m_d2_" + str(uuid.uuid4())[:8], str(uuid.uuid4())
    await _post(client, _ev("payment_initiated", tid=tid, mid=mid, offset=0))
    await _post(client, _ev("payment_failed",    tid=tid, mid=mid, offset=5))
    await _post(client, _ev("settled",           tid=tid, mid=mid, offset=10))
    data = (await client.get("/reconciliation/discrepancies")).json()
    assert tid in [i["transaction_id"] for i in data["items"]]

@pytest.mark.asyncio
async def test_discrepancies_stuck_processed(client):
    mid, tid = "m_stk_" + str(uuid.uuid4())[:8], str(uuid.uuid4())
    await _post(client, _ev("payment_initiated", tid=tid, mid=mid, offset=0))
    await _post(client, _ev("payment_processed", tid=tid, mid=mid, offset=5))
    data = (await client.get("/reconciliation/discrepancies",
             params={"merchant_id":mid})).json()
    assert data["total"] >= 1 and tid in [i["transaction_id"] for i in data["items"]]

@pytest.mark.asyncio
async def test_discrepancies_unknown_merchant_empty(client):
    r = (await client.get("/reconciliation/discrepancies",
         params={"merchant_id":"nonexistent_xyz"})).json()
    assert r["total"] == 0 and r["items"] == []

@pytest.mark.asyncio
async def test_discrepancies_pagination(client):
    r = (await client.get("/reconciliation/discrepancies",
         params={"page":1,"page_size":5})).json()
    assert len(r["items"]) <= 5 and r["page"] == 1

# ─── Health ───────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_health(client):
    assert (await client.get("/health")).json()["status"] == "healthy"

@pytest.mark.asyncio
async def test_root(client):
    assert "service" in (await client.get("/")).json()
