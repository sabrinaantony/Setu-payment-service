# Setu Payment Lifecycle Service

A production-minded backend service for ingesting payment lifecycle events, maintaining transaction and reconciliation state, and exposing APIs for operations teams.

**Stack:** FastAPI · SQLAlchemy (async) · SQLite (dev) / PostgreSQL (prod) · Pydantic v2

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Quick Start — Local Development](#2-quick-start--local-development)
3. [API Documentation](#3-api-documentation)
4. [Sample Data](#4-sample-data)
5. [Running Tests](#5-running-tests)
6. [Deployment](#6-deployment)
7. [Assumptions and Tradeoffs](#7-assumptions-and-tradeoffs)
8. [AI Tool Disclosure](#8-ai-tool-disclosure)

---

## 1. Architecture Overview

```
┌──────────────────────────────────────────────────────────┐
│                      FastAPI App                         │
│                                                          │
│  POST /events                ──► EventService            │
│  GET  /transactions          ──► TransactionService      │
│  GET  /transactions/{id}     ──► TransactionService      │
│  GET  /reconciliation/summary       ──► ReconService     │
│  GET  /reconciliation/discrepancies ──► ReconService     │
│                                                          │
└──────────────────────────┬───────────────────────────────┘
                           │  SQLAlchemy async ORM
                           ▼
              ┌────────────────────────────┐
              │  SQLite  (local / Docker)  │
              │  PostgreSQL  (production)  │
              └────────────────────────────┘
```

### Layer Map

| Path | Responsibility |
|---|---|
| `app/api/` | FastAPI routers — HTTP wiring, query param validation, status codes |
| `app/services/` | Business logic — idempotency, state machine, discrepancy detection |
| `app/models/` | SQLAlchemy ORM models, enums, indexes |
| `app/schemas/` | Pydantic v2 request/response contracts |
| `app/db/` | Async engine + session factory, `get_db` dependency |
| `scripts/seed_db.py` | Bulk seeder that reads `sample_events.json` |
| `tests/` | Async integration tests with in-memory SQLite |

### Database Schema

```
merchants
  merchant_id   PK  VARCHAR(64)
  merchant_name     VARCHAR(255)
  created_at / updated_at

transactions
  transaction_id  PK  VARCHAR(64)
  merchant_id     FK → merchants
  amount              NUMERIC(14,2)
  currency            VARCHAR(8)
  current_status      VARCHAR(32)   ← denormalised; updated on every event
  created_at / updated_at

  Indexes: merchant_id · current_status · created_at · (merchant_id, current_status)

payment_events                        ← append-only log
  id              PK  SERIAL
  event_id        UNIQUE VARCHAR(64)  ← idempotency key
  event_type          VARCHAR(32)
  transaction_id  FK → transactions
  merchant_id / amount / currency / timestamp / raw_payload / received_at

  Indexes: transaction_id · timestamp

reconciliation_records
  id              PK  SERIAL
  transaction_id  UNIQUE FK → transactions
  payment_status      VARCHAR(32)
  settlement_status   VARCHAR(32)
  discrepancy_flag    BOOLEAN       ← written at ingest time; indexed
  discrepancy_reason  TEXT
  settled_at          TIMESTAMP
  created_at / updated_at

  Indexes: transaction_id · discrepancy_flag
```

### Key Design Decisions

**`current_status` denormalisation**
The `transactions` table stores a live snapshot of the latest payment state. `GET /transactions?status=settled` is a single index scan, not a correlated subquery over `payment_events`. The tradeoff (extra write per event) is negligible given read >> write in an ops API.

**Idempotency via UNIQUE constraint**
`payment_events.event_id` has a DB-level UNIQUE constraint. The service does `INSERT`, catches `IntegrityError` on a duplicate, rolls back, and returns `is_duplicate: true` — no state is mutated. This is safe under concurrent requests because the constraint is enforced at the database, not in application code.

**Discrepancy flag written at ingest time**
`reconciliation_records.discrepancy_flag` is set during event processing. `GET /reconciliation/discrepancies` is a cheap indexed `WHERE discrepancy_flag = TRUE OR ...` rather than a full-table scan with complex runtime logic.

**Append-only event log**
`payment_events` is never mutated after insert. Full audit history is always available.

**Status rank — no illegal transitions blocked**
Forward-only advancement (`initiated → processed/failed → settled`) is enforced by a rank comparison. Out-of-order or retrograde events are stored (history preserved) but do not overwrite a higher-rank status. Discrepancies caused by conflicting events are flagged rather than silently dropped.

---

## 2. Quick Start — Local Development

### Prerequisites

- Python 3.12+

### Steps

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/setu-payment-service.git
cd setu-payment-service

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy the provided sample data file to the project root
cp /path/to/sample_events.json .

# 5. Seed the database (~10 k events, completes in < 5 s)
python scripts/seed_db.py

# 6. Start the server
uvicorn app.main:app --reload
```

The API is live at **http://localhost:8000**

Interactive Swagger docs: **http://localhost:8000/docs**

ReDoc: **http://localhost:8000/redoc**

### Docker (alternative)

```bash
# Place sample_events.json in the project root first
docker build -t setu-payment-service .
docker run -p 8000:8000 setu-payment-service
```

### Environment Variables

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./setu_payments.db` | Switch to `postgresql+asyncpg://user:pass@host/db` for Postgres |

---

## 3. API Documentation

All responses are JSON. Error responses:
```json
{ "detail": "Human-readable message" }
```

---

### POST /events

Ingest a payment lifecycle event. **Idempotent.**

**Request body**

```json
{
  "event_id":       "b768e3a7-9eb3-4603-b21c-a54cc95661bc",
  "event_type":     "payment_initiated",
  "transaction_id": "2f86e94c-239c-4302-9874-75f28e3474ee",
  "merchant_id":    "merchant_2",
  "merchant_name":  "FreshBasket",
  "amount":         15248.29,
  "currency":       "INR",
  "timestamp":      "2026-01-08T12:11:58.085567+00:00"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `event_id` | string | ✓ | Globally unique. The idempotency key. |
| `event_type` | enum | ✓ | `payment_initiated` · `payment_processed` · `payment_failed` · `settled` |
| `transaction_id` | string | ✓ | Groups events into a transaction |
| `merchant_id` | string | ✓ | Merchant identifier |
| `merchant_name` | string | ✓ | Human-readable name (upserted on every call) |
| `amount` | float | ✓ | Must be > 0 |
| `currency` | string | | ISO 4217. Default: `INR` |
| `timestamp` | datetime | ✓ | ISO 8601 with timezone |

**Response — new event**
```json
{ "status": "accepted", "message": "Event ingested successfully.",
  "event_id": "...", "transaction_id": "...", "is_duplicate": false }
```

**Response — duplicate**
```json
{ "status": "duplicate", "message": "Event already processed; no state changes applied.",
  "event_id": "...", "transaction_id": "...", "is_duplicate": true }
```

---

### GET /transactions

Paginated transaction list.

**Query parameters**

| Param | Type | Default | Description |
|---|---|---|---|
| `merchant_id` | string | — | Filter by merchant |
| `status` | enum | — | `initiated` · `processed` · `failed` · `settled` |
| `date_from` | datetime | — | ISO 8601 lower bound on `created_at` (inclusive) |
| `date_to` | datetime | — | ISO 8601 upper bound on `created_at` (inclusive) |
| `sort_by` | enum | `created_at` | `created_at` · `updated_at` · `amount` |
| `sort_order` | enum | `desc` | `asc` · `desc` |
| `page` | int | `1` | 1-indexed |
| `page_size` | int | `20` | Max 200 |

**Example**
```
GET /transactions?merchant_id=merchant_1&status=settled&sort_by=amount&sort_order=desc&page=1&page_size=10
```

**Response**
```json
{
  "total": 1430,
  "page": 1,
  "page_size": 10,
  "items": [
    {
      "transaction_id": "2f86e94c-...",
      "merchant_id": "merchant_1",
      "amount": 49876.50,
      "currency": "INR",
      "current_status": "settled",
      "created_at": "2026-01-15T08:30:00+00:00",
      "updated_at": "2026-01-16T10:00:00+00:00"
    }
  ]
}
```

---

### GET /transactions/{transaction_id}

Full transaction detail including merchant info, complete event history, and reconciliation status.

**Response**
```json
{
  "transaction_id": "2f86e94c-...",
  "merchant_id": "merchant_2",
  "amount": 15248.29,
  "currency": "INR",
  "current_status": "settled",
  "created_at": "2026-01-08T12:11:58+00:00",
  "updated_at": "2026-01-09T09:00:00+00:00",
  "merchant": {
    "merchant_id": "merchant_2",
    "merchant_name": "FreshBasket"
  },
  "events": [
    { "event_id": "b768e3a7-...", "event_type": "payment_initiated",
      "timestamp": "2026-01-08T12:11:58+00:00", "amount": 15248.29,
      "currency": "INR", "received_at": "2026-01-08T12:11:59+00:00" },
    { "event_id": "c123-...", "event_type": "payment_processed",
      "timestamp": "2026-01-08T12:13:00+00:00", "amount": 15248.29,
      "currency": "INR", "received_at": "2026-01-08T12:13:01+00:00" },
    { "event_id": "d456-...", "event_type": "settled",
      "timestamp": "2026-01-09T09:00:00+00:00", "amount": 15248.29,
      "currency": "INR", "received_at": "2026-01-09T09:00:01+00:00" }
  ],
  "reconciliation": {
    "payment_status": "settled",
    "settlement_status": "settled",
    "discrepancy_flag": false,
    "discrepancy_reason": null,
    "settled_at": "2026-01-09T09:00:00+00:00"
  }
}
```

Returns **404** if the transaction does not exist.

---

### GET /reconciliation/summary

Aggregated counts and amounts grouped by a dimension.

**Query parameters**

| Param | Type | Default | Description |
|---|---|---|---|
| `group_by` | enum | `merchant` | `merchant` · `date` · `status` |
| `merchant_id` | string | — | Narrow to one merchant |

**Examples**
```
GET /reconciliation/summary?group_by=merchant
GET /reconciliation/summary?group_by=date&merchant_id=merchant_3
GET /reconciliation/summary?group_by=status
```

**Response**
```json
{
  "group_by": "merchant",
  "total_transactions": 3800,
  "items": [
    { "merchant_id": "merchant_1", "merchant_name": "QuickMart",
      "date": null, "status": "settled",
      "transaction_count": 780, "total_amount": 19234567.50 },
    { "merchant_id": "merchant_1", "merchant_name": "QuickMart",
      "date": null, "status": "failed",
      "transaction_count": 120, "total_amount": 2345678.00 }
  ]
}
```

When `group_by=date`, the `date` field is populated as `YYYY-MM-DD`.

---

### GET /reconciliation/discrepancies

Transactions where payment and settlement states are inconsistent.

**Discrepancy types detected**

| Scenario | Condition |
|---|---|
| Settled after failure | `payment_status = failed` AND `settlement_status = settled` |
| Processed, never settled | `payment_status = processed` AND `settlement_status = pending` |
| State conflict at ingest | `discrepancy_flag = true` (e.g. `settled` event after `payment_failed`) |

**Query parameters**

| Param | Type | Default | Description |
|---|---|---|---|
| `merchant_id` | string | — | Narrow to one merchant |
| `page` | int | `1` | Page number |
| `page_size` | int | `50` | Max 200 |

**Response**
```json
{
  "total": 95,
  "page": 1,
  "page_size": 50,
  "items": [
    {
      "transaction_id": "abc123-...",
      "merchant_id": "merchant_3",
      "amount": 2500.00,
      "currency": "INR",
      "payment_status": "failed",
      "settlement_status": "settled",
      "discrepancy_reason": "Settlement received for a failed payment.",
      "created_at": "2026-02-14T07:30:00+00:00"
    }
  ]
}
```

---

## 4. Sample Data

The provided `sample_events.json` contains **10,355 events** across **3,800 unique transactions** and **5 merchants**.

| Merchant ID | Name |
|---|---|
| merchant_1 | QuickMart |
| merchant_2 | FreshBasket |
| merchant_3 | UrbanEats |
| merchant_4 | TechBazaar |
| merchant_5 | StyleHub |

**Event type breakdown**

| Type | Count |
|---|---|
| `payment_initiated` | 3,864 |
| `payment_processed` | 3,004 |
| `settled` | 2,822 |
| `payment_failed` | 665 |
| Duplicate `event_id`s | 190 |

**Transaction scenario breakdown (from the data)**

| Scenario | Count | Notes |
|---|---|---|
| initiated → processed → settled | ~2,565 | Happy path |
| initiated → failed | ~570 | Clean failure |
| initiated → processed | ~380 | Stuck / pending settlement |
| initiated only | ~190 | Partially received |
| initiated → failed → settled | ~95 | **Discrepancy** |

Timestamps span **2026-01-08 to 2026-04-08**.

To seed:
```bash
python scripts/seed_db.py sample_events.json
```

---

## 5. Running Tests

```bash
pip install -r requirements.txt
pytest -v
```

Tests use an **in-memory SQLite database** — no external services required.

**What is tested**

- POST /events: happy path, full flow, idempotency, duplicate state isolation, failed → not_applicable, discrepancy flagging, 422 on bad input
- GET /transactions: listing, merchant/status/date filters, pagination, sorting, all 422 variants
- GET /transactions/{id}: detail with events ordered by timestamp, merchant, reconciliation, 404
- GET /reconciliation/summary: all three group_by dimensions, merchant filter, 422
- GET /reconciliation/discrepancies: settle-on-fail, stuck-processed, merchant filter, pagination

---

## 6. Deployment

### Railway (recommended)

1. Fork this repo and add `sample_events.json` to the root (not gitignored in the deployment branch)
2. New project on [railway.app](https://railway.app) → connect the GitHub repo
3. Railway detects the `Dockerfile` automatically
4. The Docker build runs `seed_db.py` — data is ready on first start
5. Optional: add a PostgreSQL plugin and set `DATABASE_URL`

### Render

- **Build command:** `pip install -r requirements.txt && python scripts/seed_db.py sample_events.json`
- **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

### Fly.io

```bash
fly launch    # follows prompts, detects Dockerfile
fly deploy
```

### Local Docker

```bash
# sample_events.json must be in the project root
docker build -t setu-payment-service .
docker run -p 8000:8000 setu-payment-service
```

---

## 7. Assumptions and Tradeoffs

### What I simplified

**SQLite for development, PostgreSQL for production.**
SQLite is zero-config — the reviewer can run the project in under 2 minutes. Switching to Postgres is one env var. The only SQLite-specific code is in the seed script (uses `sqlite_insert`); the application layer is fully database-agnostic via SQLAlchemy.

**No authentication.**
The assignment spec doesn't mention auth. In production this would be JWT or API-key middleware.

**No async message queue.**
Events arrive via HTTP POST. In a production system with very high throughput, a Kafka/SQS consumer feeding the ingest service would be more appropriate. The current design can be adapted easily since all business logic is in the service layer, not the HTTP handler.

**amount stored on the transaction at first-seen.**
If subsequent events for the same transaction arrive with a different amount (edge case not in the sample data), the transaction amount is not updated. The raw event payload captures the original amount for auditing.

### What I would add with more time

- **Alembic migrations** instead of `Base.metadata.create_all` at startup
- **PostgreSQL `UPSERT` dialect** in the seed script (currently uses SQLite dialect; a production seeder would use psycopg2's `insert().on_conflict_do_update()`)
- **Rate limiting and auth** (API key per merchant)
- **Structured logging** with request IDs (Loguru or structlog)
- **Background settlement reconciliation job** — a scheduled task that re-scans `payment_processed` rows older than N hours and flags them automatically, even if the live ingest path missed them
- **OpenTelemetry tracing** for the service and DB layers

### SQL over Python

All filtering, pagination, counting, and aggregation happens in SQL. Python is only used to bind parameters and serialise results. This keeps the database doing what it is good at and avoids loading unnecessary rows into memory.

---

## 8. AI Tool Disclosure

Claude (Anthropic) was used to:
- Generate the project scaffolding, service layer, and test suite based on the problem statement
- Review and iterate on schema design decisions

All code was reviewed, understood, and validated by the author before submission. The architectural decisions, schema design rationale, and tradeoff analysis represent the author's own engineering judgment.
