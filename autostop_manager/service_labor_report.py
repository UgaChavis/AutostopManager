from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _drift_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for baseline in snapshot.get("labor_baselines") or []:
        if not isinstance(baseline, dict):
            continue
        months = [row for row in baseline.get("monthly_baselines") or [] if isinstance(row, dict)]
        if len(months) < 2:
            continue
        first = months[0]
        latest = months[-1]
        first_value = float(first.get("weighted_median_rub") or first.get("median_rub") or 0)
        latest_value = float(latest.get("weighted_median_rub") or latest.get("median_rub") or 0)
        if first_value <= 0 or latest_value <= 0:
            continue
        change = round((latest_value - first_value) / first_value, 4)
        rows.append(
            {
                "operation_name": baseline.get("operation_name"),
                "sample_count": baseline.get("inlier_sample_count"),
                "first_month": first.get("month"),
                "first_weighted_median_rub": first_value,
                "latest_month": latest.get("month"),
                "latest_weighted_median_rub": latest_value,
                "period": f"{first.get('month')} → {latest.get('month')}",
                "change_rate": change,
                "absolute_change_rate": abs(change),
            }
        )
    rows.sort(key=lambda row: (-float(row["absolute_change_rate"]), -int(row.get("sample_count") or 0)))
    return rows[:20]


