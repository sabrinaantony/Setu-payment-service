from __future__ import annotations
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator
from setu_app.models import EventType  # noqa


class EventIngestionRequest(BaseModel):
    event_id:       str      = Field(..., description="Globally unique event identifier")
    event_type:     EventType
    transaction_id: str
    merchant_id:    str
    merchant_name:  str
    amount:         float    = Field(..., gt=0)
    currency:       str      = Field(default="INR", max_length=8)
    timestamp:      datetime

    @field_validator("currency")
    @classmethod
    def _upper(cls, v): return v.upper()

    model_config = ConfigDict(use_enum_values=True)


class EventIngestionResponse(BaseModel):
    status: str; message: str; event_id: str; transaction_id: str; is_duplicate: bool = False


class MerchantOut(BaseModel):
    merchant_id: str; merchant_name: str
    model_config = ConfigDict(from_attributes=True)


class PaymentEventOut(BaseModel):
    event_id: str; event_type: str; timestamp: datetime
    amount: float; currency: str; received_at: datetime
    model_config = ConfigDict(from_attributes=True)


class ReconciliationOut(BaseModel):
    payment_status: str; settlement_status: str
    discrepancy_flag: bool; discrepancy_reason: Optional[str] = None
    settled_at: Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)


class TransactionOut(BaseModel):
    transaction_id: str; merchant_id: str; amount: float; currency: str
    current_status: str; created_at: datetime; updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


class TransactionDetailOut(BaseModel):
    transaction_id: str; merchant_id: str; amount: float; currency: str
    current_status: str; created_at: datetime; updated_at: datetime
    merchant: Optional[MerchantOut] = None
    events: list[PaymentEventOut] = []
    reconciliation: Optional[ReconciliationOut] = None
    model_config = ConfigDict(from_attributes=True)


class PaginatedTransactions(BaseModel):
    total: int; page: int; page_size: int; items: list[TransactionOut]


class ReconciliationSummaryItem(BaseModel):
    merchant_id: str; merchant_name: Optional[str] = None
    date: Optional[str] = None; status: str
    transaction_count: int; total_amount: float


class ReconciliationSummaryResponse(BaseModel):
    group_by: str; total_transactions: int; items: list[ReconciliationSummaryItem]


class DiscrepancyItem(BaseModel):
    transaction_id: str; merchant_id: str; amount: float; currency: str
    payment_status: str; settlement_status: str; discrepancy_reason: str; created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class DiscrepancyResponse(BaseModel):
    total: int; page: int; page_size: int; items: list[DiscrepancyItem]
