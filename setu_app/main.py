from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from setu_app.db import engine
from setu_app.models import Base
from setu_app.api_events import router as events_router
from setu_app.api_transactions import router as transactions_router
from setu_app.api_reconciliation import router as reconciliation_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(
    title="Setu Payment Lifecycle Service",
    description="Ingest payment lifecycle events, manage transaction state, and report reconciliation.",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(events_router,        prefix="/events",          tags=["Events"])
app.include_router(transactions_router,  prefix="/transactions",    tags=["Transactions"])
app.include_router(reconciliation_router,prefix="/reconciliation",  tags=["Reconciliation"])


@app.get("/", tags=["Health"])
async def root():
    return {"service": "Setu Payment Lifecycle Service", "status": "healthy", "version": "1.0.0", "docs": "/docs"}


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "healthy"}
