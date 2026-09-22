from __future__ import annotations

import json

from autostop_manager.work_pricing import estimate_repair_work_cost


def _quote(source: str, price: int, operation: str = "замена рулевой рейки", city: str = "Красноярск"):
    return {
        "source": source,
        "city": city,
        "operation_name": operation,
        "price_rub": price,
        "includes_parts": False,
        "captured_at": "2026-05-21",
        "confidence": "medium",
    }


def _labor_time(source: str, hours: float, operation: str = "замена рулевой рейки"):
    return {
        "source": source,
        "operation_name": operation,
        "hours": hours,
        "public_source": True,
        "captured_at": "2026-05-21",
        "confidence": "medium",
    }


def test_exact_work_with_public_quotes_excludes_outlier_and_applies_markup():
    result = estimate_repair_work_cost(
        vehicle="BMW X5",
        vin="WBA00000000000000",
        work_items=["поменять рулевую рейку"],
        quotes_json=[
            _quote("sto-a", 10000),
            _quote("sto-b", 11000),
            _quote("sto-c", 12000),
            _quote("sto-d", 13000),
            _quote("sto-e", 99000),
        ],
    )

    assert result["ok"] is True
    assert result["read_only"] is True
    assert result["crm_write_allowed"] is False
    assert result["normalized_operations"][0]["normalized_name"] == "замена рулевой рейки"
    assert result["operation_estimates"][0]["sample"]["valid_count"] == 4
    assert result["operation_estimates"][0]["sample"]["excluded_outliers"][0]["price_rub"] == 99000
    assert result["operation_estimates"][0]["pricing_method"] == "krasnoyarsk_market_mean"
    assert result["market_average_rub"] == 11500
    assert result["russia_average_rub"] == 11500
    assert result["autostop_price_rub"] == 16700
    assert result["confidence"] == "medium"
    assert result["labor_time_confidence"] == "blocked"
    assert result["pricing_basis"]["regional_methods"] == {
        "krasnoyarsk": "labor_only_mean_x_1_45",
        "saint_petersburg": "labor_only_mean_x_1_15",
    }
    assert result["pricing_basis"]["target_market_region"] == "krasnoyarsk"
    assert result["pricing_basis"]["regional_preference"] == ["krasnoyarsk", "saint_petersburg"]
    assert result["pricing_basis"]["methods_combined"] is False
    assert result["formula"]["krasnoyarsk_market"]["markup_multiplier"] == 1.45
    assert result["playbook"] == ".agents/skills/manage-autostop-store/SKILL.md"
    assert result["source_catalog"] == "docs/agent/automotive_sources/automotive_repair_sources_catalog.json"


def test_less_than_three_prices_returns_low_confidence_without_confident_price():
    result = estimate_repair_work_cost(
        vehicle="Toyota Camry",
        work_items=["замена свечей"],
        quotes_json=[
            _quote("sto-a", 3000, operation="замена свечей"),
            _quote("sto-b", 4000, operation="замена свечей"),
        ],
    )

    assert result["confidence"] == "low"
    assert result["russia_average_rub"] is None
    assert result["autostop_price_rub"] is None
    assert result["operation_estimates"][0]["weak_average_rub"] == 3500
    assert "regional_market_basis_or_valid_public_labor_hours" in result["missing_context"]


def test_internal_labor_snapshot_remains_a_provisional_anchor():
    result = estimate_repair_work_cost(
        vehicle="Toyota Camry",
        work_items=["диагностика ходовой"],
        auto_research=False,
        internal_experience_json={
            "schema_version": "autostop_service_labor_experience_v1",
            "labor_baselines": [
                {
                    "operation_key": "диагностика_подвески",
                    "operation_name": "диагностика подвески",
                    "category": "diagnostics",
                    "sample_count": 3,
                    "p25_rub": 900,
                    "p75_rub": 1100,
                    "recommended_anchor_rub": 1000,
                }
            ],
        },
    )

    operation = result["operation_estimates"][0]
    assert operation["internal_experience"]["available"] is True
    assert operation["recommendation_basis"] == "internal_experience_provisional"


