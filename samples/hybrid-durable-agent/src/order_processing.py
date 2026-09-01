from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_CENT = Decimal("0.01")


class OrderItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sku: str
    quantity: int = Field(gt=0, strict=True)
    unit_price: Decimal = Field(ge=0, allow_inf_nan=False)

    @field_validator("sku")
    @classmethod
    def normalize_sku(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("SKU cannot be empty")
        return normalized


class Customer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    loyalty_tier: Literal["standard", "silver", "gold", "platinum"] = "standard"

    @field_validator("id")
    @classmethod
    def normalize_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Customer ID cannot be empty")
        return normalized

    @field_validator("loyalty_tier", mode="before")
    @classmethod
    def normalize_loyalty_tier(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class Shipping(BaseModel):
    model_config = ConfigDict(extra="ignore")

    country: str
    method: Literal["standard", "two_day", "overnight", "same_day"]

    @field_validator("country")
    @classmethod
    def normalize_country(cls, value: str) -> str:
        normalized = value.strip().upper()
        if len(normalized) != 2 or not normalized.isalpha():
            raise ValueError("Shipping country must be a two-letter code")
        return normalized

    @field_validator("method", mode="before")
    @classmethod
    def normalize_method(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class Order(BaseModel):
    model_config = ConfigDict(extra="ignore")

    order_id: str | None = None
    currency: str = "USD"
    customer: Customer
    shipping: Shipping
    items: list[OrderItem] = Field(min_length=1)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("Currency must be a three-letter code")
        return normalized


def _money(value: Decimal) -> str:
    return f"{value.quantize(_CENT, rounding=ROUND_HALF_UP):.2f}"


def prepare_order_for_agent(payload: object, *, order_id: str | None = None) -> dict[str, object]:
    """Validate an order and return the minimum trusted context needed by the agent."""
    order = Order.model_validate(payload)
    resolved_order_id = order_id or order.order_id
    if not resolved_order_id:
        raise ValueError("Order ID is required")

    prepared_items: list[dict[str, object]] = []
    subtotal = Decimal("0")
    total_quantity = 0
    for item in order.items:
        unit_price = item.unit_price.quantize(_CENT, rounding=ROUND_HALF_UP)
        line_total = unit_price * item.quantity
        subtotal += line_total
        total_quantity += item.quantity
        prepared_items.append(
            {
                "sku": item.sku,
                "quantity": item.quantity,
                "unit_price": _money(unit_price),
                "line_total": _money(line_total),
            }
        )

    review_signals: list[str] = []
    if subtotal >= Decimal("1000"):
        review_signals.append("high_value_order")
    if total_quantity >= 25:
        review_signals.append("bulk_quantity")
    if order.shipping.method in {"overnight", "same_day"}:
        review_signals.append("expedited_shipping")
    if order.shipping.country != "US":
        review_signals.append("international_shipping")

    return {
        "order_id": resolved_order_id,
        "currency": order.currency,
        "customer": {
            "id": order.customer.id,
            "loyalty_tier": order.customer.loyalty_tier,
        },
        "shipping": {
            "country": order.shipping.country,
            "method": order.shipping.method,
        },
        "items": prepared_items,
        "summary": {
            "line_items": len(prepared_items),
            "total_quantity": total_quantity,
            "subtotal": _money(subtotal),
        },
        "review_signals": review_signals,
    }