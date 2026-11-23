"""Exercise public query planning and document admission with disposable state."""

import pytest

from autostop_manager import j1_research as j1
from autostop_manager import j1_vin_store


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "private"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "0")
    monkeypatch.delenv("AUTOSTOP_J1_SEARXNG_URL", raising=False)
    monkeypatch.setattr(j1_vin_store, "prune", lambda: None)
    monkeypatch.setattr(j1, "_STOP", False)


def _work(job_id):
    with j1._db() as connection:
        assert j1._claim_job(connection) == job_id
    j1._work_job(job_id)


def _research_inputs(monkeypatch):
    calls = []
    batches = {
        "first query": [("A2", 2), ("A1", 1), ("A3", 3)],
        "second query": [("B1", 1)],
        "third query": [("C1", 1)],
    }

    def search(query, **_kwargs):
        calls.append(("search", query))
        return [
            {"url": f"https://example.org/{name}", "search_rank": rank, "source": "searxng"}
            for name, rank in batches[query]
        ], "searxng"

    def fetch(url, **_kwargs):
        name = url.rsplit("/", 1)[-1]
        calls.append(("fetch", name))
        if name == "B1":
            return {"ok": False, "error": "http_not_found"}
        return {"ok": True, "kind": "text", "url": url, "text": f"Synthetic evidence from document {name}."}

    monkeypatch.setattr(j1, "search_public", search)
    monkeypatch.setattr(j1, "fetch_document", fetch)
    monkeypatch.setattr(j1, "_utcnow", lambda: "2026-11-23T00:01:02+00:00")
    return calls


def test_page_capacity_reserves_first_hit_for_each_query_and_skips_remaining_searches(monkeypatch):
    calls = _research_inputs(monkeypatch)
    job_id = j1.start_research("Synthetic repair evidence", ["first query", "second query", "third query"], 2)["job_id"]
    _work(job_id)

    assert calls == [("search", "first query"), ("fetch", "A1"), ("search", "second query"), ("fetch", "B1")]
    status = j1.research_status(job_id)
    assert status["status"] == "completed"
    assert (status["pages_total"], status["pages_unavailable"]) == (2, 1)
    assert (status["queries_total"], status["queries_done"], status["queries_failed"], status["queries_skipped"]) == (
        3,
        2,
        0,
        1,
    )
    assert status["search_failures"] == []
    assert status["coverage"]["search_providers"] == [{"provider": "searxng", "queries": 2, "done": 2, "failed": 0}]
    assert {item["code"] for item in j1.research_report(job_id)["report"]["limitations"]} >= {"page_budget_reached"}
    assert j1.research_add_queries(job_id, ["third query"])["added"] == 0
    assert j1.research_add_queries(job_id, ["fourth query"])["added"] == 1
    status = j1.research_status(job_id)
    assert (status["status"], status["queries_total"], status["queries_skipped"]) == ("completed", 4, 2)
    with j1._db() as connection:
        assert j1._claim_job(connection) is None
        assert connection.execute("SELECT COUNT(*) FROM query_candidates").fetchone()[0] == 0


def test_spare_capacity_is_backfilled_in_rank_order_after_first_hits(monkeypatch):
    calls = _research_inputs(monkeypatch)
    job_id = j1.start_research("Synthetic repair evidence", ["first query", "second query"], 4)["job_id"]
    _work(job_id)

    assert calls == [
        ("search", "first query"),
        ("fetch", "A1"),
        ("search", "second query"),
        ("fetch", "B1"),
        ("fetch", "A2"),
        ("fetch", "A3"),
    ]
    status = j1.research_status(job_id)
    assert (status["status"], status["pages_total"], status["queries_skipped"]) == ("completed", 4, 0)
    documents = j1.research_results(job_id)["results"]
    assert {row["url"].rsplit("/", 1)[-1]: row["search_rank"] for row in documents} == {
        "A1": 1,
        "A2": 2,
        "A3": 3,
        "B1": 1,
    }


def test_resumed_partial_query_keeps_other_queries_share_of_budget(monkeypatch):
    calls = _research_inputs(monkeypatch)
    search = j1.search_public

    def interrupted_search(query, **kwargs):
        results, provider = search(query, **kwargs)
        if calls == [("search", "first query")]:
            j1._STOP = True
            return results, "search_interrupted"
        return results, provider

    monkeypatch.setattr(j1, "search_public", interrupted_search)
    job_id = j1.start_research("Synthetic repair evidence", ["first query", "second query"], 2)["job_id"]
    _work(job_id)
    assert j1.research_status(job_id)["status"] == "queued"
    assert j1.research_status(job_id)["pages_total"] == 1
    with j1._db() as connection:
        recorded = connection.execute(
            "SELECT provider,error FROM queries WHERE job_id=? ORDER BY id", (job_id,)
        ).fetchone()
    assert tuple(recorded) == ("searxng", "search_interrupted")

    monkeypatch.setattr(j1, "_STOP", False)
    _work(job_id)
    assert calls == [
        ("search", "first query"),
        ("fetch", "A1"),
        ("search", "first query"),
        ("search", "second query"),
        ("fetch", "B1"),
    ]
    status = j1.research_status(job_id)
    assert (status["queries_done"], status["queries_partial"]) == (2, 0)
    assert status["coverage"]["search_providers"] == [{"provider": "searxng", "queries": 2, "done": 2, "failed": 0}]
    with j1._db() as connection:
        recorded = connection.execute(
            "SELECT provider,error FROM queries WHERE job_id=? ORDER BY id", (job_id,)
        ).fetchone()
    assert tuple(recorded) == ("searxng", "")