def build_service_labor_report_artifact(snapshot: dict[str, Any]) -> dict[str, Any]:
    scope = snapshot.get("scope") or {}
    quality = snapshot.get("data_quality") or {}
    baselines = [row for row in snapshot.get("labor_baselines") or [] if isinstance(row, dict)]
    reusable = [row for row in baselines if int(row.get("inlier_sample_count") or 0) >= 3]
    valid_rows = int(scope.get("valid_work_rows") or 0)
    total_rows = int(scope.get("work_rows_total") or 0)
    excluded_rows = max(0, total_rows - valid_rows)
    outlier_count = sum(int(row.get("outlier_count") or 0) for row in baselines)
    generated_at = str(snapshot.get("generated_at") or "")
    period = f"{scope.get('closed_date_from')}—{scope.get('closed_date_to')}"
    title = "Цены работ AutoStop: вся история закрытых ЗН"

    overview = [
        {
            "selected_closed_orders": int(scope.get("selected_closed_orders") or 0),
            "work_rows_total": total_rows,
            "valid_work_rows": valid_rows,
            "valid_work_row_share": float(scope.get("valid_work_row_share") or 0),
            "operation_groups": len(baselines),
            "reusable_baselines": len(reusable),
            "outlier_rows": outlier_count,
        }
    ]
    labor_rows = []
    for rank, row in enumerate(reusable[:20], start=1):
        operation_name = str(row.get("operation_name") or "")
        labor_rows.append(
            {
                "rank": rank,
                "operation_name": operation_name,
                "chart_label": operation_name if len(operation_name) <= 20 else f"{operation_name[:17]}…",
                "category": row.get("category"),
                "sample_count": row.get("sample_count"),
                "inlier_sample_count": row.get("inlier_sample_count"),
                "outlier_count": row.get("outlier_count"),
                "weighted_median_rub": row.get("weighted_median_rub"),
                "median_rub": row.get("median_rub"),
                "p25_rub": row.get("p25_rub"),
                "p75_rub": row.get("p75_rub"),
                "latest_closed_date": row.get("latest_closed_date"),
                "confidence": row.get("confidence"),
            }
        )
    quality_rows = [
        {"metric": "Пригодные строки", "rows": valid_rows},
        {"metric": "Исключённые строки", "rows": excluded_rows},
        {"metric": "Некорректное количество", "rows": int(quality.get("work_flag_invalid_quantity") or 0)},
        {"metric": "Нет положительной суммы", "rows": int(quality.get("work_flag_zero_or_missing_total") or 0)},
        {"metric": "Без исполнителя", "rows": int(quality.get("work_rows_without_executor") or 0)},
    ]
    drift_rows = _drift_rows(snapshot)

    filters = [
        "repair_order.status = closed",
        "all closed repair orders in one read-only state snapshot",
        "positive quantity and positive total for price baselines",
        "no order, client, phone, VIN, plate, payment, executor, salary or margin identity retained",
    ]
    sources = [
        {
            "id": "labor-overview-source",
            "label": "Агрегированная labor-only база закрытых ЗН AutoStop",
            "path": "data/private_knowledge/service_labor_experience.json",
            "query": {
                "engine": "artifact_snapshot",
                "language": "sql",
                "sql": (
                    "SELECT selected_closed_orders, work_rows_total, valid_work_rows, valid_work_row_share, "
                    "operation_groups, reusable_baselines, outlier_rows FROM service_labor_overview;"
                ),
                "description": "Сводные показатели полного labor-only снимка закрытых заказ-нарядов.",
                "executed_at": generated_at,
                "filters": filters,
                "metric_definitions": [
                    "Valid work row: named work with positive quantity and positive CRM row total.",
                    "Reusable baseline: normalized operation with at least three inlier observations.",
                    "Outlier: price outside the IQR rule; retained in counts but excluded from the anchor.",
                ],
                "tables_used": ["service_labor_overview"],
            },
        },
        {
            "id": "labor-baselines-source",
            "label": "Повторяемые внутренние ориентиры работ AutoStop",
            "path": "data/private_knowledge/service_labor_experience.json",
            "query": {
                "engine": "artifact_snapshot",
                "language": "sql",
                "sql": (
                    "SELECT rank, operation_name, category, sample_count, inlier_sample_count, outlier_count, "
                    "weighted_median_rub, median_rub, p25_rub, p75_rub, latest_closed_date, confidence "
                    "FROM service_labor_baselines WHERE inlier_sample_count >= 3 "
                    "ORDER BY sample_count DESC, operation_name ASC LIMIT 20;"
                ),
                "description": "Частые операции, робастный диапазон и взвешенная по свежести медиана.",
                "executed_at": generated_at,
                "filters": [*filters, "inlier_sample_count >= 3", "90-day recency half-life"],
                "metric_definitions": [
                    "Customer unit labor price: CRM work-row total divided by positive quantity.",
                    "Weighted median: exponential recency weighting with a 90-day half-life.",
                    "P25/P75: unweighted inlier interquartile range.",
                ],
                "tables_used": ["service_labor_baselines"],
            },
        },
        {
            "id": "labor-quality-source",
            "label": "Контроль качества строк работ AutoStop",
            "path": "data/private_knowledge/service_labor_experience.json",
            "query": {
                "engine": "artifact_snapshot",
                "language": "sql",
                "sql": "SELECT metric, rows FROM service_labor_quality ORDER BY rows DESC, metric ASC;",
                "description": "Пригодность и причины неполноты строк выполненных работ.",
                "executed_at": generated_at,
                "filters": filters,
                "metric_definitions": [
                    "Excluded row: missing name, non-positive quantity, or no positive CRM row total.",
                    "Missing executor is reported for operations review and never changes customer price.",
                ],
                "tables_used": ["service_labor_quality"],
            },
        },
        {
            "id": "labor-drift-source",
            "label": "Помесячная динамика внутренних ориентиров AutoStop",
            "path": "data/private_knowledge/service_labor_experience.json",
            "query": {
                "engine": "artifact_snapshot",
                "language": "sql",
                "sql": (
                    "SELECT operation_name, sample_count, first_month, first_weighted_median_rub, latest_month, "
                    "latest_weighted_median_rub, period, change_rate, absolute_change_rate FROM service_labor_drift "
                    "ORDER BY absolute_change_rate DESC, sample_count DESC LIMIT 20;"
                ),
                "description": "Изменение взвешенной медианы между первым и последним пригодным месяцем.",
                "executed_at": generated_at,
                "filters": [*filters, "at least two monthly buckets with at least three observations each"],
                "metric_definitions": [
                    "Change rate: latest weighted median divided by first weighted median minus one.",
                    "Monthly bucket is shown only with at least three work observations.",
                ],
                "tables_used": ["service_labor_drift"],
            },
        },
    ]

    cards = [
        {
            "id": "orders-card",
            "dataset": "overview",
            "sourceId": "labor-overview-source",
            "description": f"Все закрытые заказ-наряды за {period}.",
            "metrics": [{"label": "Закрытых ЗН", "field": "selected_closed_orders", "format": "number"}],
        },
        {
            "id": "coverage-card",
            "dataset": "overview",
            "sourceId": "labor-overview-source",
            "description": "Доля строк, пригодных для исторической ценовой статистики.",
            "metrics": [
                {"label": "Покрытие строк", "field": "valid_work_row_share", "format": "percent"},
                {"label": "Пригодно", "field": "valid_work_rows", "format": "number"},
            ],
        },
        {
            "id": "groups-card",
            "dataset": "overview",
            "sourceId": "labor-overview-source",
            "description": "Группы с минимум тремя невыбросными наблюдениями.",
            "metrics": [
                {"label": "Повторяемых ориентиров", "field": "reusable_baselines", "format": "number"},
                {"label": "Всего групп", "field": "operation_groups", "format": "number"},
            ],
        },
    ]
    charts = [
        {
            "id": "labor-anchor-chart",
            "title": "Взвешенная медиана наиболее частых операций",
            "subtitle": f"Внутренний труд AutoStop, {period}; только группы с n≥3, RUB.",
            "type": "bar",
            "dataset": "labor_baselines",
            "sourceId": "labor-baselines-source",
            "encodings": {
                "x": {"field": "chart_label", "type": "nominal", "label": "Операция"},
                "y": {
                    "field": "weighted_median_rub",
                    "type": "quantitative",
                    "format": "currency",
                    "unit": "RUB",
                    "label": "Взвешенная медиана",
                },
                "tooltip": [
                    {"field": "operation_name", "type": "nominal", "label": "Операция"},
                    {"field": "sample_count", "type": "quantitative", "label": "Наблюдений"},
                    {"field": "p25_rub", "type": "quantitative", "format": "currency", "label": "P25"},
                    {"field": "p75_rub", "type": "quantitative", "format": "currency", "label": "P75"},
                    {"field": "confidence", "type": "nominal", "label": "Надёжность"},
                ],
            },
            "valueFormat": "currency",
            "unit": "RUB",
            "layout": "full",
            "maxRows": 8,
            "options": {"orientation": "horizontal"},
        },
        {
            "id": "labor-quality-chart",
            "title": "Качество строк выполненных работ",
            "subtitle": f"Все {total_rows} строк закрытых ЗН; отдельные причины могут пересекаться.",
            "type": "bar",
            "dataset": "quality",
            "sourceId": "labor-quality-source",
            "encodings": {
                "x": {"field": "metric", "type": "nominal", "label": "Категория"},
                "y": {"field": "rows", "type": "quantitative", "format": "number", "label": "Строк"},
                "tooltip": [{"field": "rows", "type": "quantitative", "label": "Строк"}],
            },
            "valueFormat": "number",
            "layout": "full",
            "options": {"orientation": "horizontal"},
        },
    ]
    tables = [
        {
            "id": "labor-baselines-table",
            "title": "Повторяемые внутренние ориентиры",
            "subtitle": f"Взвешенная медиана и IQR, {period}; клиентскую цену подтверждать внешними источниками.",
            "dataset": "labor_baselines",
            "sourceId": "labor-baselines-source",
            "defaultSort": {"field": "sample_count", "direction": "desc"},
            "density": "spacious",
            "layout": "full",
            "columns": [
                {"field": "operation_name", "label": "Операция", "type": "text"},
                {"field": "sample_count", "label": "n", "format": "number"},
                {"field": "weighted_median_rub", "label": "Взвешенная медиана", "format": "currency"},
                {"field": "confidence", "label": "Надёжность", "type": "text"},
            ],
        },
        {
            "id": "labor-drift-table",
            "title": "Наибольшие изменения внутренних ориентиров",
            "subtitle": "Только операции минимум с двумя пригодными месячными периодами.",
            "dataset": "drift",
            "sourceId": "labor-drift-source",
            "defaultSort": {"field": "absolute_change_rate", "direction": "desc"},
            "density": "spacious",
            "layout": "full",
            "columns": [
                {"field": "operation_name", "label": "Операция", "type": "text"},
                {"field": "sample_count", "label": "n", "format": "number"},
                {"field": "period", "label": "Период", "type": "text"},
                {"field": "first_weighted_median_rub", "label": "Было", "format": "currency"},
                {"field": "latest_weighted_median_rub", "label": "Стало", "format": "currency"},
                {
                    "field": "change_rate",
                    "label": "Изменение",
                    "format": "percent",
                    "movement": True,
                },
                {"field": "absolute_change_rate", "label": "Модуль изменения", "format": "percent"},
            ],
        },
    ]
    blocks = [
        {"id": "title", "type": "markdown", "body": f"# {title}"},
        {
            "id": "executive-summary",
            "type": "markdown",
            "sourceId": "labor-overview-source",
            "body": (
                "## Executive Summary\n\n"
                f"- **История стала полноценным внутренним ориентиром.** Обработаны "
                f"**{int(scope.get('selected_closed_orders') or 0)} закрытых ЗН** и **{total_rows} строк работ** "
                f"за {period}; пригодны **{valid_rows} строк ({float(scope.get('valid_work_row_share') or 0):.1%})**.\n"
                f"- **Повторяемая база уже полезна для типовых операций.** Получено **{len(baselines)} групп**, "
                f"из них **{len(reusable)}** имеют минимум три невыбросных наблюдения; свежие цены имеют больший вес.\n"
                "- **Автоматический прайс по одной истории запрещён.** Для клиентской оценки внутренний ориентир "
                "сверяется с точным автомобилем и объёмом, текущим рынком и нормо-часами/сервисными данными."
            ),
        },
        {
            "id": "coverage-heading",
            "type": "markdown",
            "body": (
                "## Охват достаточен для типовых работ, длинный хвост остаётся контекстом\n\n"
                "Карточки показывают полный объём и долю пригодных строк. Большое число уникальных групп объясняется "
                "строгим сохранением оси, стороны и состава операции: это снижает риск ложного объединения разных работ."
            ),
        },
        {"id": "metrics", "type": "metric-strip", "cardIds": ["orders-card", "coverage-card", "groups-card"]},
        {
            "id": "anchors-finding",
            "type": "markdown",
            "body": (
                "## Частые операции дают устойчивые ценовые якоря\n\n"
                "Взвешенная медиана отражает фактические клиентские цены AutoStop и сильнее учитывает свежие работы. "
                "Диапазон P25–P75 показывает обычный разброс; перед сметой всё равно нужно проверить одинаковый объём."
            ),
        },
        {"id": "anchor-chart", "type": "chart", "chartId": "labor-anchor-chart"},
        {
            "id": "quality-finding",
            "type": "markdown",
            "body": (
                "## Потери данных малы и контролируемы\n\n"
                f"Из расчёта исключены **{excluded_rows} строк**: пять с некорректным количеством и девять без "
                "положительной суммы. Отсутствующий исполнитель отражён только в закрытом операционном отчёте и "
                "не меняет клиентскую цену."
            ),
        },
        {"id": "quality-chart", "type": "chart", "chartId": "labor-quality-chart"},
        {
            "id": "baseline-detail",
            "type": "markdown",
            "body": (
                "## Надёжность зависит от количества сопоставимых наблюдений\n\n"
                "`stable` означает минимум 10 невыбросных строк, `working` — 5–9, `preliminary` — 3–4. "
                "Группы с меньшей выборкой остаются поисковым контекстом и не выдаются как самостоятельная цена."
            ),
        },
        {"id": "baseline-table-block", "type": "table", "tableId": "labor-baselines-table"},
        {
            "id": "drift-finding",
            "type": "markdown",
            "body": (
                "## Ценовой дрейф — сигнал проверки, а не автоматического изменения прайса\n\n"
                "Таблица сравнивает первый и последний месяцы только там, где в каждом пригодном месячном срезе "
                "есть минимум три наблюдения. Изменение может отражать новый прайс, другой состав или смену профиля машин."
            ),
        },
        {"id": "drift-table-block", "type": "table", "tableId": "labor-drift-table"},
        {
            "id": "next-steps",
            "type": "markdown",
            "body": (
                "## Следующие действия\n\n"
                "1. Использовать новый labor-only снимок как первый внутренний источник при оценке работы.\n"
                "2. Для каждой клиентской сметы собирать EvidenceBundle: точный автомобиль/объём, внутренняя история, "
                "текущий рынок и нормо-часы/сервисные данные.\n"
                "3. Обновлять снимок после заметного накопления закрытых ЗН и отслеживать операции с существенным дрейфом.\n"
                "4. Оставлять цены запчастей в отдельном live-контуре поставщиков, OEM/кроссов и применимости."
            ),
        },
        {
            "id": "further-questions",
            "type": "markdown",
            "body": (
                "## Что исследовать дальше\n\n"
                "- Какие редкие формулировки безопасно объединить после ручной проверки состава работ?\n"
                "- Для каких операций сегмент автомобиля устойчиво меняет цену при n≥5 в каждом сегменте?\n"
                "- Где внутренний дрейф подтверждается текущим рынком и нормо-часами, а где объясняется изменением объёма?"
            ),
        },
        {
            "id": "caveats",
            "type": "markdown",
            "body": (
                "## Ограничения и допущения\n\n"
                "- Период отражает фактическую доступную историю CRM, а не весь российский рынок.\n"
                "- Статистика описывает клиентскую цену строки работы до отдельно отображаемого налога заказа.\n"
                "- Выбросы остаются в аудите, но не задают рекомендуемый внутренний ориентир.\n"
                "- Исполнитель, зарплата, себестоимость и маржа исключены из клиентского ценового контура.\n"
                "- Высокая уверенность требует точного объёма и трёх независимых семейств доказательств."
            ),
        },
    ]
    manifest = {
        "version": 1,
        "surface": "report",
        "title": title,
        "description": "Полный обезличенный labor-only анализ закрытых заказ-нарядов AutoStop.",
        "generatedAt": generated_at,
        "sources": sources,
        "cards": cards,
        "charts": charts,
        "tables": tables,
        "blocks": blocks,
    }
    return {
        "surface": "report",
        "manifest": manifest,
        "snapshot": {
            "version": 1,
            "generatedAt": generated_at,
            "status": "ready",
            "datasets": {
                "overview": overview,
                "labor_baselines": labor_rows,
                "quality": quality_rows,
                "drift": drift_rows,
            },
        },
        "sources": sources,
    }


def save_service_labor_report_artifact(artifact: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(artifact, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(target)
        os.chmod(target, 0o600)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return target
