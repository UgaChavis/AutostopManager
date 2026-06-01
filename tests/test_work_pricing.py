from __future__ import annotations

from autostop_manager.work_pricing import estimate_repair_work_cost


def _quote(source: str, price: int, operation: str = "замена рулевой рейки", city: str = "Москва"):
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
            _quote("sto-b", 11000, city="Красноярск"),
            _quote("sto-c", 12000, city="Новосибирск"),
            _quote("sto-d", 13000, city="Екатеринбург"),
            _quote("sto-e", 99000, city="Москва"),
        ],
    )

    assert result["ok"] is True
    assert result["read_only"] is True
    assert result["crm_write_allowed"] is False
    assert result["normalized_operations"][0]["normalized_name"] == "замена рулевой рейки"
    assert result["operation_estimates"][0]["sample"]["valid_count"] == 4
    assert result["operation_estimates"][0]["sample"]["excluded_outliers"][0]["price_rub"] == 99000
    assert result["russia_average_rub"] == 11500
    assert result["autostop_price_rub"] == 17300
    assert result["confidence"] == "medium"
    assert result["labor_time_confidence"] == "blocked"
    assert result["pricing_basis"]["secondary"] == "public_labor_time_plausibility_layer"


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
    assert "at_least_3_comparable_labor_only_public_prices" in result["missing_context"]


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
    assert result["autostop_price_rub"] == 2400


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
                _quote("sto-b", 11000, city="Красноярск"),
                _quote("sto-c", 12000, city="Новосибирск"),
            ],
            "labor_time_sample": [
                _labor_time("public-time-a", 3.5),
                _labor_time("public-time-b", 4.0),
            ],
        },
    )

    assert result["russia_average_rub"] == 11000
    assert result["autostop_price_rub"] == 16500
    assert result["labor_time_confidence"] == "medium"
    assert result["labor_time_average_hours"] == 3.75
    assert result["labor_time_cross_check"] == "ok"
    assert result["operation_estimates"][0]["labor_time_analysis"]["average_hours"] == 3.75


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
    assert result["autostop_price_rub"] == 5300
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