@pytest.mark.parametrize("untouched", [False, True])
def test_interrupted_search_at_capacity_counts_its_partial_result(monkeypatch, untouched):
    calls = _research_inputs(monkeypatch)
    search = j1.search_public

    def interrupted_search(query, **kwargs):
        results, _ = search(query, **kwargs)
        j1._STOP = True
        return results, "search_interrupted"

    monkeypatch.setattr(j1, "search_public", interrupted_search)
    queries = ["first query", "second query"] if untouched else ["first query"]
    job_id = j1.start_research("Synthetic repair evidence", queries, 1)["job_id"]
    _work(job_id)
    monkeypatch.setattr(j1, "_STOP", False)
    _work(job_id)

    assert calls == [("search", "first query"), ("fetch", "A1")]
    status = j1.research_status(job_id)
    assert status["schema"] == j1.SCHEMA
    assert (status["status"], status["pages_fetched"]) == ("completed", 1)
    assert (status["queries_done"], status["queries_failed"], status["queries_partial"], status["queries_skipped"]) == (
        0,
        0,
        1,
        int(untouched),
    )
    assert status["search_failures"] == []
    assert status["coverage"]["search_providers"] == [
        {"provider": "searxng", "queries": 1, "done": 0, "failed": 0, "partial": 1},
    ]
    limitations = {item["code"] for item in j1.research_report(job_id)["report"]["limitations"]}
    assert "search_incomplete" in limitations
    assert ("page_budget_reached" in limitations) == untouched


def test_interrupted_search_retains_provider_and_discloses_failed_retry(monkeypatch):
    calls = _research_inputs(monkeypatch)
    search = j1.search_public

    def search_with_failed_retry(query, **kwargs):
        if calls:
            return [], "search_timeout"
        results, _ = search(query, **kwargs)
        j1._STOP = True
        return results, "search_interrupted"

    monkeypatch.setattr(j1, "search_public", search_with_failed_retry)
    job_id = j1.start_research("Synthetic repair evidence", ["first query"], 2)["job_id"]
    _work(job_id)
    monkeypatch.setattr(j1, "_STOP", False)
    _work(job_id)

    status = j1.research_status(job_id)
    assert (status["pages_fetched"], status["queries_partial"], status["queries_failed"]) == (2, 1, 0)
    assert status["search_failures"] == [{"reason": "search_timeout", "count": 1}]
    assert status["coverage"]["search_providers"] == [
        {"provider": "searxng", "queries": 1, "done": 0, "failed": 0, "partial": 1},
    ]


def test_automotive_first_pass_keeps_room_for_followups():
    result = j1.start_research(
        "Synthetic part fitment",
        ["manufacturer application guide", "technical archive"],
        automotive_context={"make": "Astra Demo", "model": "Alpha", "part_number": "D-424242"},
    )
    assert result["query_count"] == 5
    job_id = result["job_id"]
    plan = j1.research_status(job_id)["query_plan"]
    assert [item["intent"] for item in plan[:2]] == ["manual", "manual"]
    assert all("D-424242" in item["query"] and "Alpha" in item["query"] for item in plan[2:])
    assert j1.research_add_queries(job_id, [f"source gap {number}" for number in range(7)])["queries_total"] == 12
    assert j1.research_add_queries(job_id, ["more evidence"])["error"]["code"] == "query_limit_reached"


@pytest.mark.parametrize("sources_only", [False, True])
def test_blank_optional_automotive_fields_are_treated_as_absent(sources_only):
    base = {"make": "Astra Demo", "model": "Alpha", "part_number": "D-424242"}
    context = {**base, "engine": "", "symptom": " \t ", "dtc": "", "market": "\n"}
    if sources_only:
        created = j1.research_sources(
            objective="Synthetic part fitment",
            idempotency_key="synthetic-optional-context",
            source_urls=["https://example.org/catalog"],
            automotive_context=context,
        )
    else:
        created = j1.start_research("Synthetic part fitment", automotive_context=context)
    assert created["ok"], created
    status = j1.research_status(created["job_id"])
    assert status["automotive_context"] == base
    assert ("queries_partial" in status) is not sources_only