def test_quotes_with_parts_are_excluded_from_labor_only_sample():
    result = estimate_repair_work_cost(
        vehicle="Lexus RX",
        work_items=["замена масла"],
        quotes_json=[
            _quote("sto-a", 1500, operation="замена масла"),
            _quote("sto-b", 1700, operation="замена масла"),
            _quote("sto-c", 1600, operation="замена масла"),
            {
                "source": "sto-with-parts",
                "city": "Москва",
                "operation_name": "замена масла",
                "price_rub": 8500,
                "includes_parts": True,
                "captured_at": "2026-05-21",
            },
        ],
    )

    assert result["market_sample"]["valid_count"] == 3
    assert result["market_sample"]["invalid_count"] == 1
    assert result["russia_average_rub"] == 1600
    assert result["autostop_price_rub"] == 2300


def test_quotes_without_explicit_labor_only_confirmation_are_excluded():
    result = estimate_repair_work_cost(
        vehicle="Toyota Camry",
        work_items=["замена масла"],
        quotes_json=[
            {"source": "sto-a", "operation_name": "замена масла", "price_rub": 2500},
            {"source": "sto-b", "operation_name": "замена масла", "price_rub": 3000},
            {"source": "sto-c", "operation_name": "замена масла", "price_rub": 3500},
        ],
        auto_research=False,
    )

    assert result["market_sample"]["valid_count"] == 0
    assert result["market_sample"]["invalid_count"] == 3
    assert result["autostop_price_rub"] is None
    invalid_quotes = result["operation_estimates"][0]["sample"]["invalid_quotes"]
    assert all("labor_only_not_confirmed" in quote["reasons"] for quote in invalid_quotes)


def test_complaint_without_work_is_diagnostic_first_not_final_repair_price():
    result = estimate_repair_work_cost(
        vehicle="Volkswagen Tiguan DSG",
        vin="WVG00000000000000",
        complaint="машина пинается при переключении",
        quotes_json=[
            _quote("sto-a", 2000, operation="диагностика трансмиссии"),
            _quote("sto-b", 2500, operation="диагностика трансмиссии"),
            _quote("sto-c", 3000, operation="диагностика трансмиссии"),
        ],
    )

    assert result["mode"] == "diagnostic_first"
    assert result["confidence"] == "low"
    assert result["russia_average_rub"] is None
    assert result["autostop_price_rub"] is None
    assert "confirmed_repair_work_items" in result["missing_context"]
    assert any("Не оценивать мехатроник" in action for action in result["next_actions"])


def test_public_labor_time_layer_adds_cross_check_without_changing_price_formula():
    result = estimate_repair_work_cost(
        vehicle="BMW X5",
        vin="WBA00000000000000",
        work_items=["поменять рулевую рейку"],
        quotes_json={
            "quotes": [
                _quote("sto-a", 10000),
                _quote("sto-b", 11000),
                _quote("sto-c", 12000),
            ],
            "labor_time_sample": [
                _labor_time("public-time-a", 3.5),
                _labor_time("public-time-b", 4.0),
            ],
        },
    )

    assert result["russia_average_rub"] == 11000
    assert result["autostop_price_rub"] == 16000
    assert result["operation_estimates"][0]["pricing_method"] == "krasnoyarsk_market_mean"
    assert result["labor_time_confidence"] == "medium"
    assert result["labor_time_average_hours"] == 3.75
    assert result["labor_time_cross_check"] == "ok"
    assert result["operation_estimates"][0]["labor_time_analysis"]["average_hours"] == 3.75


