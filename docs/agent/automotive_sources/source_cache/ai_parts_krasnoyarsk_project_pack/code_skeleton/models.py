"""
Базовые модели проекта поиска автозапчастей.

Файл намеренно не использует внешние зависимости. В рабочем проекте можно заменить
на pydantic-модели и JSON Schema validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class Vehicle:
    make: Optional[str] = None
    model: Optional[str] = None
    year: Optional[int] = None
    vin_or_frame: Optional[str] = None
    body_code: Optional[str] = None
    engine_code: Optional[str] = None
    transmission_code: Optional[str] = None
    drive: Optional[str] = None
    production_date: Optional[str] = None


@dataclass
class PartNeed:
    name_raw: str
    name_normalized: Optional[str] = None
    category: Optional[str] = None
    oem_number: Optional[str] = None
    article_candidates: list[str] = field(default_factory=list)
    brand_candidates: list[str] = field(default_factory=list)
    side: Optional[str] = None
    position: Optional[str] = None
    condition_allowed: list[str] = field(default_factory=lambda: ["new"])


@dataclass
class SearchConstraints:
    city: str = "Красноярск"
    region: Optional[str] = None
    urgency: str = "unknown"
    max_budget_rub: Optional[float] = None
    delivery_allowed: bool = True
    preferred_sources: list[str] = field(default_factory=list)


@dataclass
class PartRequest:
    request_id: str
    vehicle: Vehicle
    part: PartNeed
    constraints: SearchConstraints
    created_at: Optional[datetime] = None
    missing_fields: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)


@dataclass
class OfferScores:
    fitment: float = 0.0
    availability: float = 0.0
    delivery: float = 0.0
    price: float = 0.0
    seller: float = 0.0
    warranty: float = 0.0
    risk_penalty: float = 0.0
    final: float = 0.0
    expected_total_cost: float = 0.0


@dataclass
class Offer:
    offer_id: str
    source_id: str
    brand: str
    article: str
    price_rub: float
    source_type: str = "unknown"
    seller_id: Optional[str] = None
    seller_name: Optional[str] = None
    seller_city: Optional[str] = None
    article_norm: Optional[str] = None
    name: Optional[str] = None
    condition: str = "unknown"
    delivery_cost_rub: Optional[float] = None
    quantity: Optional[int] = None
    delivery_days_min: Optional[int] = None
    delivery_days_max: Optional[int] = None
    warehouse_city: Optional[str] = None
    returnable: Optional[bool] = None
    warranty_days: Optional[int] = None
    fitment_basis: Optional[str] = None
    stock_confirmed: bool = False
    confirmed_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    url: Optional[str] = None
    notes: Optional[str] = None
    risk_flags: list[str] = field(default_factory=list)
    scores: OfferScores = field(default_factory=OfferScores)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Seller:
    seller_id: str
    name: str
    city: str
    address: Optional[str] = None
    phones: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    brands_focus: list[str] = field(default_factory=list)
    has_delivery: Optional[bool] = None
    has_return: Optional[bool] = None
    warranty_default_days: Optional[int] = None
    rating_internal: float = 0.0
    orders_successful: int = 0
    orders_failed: int = 0
    last_verified_at: Optional[str] = None
    notes: Optional[str] = None
