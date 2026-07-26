from __future__ import annotations

from datetime import UTC, datetime

from autostop_manager.service_pricing_experience import (
    build_service_pricing_experience,
    canonicalize_work_name,
    find_labor_experience,
)
from autostop_manager.service_pricing_report import build_service_pricing_report_artifact
from autostop_manager.work_pricing import estimate_repair_work_cost


def _card(
    *,
    closed_at: str,
    status: str = "closed",
    work_name: str = "Диагностика подвески",
    work_price: str = "1000",
    catalog_number: str = "ABC-123",
) -> dict:
    return {
        "id": f"private-{closed_at}",
        "vehicle": "Test vehicle",
        "repair_order": {
            "status": status,
            "closed_at": closed_at,
            "client": "Private Client",
            "phone": "+70000000000",
            "vin": "PRIVATEVIN1234567",
            "license_plate": "A000AA124",
            "payments": [{"amount": "1000"}],
            "works": [
                {
                    "name": work_name,
                    "quantity": "1",
                    "price": work_price,
                    "total": work_price,
                }
            ],
            "materials": [
                {
                    "name": "Test part",
                    "catalog_number": catalog_number,
                    "quantity": "1",
                    "price": "2500",
                    "total": "2500",
                    "cost_price": "2000",
                }
            ],
        },
    }


def test_build_experience_uses_latest_closed_orders_and_keeps_only_aggregates():
    state = {
        "cards": [
            _card(closed_at="01.07.2026 10:00", work_price="900"),
            _card(closed_at="02.07.2026 10:00", work_price="1000"),
            _card(closed_at="03.07.2026 10:00", work_price="1100"),
            _card(closed_at="04.07.2026 10:00", work_price="1200"),
            _card(closed_at="05.07.2026 10:00", status="open", work_price="9999"),
        ]
    }

    snapshot = build_service_pricing_experience(
        state,
        limit=3,
        generated_at=datetime(2026, 7, 6, tzinfo=UTC),
    )

    assert snapshot["scope"]["selected_closed_orders"] == 3
    assert snapshot["scope"]["closed_date_from"] == "2026-07-02"
    assert snapshot["scope"]["closed_date_to"] == "2026-07-04"
    baseline = snapshot["labor_baselines"][0]
    assert baseline["operation_name"] == "диагностика подвески"
    assert baseline["sample_count"] == 3
    assert baseline["median_rub"] == 1100
    serialized = str(snapshot)
    assert "Private Client" not in serialized
    assert "PRIVATEVIN" not in serialized
    assert "private-" not in serialized
    assert snapshot["privacy"]["raw_repair_orders_persisted"] is False


def test_canonicalization_keeps_related_but_distinct_operations_separate():
    assert canonicalize_work_name("Замена масла в ДВС")["key"] == "замена_масла_двс"
    assert canonicalize_work_name("Замена топливного фильтра")["key"] == "замена_топливного_фильтра"
    assert canonicalize_work_name("Замена проводки АКПП")["key"] != "снятие_установка_трансмиссии"
    assert canonicalize_work_name("Демонтаж / монтаж АКПП")["key"] == "снятие_установка_трансмиссии"


def test_find_labor_experience_and_estimator_return_provisional_internal_anchor():
    state = {
        "cards": [
            _card(closed_at="01.07.2026 10:00", work_price="1000"),
            _card(closed_at="02.07.2026 10:00", work_price="1000"),
            _card(closed_at="03.07.2026 10:00", work_price="1200"),
        ]
    }
    snapshot = build_service_pricing_experience(state, limit=3)

    matches = find_labor_experience("диагностика ходовой", snapshot=snapshot)
    assert matches[0]["sample_count"] == 3
    assert matches[0]["recommended_anchor_rub"] == 1000

    estimate = estimate_repair_work_cost(
        vehicle="Test vehicle",
        work_items=["диагностика ходовой"],
        auto_research=False,
        internal_experience_json=snapshot,
    )
    operation = estimate["operation_estimates"][0]
    assert operation["autostop_price_rub"] is None
    assert operation["recommended_price_rub"] == 1000
    assert operation["recommendation_basis"] == "internal_experience_provisional"
    assert operation["decision_confidence"] == "low"
    assert estimate["recommended_total_works_rub"] == 1000


def test_report_artifact_is_bounded_and_uses_runnable_source_metadata():
    state = {
        "cards": [
            _card(closed_at="01.07.2026 10:00", work_price="1000"),
            _card(closed_at="02.07.2026 10:00", work_price="1000"),
            _card(closed_at="03.07.2026 10:00", work_price="1200"),
        ]
    }
    snapshot = build_service_pricing_experience(state, limit=3)

    artifact = build_service_pricing_report_artifact(snapshot)

    assert artifact["surface"] == "report"
    manifest = artifact["manifest"]
    assert manifest["blocks"][0]["body"] == f"# {manifest['title']}"
    assert any(block["type"] == "chart" for block in manifest["blocks"])
    assert all(source["query"]["sql"].startswith("SELECT ") for source in manifest["sources"])
    assert artifact["snapshot"]["status"] == "ready"
    assert len(artifact["snapshot"]["datasets"]["labor_baselines"]) == 1