def test_saint_petersburg_market_is_used_only_when_krasnoyarsk_has_fewer_than_three_quotes():
    result = estimate_repair_work_cost(
        vehicle="Toyota Camry",
        work_items=["замена свечей"],
        quotes_json=[
            _quote("krsk-a", 5000, operation="замена свечей"),
            _quote("krsk-b", 6000, operation="замена свечей"),
            _quote("spb-a", 10000, operation="замена свечей", city="Санкт-Петербург"),
            _quote("spb-b", 12000, operation="замена свечей", city="СПб"),
            _quote("spb-c", 14000, operation="замена свечей", city="Saint Petersburg"),
        ],
        auto_research=False,
    )

    operation = result["operation_estimates"][0]
    assert operation["pricing_method"] == "saint_petersburg_market_mean"
    assert operation["market_region"] == "saint_petersburg"
    assert operation["market_average_rub"] == 12000
    assert operation["autostop_price_rub"] == 13800
    assert operation["sample"]["regional_counts"] == {"krasnoyarsk": 2, "saint_petersburg": 3}


def test_krasnoyarsk_market_has_priority_and_pricing_methods_are_not_combined():
    result = estimate_repair_work_cost(
        vehicle="Toyota Camry",
        work_items=["замена свечей"],
        quotes_json={
            "quotes": [
                _quote("krsk-a", 9000, operation="замена свечей"),
                _quote("krsk-b", 10000, operation="замена свечей"),
                _quote("krsk-c", 11000, operation="замена свечей"),
                _quote("spb-a", 18000, operation="замена свечей", city="Санкт-Петербург"),
                _quote("spb-b", 20000, operation="замена свечей", city="Санкт-Петербург"),
                _quote("spb-c", 22000, operation="замена свечей", city="Санкт-Петербург"),
            ],
            "labor_time_sample": [
                _labor_time("public-time-a", 8, operation="замена свечей"),
                _labor_time("public-time-b", 8, operation="замена свечей"),
            ],
        },
        auto_research=False,
    )

    operation = result["operation_estimates"][0]
    assert operation["pricing_method"] == "krasnoyarsk_market_mean"
    assert operation["market_average_rub"] == 10000
    assert operation["autostop_price_rub"] == 14500
    assert operation["pricing_formula"] == {
        "basis": "arithmetic_mean(selected_city_labor_only_quotes_after_outlier_filter)",
        "multiplier": 1.45,
        "methods_combined": False,
    }
    assert result["formula"]["methods_combined"] is False


def test_saint_petersburg_target_prefers_saint_petersburg_when_both_regions_qualify():
    result = estimate_repair_work_cost(
        vehicle="Toyota Camry",
        city="Санкт-Петербург",
        work_items=["замена свечей"],
        quotes_json=[
            _quote("krsk-a", 9000, operation="замена свечей"),
            _quote("krsk-b", 10000, operation="замена свечей"),
            _quote("krsk-c", 11000, operation="замена свечей"),
            _quote("spb-a", 18000, operation="замена свечей", city="Санкт-Петербург"),
            _quote("spb-b", 20000, operation="замена свечей", city="Санкт-Петербург"),
            _quote("spb-c", 22000, operation="замена свечей", city="Санкт-Петербург"),
        ],
        auto_research=False,
    )

    operation = result["operation_estimates"][0]
    assert operation["pricing_method"] == "saint_petersburg_market_mean"
    assert operation["market_average_rub"] == 20000
    assert operation["autostop_price_rub"] == 23000
    assert operation["regional_preference"] == ["saint_petersburg", "krasnoyarsk"]
    assert result["pricing_basis"]["target_market_region"] == "saint_petersburg"


def test_auto_research_false_keeps_offline_quote_mode_and_stable_research_keys():
    result = estimate_repair_work_cost(
        vehicle="Toyota Camry",
        work_items=["замена свечей"],
        quotes_json=[
            _quote("sto-a", 3000, operation="замена свечей"),
            _quote("sto-b", 4000, operation="замена свечей"),
            _quote("sto-c", 3500, operation="замена свечей"),
        ],
        auto_research=False,
    )

    assert result["russia_average_rub"] == 3500
    assert result["autostop_price_rub"] == 5100
    assert result["sources_checked"][0]["status"] == "disabled"
    assert result["research"]["enabled"] is False


