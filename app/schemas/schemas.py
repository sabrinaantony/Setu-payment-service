"""
Pydantic v2 request / response schemas.

All response models are defined independently of ORM models to keep
the API contract explicit and avoid accidental field leakage.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.models import EventType  # noqa: F401 — re-exported


# ---------------------------------------------------------------------------
# Event schemas
# ---------------------------------------------------------------------------


class EventIngestionRequest(BaseModel):
    event_id: str = Field(..., description="Globally unique event identifier")
    event_type: EventType = Field(..., description="payment_initiated | payment_processed | payment_failed | settled")
    transaction_id: str = Field(..., description="Transaction this event belongs to")
    merchant_id: str = Field(..., description="Merchant identifier")
    merchant_name: str = Field(..., description="Human-readable merchant name")
    amount: float = Field(..., gt=0, description="Transaction amount — must be positive")
    currency: str = Field(default="INR", max_length=8, description="ISO 4217 currency code")
    timestamp: datetime = Field(..., description="Event timestamp — ISO 8601 with timezone")

    @field_validator("currency")
    @classmethod
    def currency_upper(cls, v: str) -> str:
        return v.upper()

    model_config = ConfigDict(use_enum_values=True)


class EventIngestionResponse(BaseModel):
    status: str
    message: str
    event_id: str
    transaction_id: str
    is_duplicate: bool = False


# ---------------------------------------------------------------------------
# Merchant
# ---------------------------------------------------------------------------


class MerchantOut(BaseModel):
    merchant_id: str
    merchant_name: str
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Payment event (history entry)
# ---------------------------------------------------------------------------


class PaymentEventOut(BaseModel):
    event_id: str
    event_type: str
    timestamp: datetime
    amount: float
    currency: str
    received_at: datetime
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


class ReconciliationOut(BaseModel):
    payment_status: str
    settlement_status: str
    discrepancy_flag: bool
    discrepancy_reason: Optional[str] = None
    settled_at: Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------


class TransactionOut(BaseModel):
    transaction_id: str
    merchant_id: str
    amount: float
    currency: str
    current_status: str
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


class TransactionDetailOut(BaseModel):
    transaction_id: str
    merchant_id: str
    amount: float
    currency: str
    current_status: str
    created_at: datetime
    updated_at: datetime
    merchant: Optional[MerchantOut] = None
    events: list[PaymentEventOut] = []
    reconciliation: Optional[ReconciliationOut] = None
    model_config = ConfigDict(from_attributes=True)


class PaginatedTransactions(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[TransactionOut]


# ---------------------------------------------------------------------------
# Reconciliation summary
# ---------------------------------------------------------------------------


class ReconciliationSummaryItem(BaseModel):
    merchant_id: str
    merchant_name: Optional[str] = None
    date: Optional[str] = None  # YYYY-MM-DD; present only when group_by=date
    status: str
    transaction_count: int
    total_amount: float


class ReconciliationSummaryResponse(BaseModel):
    group_by: str
    total_transactions: int
    items: list[ReconciliationSummaryItem]


# ---------------------------------------------------------------------------
# Reconciliation discrepancies
# ---------------------------------------------------------------------------


class DiscrepancyItem(BaseModel):
    transaction_id: str
    merchant_id: str
    amount: float
    currency: str
    payment_status: str
    settlement_status: str
    discrepancy_reason: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class DiscrepancyResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[DiscrepancyItem]
