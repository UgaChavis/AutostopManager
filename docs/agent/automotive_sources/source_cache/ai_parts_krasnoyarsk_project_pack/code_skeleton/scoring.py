"""Скоринг предложений автозапчастей.

Это рабочий скелет. Его можно запускать на синтетических офферах и расширять под реальные источники.
"""

from __future__ import annotations

from dataclasses import replace
from statistics import median
from typing import Iterable

from models import Offer, OfferScores

DEFAULT_WEIGHTS = {
    "standard": {"fitment": 0.32, "availability": 0.18, "delivery": 0.14, "price": 0.14, "seller": 0.12, "warranty": 0.10},
    "urgent_today": {"fitment": 0.34, "availability": 0.26, "delivery": 0.22, "price": 0.06, "seller": 0.08, "warranty": 0.04},
    "used_or_contract": {"fitment": 0.32, "availability": 0.14, "delivery": 0.08, "price": 0.10, "seller": 0.18, "warranty": 0.18},
    "electronics": {"fitment": 0.42, "availability": 0.12, "delivery": 0.06, "price": 0.08, "seller": 0.14, "warranty": 0.18},
}

FITMENT_BASIS_SCORE = {
    "vin_oem": 100,
    "exact_oem": 90,
    "catalog_cross": 80,
    "supplier_cross": 65,
    "title_match": 45,
    "seller_claim": 40,
    "unknown": 20,
    None: 20,
}

RISK_PENALTIES = {
    "price_too_low": 8,
    "no_photo_marking_used_part": 12,
    "no_return": 10,
    "no_warranty": 8,
    "electronic_no_hw_number": 25,
    "electronic_no_coding_info": 15,
    "aggregator_unconfirmed_stock": 8,
    "marketplace_unconfirmed_stock": 12,
    "seller_new_unknown": 8,
    "stale_data": 8,
    "vin_required_missing": 30,
    "side_missing": 15,
    "body_restyling_unknown": 10,
    "needs_marking_photo": 12,
    "needs_video": 12,
}


def delivery_score(offer: Offer) -> float:
    days = offer.delivery_days_min if offer.delivery_days_min is not None else offer.delivery_days_max
    if days is None:
        return 0
    if days <= 0:
        return 100
    if days == 1:
        return 80
    if days <= 3:
        return 65
    if days <= 7:
        return 45
    if days <= 14:
        return 25
    return 10


def availability_score(offer: Offer) -> float:
    if offer.stock_confirmed and (offer.quantity is None or offer.quantity > 0):
        if offer.warehouse_city and offer.warehouse_city.lower() == "красноярск":
            return 100
        return 90
    if offer.quantity is not None and offer.quantity > 0:
        if offer.source_type == "b2b_api":
            return 80
        return 60
    if offer.source_type == "marketplace_manual":
        return 40
    return 20


def warranty_score(offer: Offer) -> float:
    if offer.returnable and (offer.warranty_days or 0) >= 14:
        return 100
    if offer.returnable and (offer.warranty_days or 0) > 0:
        return 75
    if offer.returnable:
        return 60
    if (offer.warranty_days or 0) > 0:
        return 45
    return 10


def seller_score(offer: Offer, seller_rating: float | None = None) -> float:
    if seller_rating is not None:
        return max(0, min(100, 50 + seller_rating))
    if offer.source_type == "local_seller":
        return 70
    if offer.source_type == "b2b_api":
        return 75
    if offer.source_type == "marketplace_manual":
        return 45
    return 50


def price_score(offer: Offer, market_median: float | None) -> float:
    total = total_cost(offer)
    if not market_median or market_median <= 0:
        return 50
    ratio = total / market_median
    if ratio <= 0.85:
        return 100
    if ratio <= 0.95:
        return 90
    if ratio <= 1.10:
        return 75
    if ratio <= 1.25:
        return 60
    if ratio <= 1.50:
        return 40
    return 20


def total_cost(offer: Offer, bay_idle_cost_per_day: float = 0.0) -> float:
    delivery = offer.delivery_cost_rub or 0
    days = offer.delivery_days_min or 0
    return float(offer.price_rub + delivery + days * bay_idle_cost_per_day)


def risk_penalty(offer: Offer, market_median: float | None) -> float:
    penalty = sum(RISK_PENALTIES.get(flag, 0) for flag in offer.risk_flags)
    if market_median and market_median > 0:
        ratio = total_cost(offer) / market_median
        if ratio < 0.65:
            penalty += RISK_PENALTIES["price_too_low"]
    if offer.condition in {"used", "contract"} and not offer.returnable:
        penalty += RISK_PENALTIES["no_return"]
    if offer.source_type == "marketplace_manual" and not offer.stock_confirmed:
        penalty += RISK_PENALTIES["marketplace_unconfirmed_stock"]
    return min(100, penalty)


def market_median_price(offers: Iterable[Offer]) -> float | None:
    values = [total_cost(o) for o in offers if o.price_rub is not None and o.price_rub > 0]
    if not values:
        return None
    return float(median(values))


def score_offer(offer: Offer, mode: str = "standard", market_median: float | None = None, seller_rating: float | None = None) -> Offer:
    weights = DEFAULT_WEIGHTS.get(mode, DEFAULT_WEIGHTS["standard"])
    fitment = FITMENT_BASIS_SCORE.get(offer.fitment_basis, 20)
    availability = availability_score(offer)
    delivery = delivery_score(offer)
    price = price_score(offer, market_median)
    seller = seller_score(offer, seller_rating=seller_rating)
    warranty = warranty_score(offer)
    penalty = risk_penalty(offer, market_median)
    final = (
        fitment * weights["fitment"]
        + availability * weights["availability"]
        + delivery * weights["delivery"]
        + price * weights["price"]
        + seller * weights["seller"]
        + warranty * weights["warranty"]
        - penalty
    )
    final = max(0, min(100, final))
    scored = replace(offer)
    scored.scores = OfferScores(
        fitment=fitment,
        availability=availability,
        delivery=delivery,
        price=price,
        seller=seller,
        warranty=warranty,
        risk_penalty=penalty,
        final=round(final, 2),
        expected_total_cost=round(total_cost(offer), 2),
    )
    return scored


def rank_offers(offers: list[Offer], mode: str = "standard") -> list[Offer]:
    med = market_median_price(offers)
    scored = [score_offer(o, mode=mode, market_median=med) for o in offers]
    return sorted(scored, key=lambda x: (x.scores.final, -x.scores.expected_total_cost), reverse=True)
