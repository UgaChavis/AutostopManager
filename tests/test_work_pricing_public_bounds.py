"""One-sided public time limits must not become precise price evidence."""

import pytest

from autostop_manager import work_pricing as pricing, work_pricing_research as research


@pytest.fixture(autouse=True)
def no_acquisition(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("unexpected external or private acquisition")

    monkeypatch.setattr(research, "urlopen", denied)
    monkeypatch.setattr(pricing, "_load_labor_experience", denied)


def estimate_snippets(text):
    operation = "замена масла"
    rows = []
    for index in range(2):
        row = {
            "source": f"synthetic-{index}.example",
            "url": f"https://synthetic-{index}.example/labor",
            "title": "Замена масла",
            "snippet": text,
        }
        parsed, _ = research.parse_public_work_evidence(
            row, "норма времени", [{"normalized_name": operation}], "labor_times"
        )
        rows.extend(parsed)
    result = pricing.estimate_repair_work_cost(
        make="DEMO",
        work_items=[operation],
        quotes_json={"labor_time_sample": rows},
        auto_research=False,
        use_internal_experience=False,
    )
    return rows, result["operation_estimates"][0]


@pytest.mark.parametrize(
    "snippet",
    [
        "до 2 часов",
        "от 1 часа",
        "Норма времени: до 2 часов",
        "от 1 н/ч",
        "до 2 ч.",
        "до 2 нормо-часов",
        "до 1–2 часов",
        "от 1–2 часов",
        "от 1 до ? часов",
    ],
)
def test_one_sided_or_incomplete_time_does_not_create_a_price(snippet):
    rows, operation = estimate_snippets(snippet)
    assert not any(row["operation_name"] for row in rows)
    assert operation["labor_time_analysis"]["valid_count"] == 0
    assert operation["autostop_price_rub"] is None


@pytest.mark.parametrize(
    "snippet, expected_range, price", [("2 часа", [2.0, 2.0], 8000), ("1–2 часа", [1.0, 2.0], 6000)]
)
def test_precise_time_and_complete_range_keep_operation_binding(snippet, expected_range, price):
    rows, operation = estimate_snippets(snippet)
    assert len(rows) == 2
    assert all(row["operation_name"] == "замена масла" and row["range_hours"] == expected_range for row in rows)
    assert operation["labor_time_analysis"]["valid_count"] == 2
    assert operation["autostop_price_rub"] == price


def test_unknown_endpoint_before_another_operation_does_not_bind():
    operations = [{"normalized_name": "замена масла"}, {"normalized_name": "замена свечей"}]
    row = {
        "source": "synthetic.example",
        "title": "Прайс",
        "snippet": "Замена масла: 1 час — неизвестно, замена свечей: 3 часа",
    }
    rows, _ = research.parse_public_work_evidence(row, "норма", operations, "labor_times")
    assert [(row["operation_name"], row["range_hours"]) for row in rows] == [("замена свечей", [3.0, 3.0])]
