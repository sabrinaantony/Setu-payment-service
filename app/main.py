"""
Setu Payment Lifecycle Service — FastAPI application entry point.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import events, reconciliation, transactions
from app.db.session import engine
from app.models.base import Base


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(
    title="Setu Payment Lifecycle Service",
    description=(
        "Backend service for ingesting payment lifecycle events, "
        "managing transaction state, and providing reconciliation reporting."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(events.router, prefix="/events", tags=["Events"])
app.include_router(transactions.router, prefix="/transactions", tags=["Transactions"])
app.include_router(reconciliation.router, prefix="/reconciliation", tags=["Reconciliation"])


@app.get("/", tags=["Health"])
async def root():
    return {
        "service": "Setu Payment Lifecycle Service",
        "status": "healthy",
        "version": "1.0.0",
        "docs": "/docs",
    }


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "healthy"}
