from __future__ import annotations

import autostop_manager.work_pricing_research as research


def test_labor_only_flags_require_explicit_snippet_evidence():
    assert research._labor_only_flags("Замена масла — только работа 2 500 руб.") == (False, True)
    assert research._labor_only_flags("Работа с запчастями 12 000 руб.") == (True, False)
    assert research._labor_only_flags("Не только работа — пакет 12 000 руб.") == (True, False)
    assert research._labor_only_flags("Работа 2 500 руб., запчасти не включены") == (False, True)
    assert research._labor_only_flags("Замена масла от 3 000 руб.") == (None, False)


def test_public_research_preserves_unknown_and_parts_included_quotes(monkeypatch):
    def fake_search(_query, *, timeout_seconds):
        assert timeout_seconds == 2
        return {
            "results": [
                {"source": "labor.example", "title": "Только работа 2 500 руб.", "snippet": "без запчастей"},
                {"source": "bundle.example", "title": "Комплект 12 000 руб.", "snippet": "работа с запчастями"},
                {"source": "unknown.example", "title": "Цена от 3 000 руб.", "snippet": "подробнее на сайте"},
            ]
        }

    monkeypatch.setattr(research, "_ddg_search", fake_search)

    result = research.collect_public_work_pricing_research(
        vehicle_context={"vehicle": "Toyota Camry"},
        operations=[{"normalized_name": "замена масла"}],
        city="Красноярск",
        auto_research=True,
        timeout_seconds=2,
    )

    quotes = {quote["source"]: quote for quote in result["quotes"]}
    assert quotes["labor.example"]["includes_parts"] is False
    assert quotes["labor.example"]["labor_only"] is True
    assert quotes["bundle.example"]["includes_parts"] is True
    assert quotes["bundle.example"]["labor_only"] is False
    assert quotes["unknown.example"]["includes_parts"] is None
    assert quotes["unknown.example"]["labor_only"] is False
    assert "Public labor-only price quotes were not found automatically." not in result["warnings"]


def test_quote_deduplication_keeps_conflicting_parts_evidence_conservative():
    rows = [
        {
            "source": "sto.example",
            "operation_name": "замена масла",
            "price_rub": 2_500,
            "includes_parts": False,
            "labor_only": True,
        },
        {
            "source": "sto.example",
            "operation_name": "замена масла",
            "price_rub": 2_500,
            "includes_parts": True,
            "labor_only": False,
        },
    ]

    assert research._dedupe_quote_rows(rows) == [
        {
            "source": "sto.example",
            "operation_name": "замена масла",
            "price_rub": 2_500,
            "includes_parts": True,
            "labor_only": False,
        }
    ]


def test_research_warns_when_prices_exist_but_none_are_confirmed_labor_only(monkeypatch):
    monkeypatch.setattr(
        research,
        "_ddg_search",
        lambda *_args, **_kwargs: {
            "results": [
                {"source": "bundle.example", "title": "Работа с запчастями 12 000 руб.", "snippet": ""},
                {"source": "unknown.example", "title": "Цена 3 000 руб.", "snippet": ""},
            ]
        },
    )

    result = research.collect_public_work_pricing_research(
        vehicle_context={"vehicle": "Toyota Camry"},
        operations=[{"normalized_name": "замена масла"}],
        city="Красноярск",
        auto_research=True,
    )

    assert result["quotes"]
    assert "Public labor-only price quotes were not found automatically." in result["warnings"]


def test_research_warns_after_conflicting_duplicate_quotes_are_merged(monkeypatch):
    monkeypatch.setattr(
        research,
        "_ddg_search",
        lambda *_args, **_kwargs: {
            "results": [
                {"source": "sto.example", "title": "Только работа 2 500 руб.", "snippet": ""},
                {"source": "sto.example", "title": "Работа с запчастями 2 500 руб.", "snippet": ""},
            ]
        },
    )

    result = research.collect_public_work_pricing_research(
        vehicle_context={"vehicle": "Toyota Camry"},
        operations=[{"normalized_name": "замена масла"}],
        city="Красноярск",
        auto_research=True,
    )

    assert len(result["quotes"]) == 1
    assert result["quotes"][0]["includes_parts"] is True
    assert result["quotes"][0]["labor_only"] is False
    assert "Public labor-only price quotes were not found automatically." in result["warnings"]
