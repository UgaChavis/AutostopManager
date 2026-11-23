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

    monkeypatch.setattr(j1, "_STOP", False)
    _work(job_id)
    assert calls == [
        ("search", "first query"),
        ("fetch", "A1"),
        ("search", "first query"),
        ("search", "second query"),
        ("fetch", "B1"),
    ]
    assert j1.research_status(job_id)["queries_done"] == 2


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