@pytest.mark.parametrize("separator", ["-", "‐", "‑", "‒", "–", "—", "−"])
def test_automotive_dtc_list_normalizes_spacing_around_subcodes(separator):
    created = j1.start_research(
        "Synthetic repair evidence",
        automotive_context={"make": "Astra Demo", "model": "Alpha", "dtc": f"P0304 {separator} 01, U0100-02 / B1234"},
    )
    assert created["ok"], created
    status = j1.research_status(created["job_id"])
    assert status["automotive_context"]["dtc"] == "P0304-01, U0100-02, B1234"
    assert all("P0304-01, U0100-02, B1234" in item["query"] for item in status["query_plan"])


def test_query_schema_migrates_previous_cache_and_keeps_pending_jobs(monkeypatch):
    _research_inputs(monkeypatch)
    job_id = j1.start_research("Retained synthetic research", ["first query"], 2)["job_id"]
    with j1._db() as connection:
        connection.execute("DROP TABLE query_candidates")
        connection.execute("ALTER TABLE queries DROP COLUMN primary_admitted")
        connection.commit()

    assert j1.research_status(job_id)["queries_skipped"] == 0
    _work(job_id)
    status = j1.research_status(job_id)
    assert (status["status"], status["queries_done"], status["pages_fetched"]) == ("completed", 1, 2)


@pytest.mark.parametrize("dtc", [None, "P0304"])
def test_auto_search_uses_market_position_and_one_production_date_hint(dtc):
    context = {"make": "Astra Demo", "model": "Alpha", "year": 2024, "part_number": "D-424242", "dtc": dtc}
    for market, position, date in (
        ("Europe", "front left", "2024-02-05"),
        ("Japan", "rear right", "2024-10-05"),
    ):
        created = j1.start_research(
            "Synthetic part fitment",
            automotive_context={**context, "market": market, "position": position, "production_date": date},
        )
        plan = j1.research_status(created["job_id"])["query_plan"]
        assert len(plan) == 4
        assert all(market in item["query"] and position in item["query"] for item in plan)
        assert sum(date in item["query"] for item in plan) == 1
        assert j1.research_cancel(created["job_id"])["status"] == "cancelled"


def test_long_auto_search_context_preserves_distinct_intent_terms():
    context = {
        "make": "manufacturer " * 6,
        "model": "generation " * 9,
        "year": 2024,
        "engine": "powertrain " * 10,
        "system": "suspension " * 12,
        "part_number": "component " * 10,
        "market": "Europe",
        "position": "rear right",
    }
    created = j1.start_research("Synthetic part fitment", automotive_context=context)
    plan = j1.research_status(created["job_id"])["query_plan"]
    assert len(plan) == 4
    assert all(len(item["query"]) <= 500 and "Europe rear right" in item["query"] for item in plan)
    assert plan[0]["query"].endswith("OEM parts catalog fitment")
    assert plan[1]["query"].endswith("component manufacturer specifications")


def _stop_with_deferred_documents(monkeypatch, max_pages):
    _research_inputs(monkeypatch)
    fetch = j1.fetch_document

    def stop_after_fetch(url, **kwargs):
        result = fetch(url, **kwargs)
        j1._STOP = True
        return result

    monkeypatch.setattr(j1, "fetch_document", stop_after_fetch)
    job_id = j1.start_research("Synthetic repair evidence", ["first query"], max_pages)["job_id"]
    _work(job_id)
    assert j1.research_status(job_id)["status"] == "queued"
    return job_id


def test_prepare_rollback_passes_deferred_documents_to_the_legacy_queue(monkeypatch):
    job_id = _stop_with_deferred_documents(monkeypatch, 2)
    with j1._worker_lock():
        assert j1.prepare_rollback()["error"]["code"] == "j1_worker_active"

    assert j1.prepare_rollback() == {
        "ok": True,
        "schema": j1.SCHEMA,
        "jobs_reconciled": 1,
        "documents_queued": 1,
        "candidates_discarded": 1,
    }
    with j1._db() as connection:
        queued = connection.execute(
            "SELECT url,status FROM documents WHERE job_id=? ORDER BY search_rank", (job_id,)
        ).fetchall()
        assert [tuple(row) for row in queued] == [
            ("https://example.org/A1", "fetched"),
            ("https://example.org/A2", "pending"),
        ]
    assert j1.prepare_rollback()["jobs_reconciled"] == 0


def test_prepare_rollback_retains_deferred_urls_on_write_failure(monkeypatch):
    job_id = _stop_with_deferred_documents(monkeypatch, 3)
    with j1._db() as connection:
        connection.execute(
            """CREATE TRIGGER reject_last_candidate BEFORE INSERT ON documents
               WHEN NEW.search_rank=3 BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END"""
        )
        connection.commit()

    assert j1.prepare_rollback()["error"]["code"] == "j1_store_unavailable"
    with j1._db() as connection:
        records = connection.execute("SELECT rank FROM query_candidates ORDER BY rank").fetchall()
        assert [row["rank"] for row in records] == [2, 3]
        connection.execute("DROP TRIGGER reject_last_candidate")
        connection.commit()
    assert j1.research_status(job_id)["pages_total"] == 1
    assert j1.prepare_rollback()["documents_queued"] == 2
