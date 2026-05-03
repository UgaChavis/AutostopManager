"""Формирование короткого отчета для менеджера."""

from __future__ import annotations

from models import Offer, PartRequest


def format_offer_line(offer: Offer) -> str:
    return (
        f"{offer.seller_name or offer.source_id}: {offer.brand} {offer.article}, "
        f"{offer.price_rub:.0f} ₽, срок {offer.delivery_days_min}-{offer.delivery_days_max} дн., "
        f"рейтинг {offer.scores.final:.1f}/100"
    )


def build_manager_report(request: PartRequest, ranked_offers: list[Offer]) -> str:
    if not ranked_offers:
        return (
            "Надежные предложения не найдены.\n"
            "Нужно уточнить VIN/номер детали, расширить географию и позвонить профильным продавцам."
        )
    best = ranked_offers[0]
    backups = ranked_offers[1:3]
    missing = ", ".join(request.missing_fields) if request.missing_fields else "нет"
    risks = sorted(set(flag for offer in ranked_offers[:3] for flag in offer.risk_flags))
    risk_text = ", ".join(risks) if risks else "существенные не выявлены"
    lines = [
        f"Заявка: {request.vehicle.make} {request.vehicle.model} / {request.part.name_normalized or request.part.name_raw}",
        f"OEM: {request.part.oem_number or 'не указан'}",
        f"Недостающие данные: {missing}",
        "",
        f"Лучший вариант: {format_offer_line(best)}",
        f"Почему: рейтинг {best.scores.final:.1f}/100, применяемость {best.scores.fitment:.0f}, наличие {best.scores.availability:.0f}, доставка {best.scores.delivery:.0f}.",
        "",
        "Резервные варианты:",
    ]
    if backups:
        for idx, offer in enumerate(backups, start=1):
            lines.append(f"{idx}. {format_offer_line(offer)}")
    else:
        lines.append("нет")
    lines.extend([
        "",
        f"Риски: {risk_text}",
        "Перед оплатой: подтвердить наличие, цену, возврат, фото маркировки и применяемость по VIN/номеру.",
    ])
    return "\n".join(lines)