def test_related_operations_return_overlap_adjustments():
    result = estimate_repair_work_cost(
        vehicle="Audi Q3",
        work_items=["замена опорных подшипников стоек", "снять стойку переднюю"],
        quotes_json=[
            _quote("sto-a", 5000, operation="замена опорных подшипников передних стоек"),
            _quote("sto-b", 5500, operation="замена опорных подшипников передних стоек"),
            _quote("sto-c", 6000, operation="замена опорных подшипников передних стоек"),
        ],
    )

    assert result["overlap_adjustments"]
    assert any(item["type"] == "possible_included_remove_install" for item in result["overlap_adjustments"])


def test_work_pricing_redacts_raw_vehicle_identifiers_from_output_and_research(monkeypatch):
    raw_vin = "WBA00000000000000"
    raw_chassis = "ES19999999"
    captured = {}

    def fake_research(**kwargs):
        captured["vehicle_context"] = kwargs["vehicle_context"]
        return {
            "enabled": True,
            "policy": kwargs["labor_time_policy"],
            "access_mode": "public_web_only",
            "search_queries": {"labor_prices": [], "labor_times": []},
            "quotes": [],
            "labor_time_sample": [],
            "sources_checked": [],
            "warnings": [],
        }

    monkeypatch.setattr("autostop_manager.work_pricing.collect_public_work_pricing_research", fake_research)

    result = estimate_repair_work_cost(
        vehicle=f"BMW X5 {raw_vin}",
        vin=raw_vin,
        chassis=raw_chassis,
        work_items=["замена рулевой рейки"],
    )

    rendered = json.dumps(result, ensure_ascii=False)
    research_context = json.dumps(captured["vehicle_context"], ensure_ascii=False)

    assert raw_vin not in rendered
    assert raw_chassis not in rendered
    assert raw_vin not in research_context
    assert raw_chassis not in research_context
    assert result["vehicle_context"]["vin"] == "WBA***000"
    assert result["vehicle_context"]["chassis"] == "ES1***999"
    assert result["privacy"]["raw_vehicle_identifier_redacted_from_output"] is True


def test_price_fallback_is_used_when_price_rub_is_empty():
    result = estimate_repair_work_cost(
        vehicle="Toyota Camry",
        work_items=["замена свечей"],
        quotes_json=[
            {**_quote("sto-a", 0, operation="замена свечей"), "price_rub": None, "price": 3000},
            {**_quote("sto-b", 0, operation="замена свечей"), "price_rub": None, "price": 4000},
            {**_quote("sto-c", 0, operation="замена свечей"), "price_rub": None, "price": 3500},
        ],
        auto_research=False,
    )

    assert result["market_sample"]["valid_count"] == 3
    assert result["market_sample"]["invalid_count"] == 0
    assert result["russia_average_rub"] == 3500
    assert result["autostop_price_rub"] == 5100


def test_labor_time_fallback_uses_range_or_norm_hours_when_hours_is_empty():
    result = estimate_repair_work_cost(
        vehicle="BMW X5",
        vin="WBA00000000000000",
        work_items=["поменять рулевую рейку"],
        quotes_json={
            "quotes": [
                _quote("sto-a", 10000),
                _quote("sto-b", 11000, city="Красноярск"),
                _quote("sto-c", 12000, city="Новосибирск"),
            ],
            "labor_time_sample": [
                {**_labor_time("public-time-a", 0), "hours": None, "range_hours": [3.5, 4.0]},
                {**_labor_time("public-time-b", 0), "labor_hours": None, "norm_hours": "4-5 ч"},
            ],
        },
        auto_research=False,
    )

    assert result["labor_time_sample"]["valid_count"] == 2
    assert result["labor_time_sample"]["invalid_count"] == 0
    assert result["labor_time_range_hours"] == [3.5, 5.0]
    assert result["labor_time_average_hours"] == 4.12
    operation = result["operation_estimates"][0]
    assert operation["labor_time_analysis"]["average_hours"] == 4.12
    assert operation["pricing_method"] == "public_labor_hours"
    assert operation["price_qualifier"] == "estimate"
    assert operation["market_average_rub"] is None
    assert operation["autostop_price_rub"] == 16500
    assert operation["confidence"] == "medium"
    assert operation["pricing_formula"] == {
        "basis": "valid_public_labor_hours_x_hourly_rate",
        "average_hours": 4.12,
        "hourly_rate_rub": 4000,
        "methods_combined": False,
    }
    assert result["market_average_rub"] is None
    assert result["autostop_price_rub"] == 16500


