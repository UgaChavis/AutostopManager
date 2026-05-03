"""Генерация безопасных поисковых фраз для Авито/Дром/FarPost.

Этот модуль не скачивает объявления. Он помогает менеджеру искать вручную или через
официально разрешенный API, если такой доступ есть.
"""

from __future__ import annotations

from models import PartRequest
from normalization import article_variants


def build_marketplace_queries(request: PartRequest) -> list[str]:
    v = request.vehicle
    p = request.part
    pieces = {
        "make": v.make or "",
        "model": v.model or "",
        "body": v.body_code or "",
        "engine": v.engine_code or "",
        "part": p.name_normalized or p.name_raw,
        "side": p.side or "",
    }
    queries: set[str] = set()
    if p.oem_number:
        for variant in article_variants(p.oem_number):
            queries.add(variant)
            if v.model:
                queries.add(f"{variant} {v.model}")
    base = " ".join(x for x in [pieces["part"], pieces["make"], pieces["model"], pieces["body"]] if x)
    if base:
        queries.add(base)
        queries.add(f"б/у {base}")
        queries.add(f"контрактный {base}")
        queries.add(f"разбор {base}")
    if pieces["engine"]:
        queries.add(f"{pieces['part']} {pieces['engine']}")
    if pieces["side"] and pieces["side"] != "not_applicable":
        queries.add(f"{base} {pieces['side']}")
    return sorted(q.strip() for q in queries if q.strip())


def seller_questions_for_category(category: str | None) -> list[str]:
    common = [
        "Деталь физически есть в наличии?",
        "Где находится деталь?",
        "Цена актуальна?",
        "Можно ли поставить резерв?",
        "Есть ли гарантия и возврат?",
        "Можно ли отправить фото маркировки?",
    ]
    if category == "electronics":
        common.extend([
            "Какой аппаратный номер?",
            "Какая программная версия?",
            "Нужна привязка или кодирование?",
            "Возврат возможен, если блок не подойдет?",
        ])
    if category == "powertrain":
        common.extend([
            "Какая маркировка агрегата?",
            "Какой пробег?",
            "Есть ли видео работы?",
            "Какая гарантия запуска?",
        ])
    if category == "body":
        common.extend([
            "Есть фото креплений?",
            "Есть дефекты, ремонт, трещины?",
            "Какой цвет/код цвета?",
        ])
    return common
