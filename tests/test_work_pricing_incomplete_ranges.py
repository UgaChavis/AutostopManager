"""Incomplete public time ranges must not become precise price evidence."""

import pytest

from autostop_manager import work_pricing as pricing


@pytest.mark.parametrize(
    "value",
    [
        "1-",
        "1–NaN",
        "1-неизвестно",
        "1 – ?",
        "1 — ?",
        "1 час – ?",
        "1 ч. - неизвестно",
        "1-нормо-час - ?",
        "–2",
        "NaN–2",
        "0-2",
        "1-0",
        "NaN",
        0,
        [1],
        ["1"],
        [1, 2, 3],
        "1-2-?",
        "1-2-3",
        "1–2 – 3 часа",
        "1–2 часа – ?",
        "1–2 ч до ?",
        "1-2-нормо-часа - ?",
        "от 1 до 2 до ?",
        "от 1 до 2 до 3",
        "1-2 до 3",
        "от 1 до ?",
        "от ? до 2",
        "1 до NaN",
        "от 1 часа до ?",
        "до 2 часов",
        "от 1 часа",
    ],
)
@pytest.mark.parametrize("field", ["hours", "range_hours"])
def test_incomplete_range_cannot_create_price_from_two_sources(value, field, monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("no acquisition expected")

    monkeypatch.setattr(pricing, "_load_labor_experience", denied)
    rows = [
        {
            "source": f"synthetic-{i}.example",
            "operation_name": "замена масла",
            field: value,
            "public_source": True,
            "confidence": "high",
        }
        for i in range(2)
    ]
    result = pricing.estimate_repair_work_cost(
        make="DEMO",
        work_items=["замена масла"],
        quotes_json={"labor_time_sample": rows},
        auto_research=False,
        use_internal_experience=False,
    )
    analysis = result["operation_estimates"][0]["labor_time_analysis"]
    assert analysis["valid_count"] == 0
    assert analysis["average_hours"] is None
    assert result["recommended_total_works_rub"] is None


@pytest.mark.parametrize(
    "value, expected",
    [
        ("1", (1.0, [1.0, 1.0])),
        ("1 час", (1.0, [1.0, 1.0])),
        ("норма: 2 часа", (2.0, [2.0, 2.0])),
        ("норма – 2 часа", (2.0, [2.0, 2.0])),
        ("1-нормо-часа", (1.0, [1.0, 1.0])),
        ("1-час", (1.0, [1.0, 1.0])),
        ("1-нормо-час", (1.0, [1.0, 1.0])),
        ("1 ч – 2 ч", (1.5, [1.0, 2.0])),
        ("1 час — замена масла", (1.0, [1.0, 1.0])),
        ("1–2 часа — замена масла", (1.5, [1.0, 2.0])),
        ("1–2 часа", (1.5, [1.0, 2.0])),
        ("1-2-нормо-часа", (1.5, [1.0, 2.0])),
        ("1—2 часа", (1.5, [1.0, 2.0])),
        ("от 1 до 2 часов", (1.5, [1.0, 2.0])),
        ("от 1 часа до 2 часов", (1.5, [1.0, 2.0])),
        ("1e0 до 2e0 часов", (1.5, [1.0, 2.0])),
        ([1, 2], (1.5, [1.0, 2.0])),
        ("1e-1 часа", (0.1, [0.1, 0.1])),
        ("1e0-2e0 часа", (1.5, [1.0, 2.0])),
    ],
)
def test_complete_values_and_units_are_unchanged(value, expected):
    assert pricing._coerce_hours(value) == expected


def test_labeled_single_value_in_range_field_remains_valid():
    assert pricing._coerce_hours("норма – 2 часа", range_field=True) == (2.0, [2.0, 2.0])
