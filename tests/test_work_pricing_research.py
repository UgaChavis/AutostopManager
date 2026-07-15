from __future__ import annotations

from urllib.error import URLError

from autostop_manager import work_pricing_research as research


class _FakeResponse:
    def __init__(self, payload: str):
        self.payload = payload.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return False

    def read(self, _limit: int = -1) -> bytes:
        return self.payload


def _operation() -> dict[str, object]:
    return {
        "input": "замена рулевой рейки",
        "normalized_name": "замена рулевой рейки",
        "aliases": ["рулевая рейка"],
    }


def test_ddg_search_extracts_safe_compact_results(monkeypatch):
    html = """
    <div class="result results_links">
      <a rel="nofollow" class="result__a" href="https://example.test/service">Прайс СТО</a>
      <a class="result__snippet">Замена рулевой рейки — 12 000 руб.</a>
    </div></div>
    """
    monkeypatch.setattr(research, "urlopen", lambda request, timeout=4: _FakeResponse(html))

    result = research._ddg_search("замена рулевой рейки цена")

    assert result["results"] == [
        {
            "source": "example.test",
            "title": "Прайс СТО",
            "url": "https://example.test/service",
            "snippet": "Замена рулевой рейки — 12 000 руб.",
        }
    ]


def test_public_research_collects_price_and_labor_time_rows(monkeypatch):
    def fake_search(query: str, *, timeout_seconds: int):
        is_time_query = "нормо" in query or "норма времени" in query
        snippet = (
            "Замена рулевой рейки: 2,5-3,0 нормо-часа"
            if is_time_query
            else "Замена рулевой рейки, стоимость работы 12 000 руб."
        )
        return {
            "query": query,
            "results": [
                {
                    "source": "sto.example",
                    "title": "Прайс",
                    "url": "https://sto.example/prices",
                    "snippet": snippet,
                }
            ],
        }

    monkeypatch.setattr(research, "_ddg_search", fake_search)

    result = research.collect_public_work_pricing_research(
        vehicle_context={"make": "BMW", "model": "X5"},
        operations=[_operation()],
        city="Красноярск",
        auto_research=True,
    )

    assert result["quotes"][0]["price_rub"] == 12_000
    assert result["labor_time_sample"][0]["hours"] == 2.75
    assert result["labor_time_sample"][0]["range_hours"] == [2.5, 3.0]
    assert all(item["status"] == "ok" for item in result["sources_checked"])


def test_public_research_turns_network_errors_into_source_status(monkeypatch):
    def fail_search(_query: str, *, timeout_seconds: int):
        raise URLError("offline")

    monkeypatch.setattr(research, "_ddg_search", fail_search)

    result = research.collect_public_work_pricing_research(
        vehicle_context={"vehicle": "Toyota Camry"},
        operations=[_operation()],
        city="Красноярск",
        auto_research=True,
    )

    assert result["quotes"] == []
    assert result["labor_time_sample"] == []
    assert all(item["status"] == "error" for item in result["sources_checked"])
    assert all(item["error"] == "URLError" for item in result["sources_checked"])
    assert len(result["warnings"]) == 2


def test_public_research_disabled_is_network_free(monkeypatch):
    monkeypatch.setattr(
        research,
        "_ddg_search",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network must stay disabled")),
    )

    result = research.collect_public_work_pricing_research(
        vehicle_context={},
        operations=[_operation()],
        city="Красноярск",
        auto_research=False,
    )

    assert result["enabled"] is False
    assert result["sources_checked"] == [
        {"source_id": "public_web_search", "status": "disabled", "reason": "auto_research_false"}
    ]


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
