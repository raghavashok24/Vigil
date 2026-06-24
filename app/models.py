"""Domain models for the binary-outcome order-matching engine."""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class Side(str, Enum):
    YES = "YES"
    NO = "NO"


class OrderStatus(str, Enum):
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"


class Order(BaseModel):
    order_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    market_id: str
    user_id: str
    side: Side
    price: float  # probability in (0, 1) exclusive
    quantity: int  # number of contracts (positive integer)
    filled: int = 0
    status: OrderStatus = OrderStatus.OPEN
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @field_validator("price")
    @classmethod
    def price_must_be_valid_probability(cls, v: float) -> float:
        if not (0 < v < 1):
            raise ValueError("price must be strictly between 0 and 1")
        return round(v, 6)

    @field_validator("quantity")
    @classmethod
    def quantity_must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("quantity must be a positive integer")
        return v

    @property
    def remaining(self) -> int:
        return self.quantity - self.filled

    def is_active(self) -> bool:
        return self.status in (OrderStatus.OPEN, OrderStatus.PARTIAL)


class Trade(BaseModel):
    trade_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    market_id: str
    taker_order_id: str
    maker_order_id: str
    taker_user_id: str
    maker_user_id: str
    side: Side  # taker's side
    price: float
    quantity: int
    created_at: float = Field(default_factory=time.time)


# ── REST request / response schemas ──────────────────────────────────────────

class SubmitOrderRequest(BaseModel):
    market_id: str
    user_id: str
    side: Side
    price: float
    quantity: int

    @field_validator("price")
    @classmethod
    def price_valid(cls, v: float) -> float:
        if not (0 < v < 1):
            raise ValueError("price must be strictly between 0 and 1")
        return round(v, 6)

    @field_validator("quantity")
    @classmethod
    def qty_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("quantity must be positive")
        return v


class SubmitOrderResponse(BaseModel):
    order: Order
    trades: list[Trade]


class CancelOrderResponse(BaseModel):
    order: Order


class OrderBookLevel(BaseModel):
    price: float
    quantity: int  # total remaining at this price level


class OrderBookSnapshot(BaseModel):
    market_id: str
    yes_bids: list[OrderBookLevel]  # YES side, descending price
    no_bids: list[OrderBookLevel]   # NO side, descending price
    trades: list[Trade]             # recent trades (last 50)
