from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .service_pricing_experience import MIN_BASELINE_SAMPLES


def build_service_pricing_report_artifact(snapshot: dict[str, Any]) -> dict[str, Any]:
    scope = snapshot.get("scope") or {}
    quality = snapshot.get("data_quality") or {}
    baselines = [
        row
        for row in snapshot.get("labor_baselines") or []
        if int(row.get("sample_count") or 0) >= MIN_BASELINE_SAMPLES
    ]
    article_refs = snapshot.get("part_price_references") or []
    work_rows_total = int(quality.get("work_rows_total") or 0)
    valid_work_rows = int(quality.get("work_rows_valid") or 0)
    material_rows_total = int(quality.get("material_rows_total") or 0)
    article_material_rows = int(quality.get("material_rows_valid_for_article_reference") or 0)
    all_groups = len(snapshot.get("labor_baselines") or [])
    reusable_count = len(baselines)
    baseline_coverage = reusable_count / all_groups if all_groups else 0.0
    article_coverage = article_material_rows / material_rows_total if material_rows_total else 0.0
    generated_at = str(snapshot.get("generated_at") or datetime.now(UTC).isoformat())
    overview_source_id = "autostop_pricing_overview"
    labor_source_id = "autostop_labor_baselines"
    quality_source_id = "autostop_pricing_quality"

    labor_rows = []
    for rank, row in enumerate(baselines[:15], start=1):
        operation_name = str(row.get("operation_name") or "")
        chart_label = operation_name if len(operation_name) <= 30 else f"{operation_name[:27]}…"
        labor_rows.append(
            {
                "rank": rank,
                "operation_name": operation_name,
                "chart_label": chart_label,
                "category": row.get("category"),
                "sample_count": row.get("sample_count"),
                "median_rub": row.get("median_rub"),
                "p25_rub": row.get("p25_rub"),
                "p75_rub": row.get("p75_rub"),
                "min_rub": row.get("min_rub"),
                "max_rub": row.get("max_rub"),
                "latest_closed_date": row.get("latest_closed_date"),
                "confidence": row.get("confidence"),
            }
        )

    quality_rows = [
        {
            "metric": "Валидные строки работ",
            "valid_rows": valid_work_rows,
            "excluded_or_missing_rows": max(0, work_rows_total - valid_work_rows),
            "total_rows": work_rows_total,
            "coverage": valid_work_rows / work_rows_total if work_rows_total else 0.0,
            "kind": "labor",
        },
        {
            "metric": "Материалы с артикулом и ценой",
            "valid_rows": article_material_rows,
            "excluded_or_missing_rows": max(0, material_rows_total - article_material_rows),
            "total_rows": material_rows_total,
            "coverage": article_coverage,
            "kind": "parts",
        },
    ]
    overview = [
        {
            "selected_orders": int(scope.get("selected_closed_orders") or 0),
            "valid_work_rows": valid_work_rows,
            "reusable_baselines": reusable_count,
            "baseline_coverage": baseline_coverage,
            "article_references": len(article_refs),
            "article_coverage": article_coverage,
        }
    ]

    common_filters = [
        "repair_order.status = closed",
        f"latest {int(scope.get('selected_closed_orders') or 0)} orders by closed_at descending",
        "no order, client, phone, VIN, plate or payment identifiers retained",
    ]
    sources = [
        {
            "id": overview_source_id,
            "label": "Сводка обезличенного агрегата закрытых ЗН AutoStop",
            "path": "data/private_knowledge/service_pricing_experience.json",
            "query": {
                "engine": "sqlite",
                "language": "sql",
                "sql": (
                    "SELECT selected_orders, valid_work_rows, reusable_baselines, "
                    "baseline_coverage, article_references, article_coverage "
                    "FROM service_pricing_overview;"
                ),
                "description": "Сводные метрики окна последних закрытых заказ-нарядов.",
                "executed_at": generated_at,
                "filters": common_filters,
                "metric_definitions": [
                    "Reusable baseline: normalized operation with at least 3 valid work-row observations.",
                    "Article reference: historical CRM material unit price with a catalog number; not fitment or current supplier proof.",
                ],
                "tables_used": ["service_pricing_overview"],
            },
        },
        {
            "id": labor_source_id,
            "label": "Повторяемые внутренние ориентиры работ AutoStop",
            "path": "data/private_knowledge/service_pricing_experience.json",
            "query": {
                "engine": "sqlite",
                "language": "sql",
                "sql": (
                    "SELECT rank, operation_name, category, sample_count, median_rub, "
                    "p25_rub, p75_rub, min_rub, max_rub, latest_closed_date, confidence "
                    "FROM service_pricing_labor_baselines "
                    "WHERE sample_count >= 3 ORDER BY sample_count DESC, operation_name ASC LIMIT 15;"
                ),
                "description": "Повторяемые операции и робастные ценовые статистики.",
                "executed_at": generated_at,
                "filters": [*common_filters, "positive work-row unit price", "sample_count >= 3"],
                "metric_definitions": [
                    "Цена работы: CRM unit work-row price before separately displayed order tax.",
                    "Anchor: median unit price; uncertainty range: P25 to P75.",
                ],
                "tables_used": ["service_pricing_labor_baselines"],
            },
        },
        {
            "id": quality_source_id,
            "label": "Контроль качества ценовых строк AutoStop",
            "path": "data/private_knowledge/service_pricing_experience.json",
            "query": {
                "engine": "sqlite",
                "language": "sql",
                "sql": (
                    "SELECT metric, valid_rows, excluded_or_missing_rows, total_rows, coverage, kind "
                    "FROM service_pricing_quality ORDER BY kind ASC;"
                ),
                "description": "Покрытие строк работ и материалов пригодными наблюдениями.",
                "executed_at": generated_at,
                "filters": [
                    *common_filters,
                    "work rows require positive calculated unit price",
                    "part references require non-empty catalog number and positive sale price",
                ],
                "metric_definitions": [
                    "Coverage: valid_rows divided by total_rows.",
                    "Material row is article-safe only when catalog number, name and positive sale price exist.",
                ],
                "tables_used": ["service_pricing_quality"],
            },
        },
    ]

    title = "Опыт AutoStop по ценам работ: последние 100 закрытых ЗН"
    manifest = {
        "version": 1,
        "surface": "report",
        "title": title,
        "description": "Обезличенный анализ ценовых ориентиров и модернизации сервисного оценщика.",
        "generatedAt": generated_at,
        "sources": sources,
        "cards": [
            {
                "id": "orders-card",
                "dataset": "overview",
                "sourceId": overview_source_id,
                "description": "Закрытые заказ-наряды в анализируемом окне.",
                "metrics": [{"label": "Закрытых ЗН", "field": "selected_orders", "format": "number"}],
            },
            {
                "id": "work-rows-card",
                "dataset": "overview",
                "sourceId": overview_source_id,
                "description": "Строки работ с названием и положительной рассчитанной ценой единицы.",
                "metrics": [{"label": "Валидных строк работ", "field": "valid_work_rows", "format": "number"}],
            },
            {
                "id": "baselines-card",
                "dataset": "overview",
                "sourceId": overview_source_id,
                "description": "Нормализованные операции с минимум тремя наблюдениями.",
                "metrics": [
                    {"label": "Повторяемых ориентиров", "field": "reusable_baselines", "format": "number"},
                    {"label": "Доля групп", "field": "baseline_coverage", "format": "percent"},
                ],
            },
            {
                "id": "articles-card",
                "dataset": "overview",
                "sourceId": overview_source_id,
                "description": "Уникальные исторические артикульные ориентиры; актуальная цена и применимость требуют live-проверки.",
                "metrics": [
                    {"label": "Артикульных ориентиров", "field": "article_references", "format": "number"},
                    {"label": "Покрытие строк", "field": "article_coverage", "format": "percent"},
                ],
            },
        ],
        "charts": [
            {
                "id": "labor-median-chart",
                "title": "Медианная цена повторяемых операций",
                "subtitle": "Только группы с n≥3; диапазон и состав работ необходимо проверять перед сметой.",
                "type": "bar",
                "dataset": "labor_baselines",
                "sourceId": labor_source_id,
                "encodings": {
                    "x": {"field": "chart_label", "type": "nominal", "label": "Операция"},
                    "y": {
                        "field": "median_rub",
                        "type": "quantitative",
                        "format": "currency",
                        "unit": "RUB",
                        "label": "Медиана",
                    },
                    "tooltip": [
                        {"field": "operation_name", "type": "nominal", "label": "Полное название"},
                        {"field": "sample_count", "type": "quantitative", "label": "Наблюдений"},
                        {"field": "p25_rub", "type": "quantitative", "format": "currency", "label": "P25"},
                        {"field": "p75_rub", "type": "quantitative", "format": "currency", "label": "P75"},
                        {"field": "latest_closed_date", "type": "temporal", "label": "Последнее наблюдение"},
                    ],
                },
                "valueFormat": "currency",
                "unit": "RUB",
                "layout": "full",
                "maxRows": 10,
            },
            {
                "id": "data-coverage-chart",
                "title": "Покрытие строк пригодными ценовыми наблюдениями",
                "subtitle": "Работы почти полностью пригодны; материалы часто исключаются из-за отсутствия артикула.",
                "type": "bar",
                "dataset": "quality",
                "sourceId": quality_source_id,
                "encodings": {
                    "x": {"field": "metric", "type": "nominal", "label": "Тип строки"},
                    "y": {"field": "coverage", "type": "quantitative", "format": "percent", "label": "Доля"},
                    "tooltip": [
                        {"field": "valid_rows", "type": "quantitative", "label": "Пригодно"},
                        {"field": "excluded_or_missing_rows", "type": "quantitative", "label": "Исключено"},
                        {"field": "total_rows", "type": "quantitative", "label": "Всего"},
                    ],
                },
                "valueFormat": "percent",
                "layout": "full",
            },
        ],
        "tables": [
            {
                "id": "labor-baselines-table",
                "title": "Повторяемые внутренние ориентиры",
                "subtitle": "Медиана и межквартильный диапазон в рублях, группы с минимум тремя наблюдениями.",
                "dataset": "labor_baselines",
                "sourceId": labor_source_id,
                "defaultSort": {"field": "sample_count", "direction": "desc"},
                "density": "spacious",
                "layout": "full",
                "columns": [
                    {"field": "operation_name", "label": "Операция", "type": "text"},
                    {"field": "sample_count", "label": "n", "format": "number"},
                    {"field": "median_rub", "label": "Медиана", "format": "currency"},
                    {"field": "p25_rub", "label": "P25", "format": "currency"},
                    {"field": "p75_rub", "label": "P75", "format": "currency"},
                    {"field": "latest_closed_date", "label": "Последнее", "type": "date"},
                ],
            }
        ],
        "blocks": [
            {"id": "title", "type": "markdown", "body": f"# {title}"},
            {
                "id": "technical-summary",
                "type": "markdown",
                "sourceId": overview_source_id,
                "body": (
                    "## Технический итог\n\n"
                    f"Проанализированы **{int(scope.get('selected_closed_orders') or 0)} последних закрытых ЗН** "
                    f"за период **{scope.get('closed_date_from')}—{scope.get('closed_date_to')}**. "
                    f"Из **{work_rows_total}** строк работ пригодны **{valid_work_rows}**; "
                    f"из **{all_groups}** нормализованных групп только **{reusable_count}** имеют n≥3. "
                    f"Для деталей сохранены **{len(article_refs)}** исторических артикульных ориентира. "
                    "Главный вывод: внутренняя история уже надёжно поддерживает типовые работы, но редкие операции "
                    "нельзя оценивать без текущего рынка, точного автомобиля и трудоёмкости."
                ),
            },
            {
                "id": "metrics-heading",
                "type": "markdown",
                "body": "## Выборка стала полезным, но не самостоятельным ценовым источником\n\n"
                "Карточки ниже показывают объём и покрытие. Пригодность строки не означает, что её цена актуальна "
                "для другого автомобиля или включает тот же состав работ.",
            },
            {
                "id": "metrics",
                "type": "metric-strip",
                "cardIds": ["orders-card", "work-rows-card", "baselines-card", "articles-card"],
            },
            {
                "id": "labor-finding",
                "type": "markdown",
                "body": "## Типовые операции дают устойчивые внутренние якоря\n\n"
                "Наиболее повторяемые позиции образуют узкие диапазоны, но широкие общие названия вроде "
                "«диагностика» или «слесарные работы» требуют уточнения состава. График нужен для внутренней "
                "калибровки, а не для автоматической выдачи прайса клиенту.",
            },
            {"id": "labor-chart", "type": "chart", "chartId": "labor-median-chart"},
            {
                "id": "quality-finding",
                "type": "markdown",
                "body": "## Качество работ высокое, артикульная дисциплина материалов остаётся главным пробелом\n\n"
                "Почти все строки работ имеют положительную цену. Для материалов строгий фильтр пропускает только "
                "строки с артикулом и ценой; остальные нельзя безопасно использовать для будущего подбора и сравнения.",
            },
            {"id": "quality-chart", "type": "chart", "chartId": "data-coverage-chart"},
            {
                "id": "definitions",
                "type": "markdown",
                "body": "## Что именно измерено\n\n"
                "- Наблюдение по работе — цена единицы строки CRM до отдельно показанного налога заказа.\n"
                "- Внутренний ориентир — медиана; рабочий диапазон — P25–P75.\n"
                "- Повторяемая группа — минимум три валидных наблюдения.\n"
                "- Историческая цена детали хранится только при наличии артикула и не подтверждает применимость.",
            },
            {"id": "baseline-table", "type": "table", "tableId": "labor-baselines-table"},
            {
                "id": "methodology",
                "type": "markdown",
                "body": "## Методика и встроенная модернизация\n\n"
                "Последние закрытые заказы выбираются по `closed_at`, затем работы нормализуются по смысловой "
                "операции. Нулевые цены исключаются, расхождения количества/итога помечаются, сырые заказы не "
                "сохраняются. Оценщик теперь формирует `EvidenceBundle`: внутренняя история, текущий рынок, "
                "нормо-часы/сервисные данные и точный контекст автомобиля. Высокая уверенность требует трёх "
                "независимых семейств источников.",
            },
            {
                "id": "limitations",
                "type": "markdown",
                "body": "## Ограничения и проверки устойчивости\n\n"
                "- Окно короткое и отражает текущий профиль AutoStop, а не весь рынок.\n"
                "- Общие названия могут объединять разный объём; перед сметой обязателен scope-check.\n"
                "- НДС/налог заказа показывается отдельно и не включён в строковые ориентиры.\n"
                "- Исторические цены деталей быстро устаревают и требуют live-поставщика.\n"
                "- Редкие группы n<3 остаются только поисковыми подсказками, не ценовыми базовыми линиями.",
            },
            {
                "id": "next-steps",
                "type": "markdown",
                "body": "## Следующие шаги\n\n"
                "1. Обновлять агрегат по прямому поручению или плановому ценовому ревью, сохраняя только статистику.\n"
                "2. В ЗН обязательно указывать артикул выбранной детали и отделять закупку от продажи.\n"
                "3. Для каждого нового расчёта сверять внутреннюю медиану с AUTONORMS/OEM трудоёмкостью и текущим рынком.\n"
                "4. Отслеживать расхождения внутренней цены и рынка как сигнал пересмотра прайса или состава операции.",
            },
            {
                "id": "further-questions",
                "type": "markdown",
                "body": "## Что стоит исследовать дальше\n\n"
                "- Разделить ориентиры по классу автомобиля там, где накопится n≥5 на сегмент.\n"
                "- Добавить контроль изменения медианы по месяцам после появления более длинной истории.\n"
                "- Сопоставить самые частые операции с точными AUTONORMS-категориями и эффективной ставкой нормо-часа.",
            },
        ],
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
            },
        },
        "sources": sources,
    }


def save_report_artifact(artifact: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target
