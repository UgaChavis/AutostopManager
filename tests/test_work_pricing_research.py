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