def test_labor_hour_fallback_does_not_blend_with_internal_experience():
    result = estimate_repair_work_cost(
        vehicle="Toyota Camry",
        work_items=["замена масла"],
        quotes_json={
            "quotes": [],
            "labor_time_sample": [
                _labor_time("public-time-a", 2, operation="замена масла"),
                _labor_time("public-time-b", 2, operation="замена масла"),
            ],
        },
        auto_research=False,
        internal_experience_json={
            "schema_version": "autostop_service_labor_experience_v1",
            "labor_baselines": [
                {
                    "operation_key": "замена_масла",
                    "operation_name": "замена масла",
                    "category": "general",
                    "sample_count": 10,
                    "p25_rub": 18000,
                    "p75_rub": 22000,
                    "recommended_anchor_rub": 20000,
                }
            ],
        },
    )

    operation = result["operation_estimates"][0]
    assert operation["pricing_method"] == "public_labor_hours"
    assert operation["pricing_formula"]["methods_combined"] is False
    assert operation["autostop_price_rub"] == 8000
    assert operation["recommended_price_rub"] == 8000
    assert operation["recommended_range_rub"] == [8000, 8000]
    assert operation["recommendation_basis"] == "public_labor_hours_exclusive"
    assert operation["internal_experience"]["available"] is True
    assert operation["internal_experience"]["selected"]["recommended_anchor_rub"] == 20000
    assert operation["internal_experience"]["applied_to_price"] is False
    assert "internal_closed_repair_order_experience" not in operation["evidence_source_families"]
    assert result["autostop_price_rub"] == 8000
    assert result["recommended_total_works_rub"] == 8000
    assert result["formula"]["methods_combined"] is False


def test_single_public_labor_time_row_is_about_only_and_not_a_confident_total():
    result = estimate_repair_work_cost(
        vehicle="Toyota Camry",
        work_items=["замена масла"],
        quotes_json={
            "quotes": [],
            "labor_time_sample": [_labor_time("public-time-a", 2, operation="замена масла")],
        },
        auto_research=False,
        use_internal_experience=False,
    )

    operation = result["operation_estimates"][0]
    assert operation["pricing_method"] == "public_labor_hours"
    assert operation["price_qualifier"] == "about"
    assert operation["autostop_price_rub"] == 8000
    assert operation["confidence"] == "low"
    assert result["autostop_price_rub"] is None
    assert result["total_works_rub"] is None
    assert result["confidence"] == "low"
    assert result["formula"]["public_labor_hours"]["hourly_rate_rub"] == 4000


def test_unconfirmed_public_labor_time_does_not_activate_hourly_fallback():
    result = estimate_repair_work_cost(
        vehicle="Toyota Camry",
        work_items=["замена масла"],
        quotes_json={
            "quotes": [],
            "labor_time_sample": [
                {
                    "source": "unconfirmed-time-source",
                    "operation_name": "замена масла",
                    "hours": 2,
                    "captured_at": "2026-05-21",
                    "confidence": "medium",
                }
            ],
        },
        auto_research=False,
        use_internal_experience=False,
    )

    operation = result["operation_estimates"][0]
    assert operation["pricing_method"] is None
    assert operation["autostop_price_rub"] is None
    assert result["labor_time_sample"]["valid_count"] == 0
    assert result["labor_time_sample"]["invalid_count"] == 1
    assert result["autostop_price_rub"] is None
