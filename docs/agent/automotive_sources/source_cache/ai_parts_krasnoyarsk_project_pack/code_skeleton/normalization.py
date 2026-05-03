"""Нормализация брендов, артикулов и текстовых запросов."""

from __future__ import annotations

import re
from typing import Iterable

CYR_LAT_CONFUSABLES = str.maketrans({
    "А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "К": "K", "М": "M",
    "О": "O", "Р": "P", "Т": "T", "Х": "X", "У": "Y",
    "а": "A", "в": "B", "с": "C", "е": "E", "н": "H", "к": "K", "м": "M",
    "о": "O", "р": "P", "т": "T", "х": "X", "у": "Y",
})


def normalize_article(article: str | None) -> str | None:
    """Удаляет пробелы, дефисы, точки, слэши и приводит к верхнему регистру."""
    if not article:
        return None
    text = article.translate(CYR_LAT_CONFUSABLES).upper()
    return re.sub(r"[^A-Z0-9]", "", text)


def article_variants(article: str) -> list[str]:
    """Возвращает варианты артикула для поиска."""
    norm = normalize_article(article)
    if not norm:
        return []
    variants = {article.strip(), norm}
    # BMW-like grouping: 11537509227 -> 11 53 7 509 227
    if len(norm) == 11 and norm.isdigit():
        variants.add(f"{norm[:2]} {norm[2:4]} {norm[4]} {norm[5:8]} {norm[8:]}")
        variants.add(f"{norm[:2]}-{norm[2:4]}-{norm[4]}-{norm[5:8]}-{norm[8:]}")
    return sorted(variants)


def normalize_brand(brand: str | None, aliases: dict[str, Iterable[str]] | None = None) -> str | None:
    if not brand:
        return None
    raw = brand.strip().upper()
    if not aliases:
        return raw
    for canonical, values in aliases.items():
        if raw == canonical.upper() or raw in {v.upper() for v in values}:
            return canonical.upper()
    return raw


def make_offer_key(seller: str | None, brand: str, article: str, warehouse_city: str | None, price: float, delivery_days: int | None) -> str:
    return "|".join([
        (seller or "").strip().lower(),
        normalize_brand(brand) or "",
        normalize_article(article) or "",
        (warehouse_city or "").strip().lower(),
        str(round(price, 2)),
        str(delivery_days) if delivery_days is not None else "",
    ])


def normalize_text_query(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text
