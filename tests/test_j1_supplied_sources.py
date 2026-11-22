"""Offline checks for the supplied-URL research queue."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from autostop_manager import j1_research as j1
from autostop_manager import j1_vin_store


@pytest.fixture(autouse=True)
def isolated_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "private"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "0")
    monkeypatch.setattr(j1, "_STOP", False)
    monkeypatch.setattr(j1_vin_store, "prune", lambda: None)


def test_unsafe_input_does_not_initialize_the_cache():
    synthetic_vin = "WBA" + "0" * 14
    private_url = "https://example.org/car?vin=" + synthetic_vin
    rejected = j1.research_sources(
        objective="Public sources",
        source_urls=["https://example.org/manual.pdf", private_url],
        idempotency_key="request-1",
    )

    assert rejected == {"ok": False, "schema": j1.SCHEMA, "error": {"code": "source_urls_invalid_or_sensitive"}}
    assert j1.research_sources(objective="Public sources", source_urls=["https://example.org/"])["error"] == {
        "code": "idempotency_key_invalid"
    }
    assert not j1._db_path().parent.exists()


@pytest.mark.parametrize("existing_cache", [False, True])
def test_start_receipt_serializes_competing_calls_and_checks_original_body(existing_cache):
    if existing_cache:
        with j1._db() as conn:
            conn.executescript(
                "ALTER TABLE jobs DROP COLUMN acquisition_mode; "
                "ALTER TABLE documents DROP COLUMN requested_canonical_url;"
            )
    request = {
        "objective": "Public sources",
        "source_urls": ["https://example.org/manual?utm_source=one", "https://example.org/manual"],
        "idempotency_key": "request-1",
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: j1.research_sources(**request), range(2)))

    assert all(response["ok"] for response in responses)
    assert sorted(response["added"] for response in responses) == [0, 1]
    assert {response["job_id"] for response in responses} == {responses[0]["job_id"]}
    assert sorted(response["replayed"] for response in responses) == [False, True]
    assert j1.research_sources(**(request | {"source_urls": ["https://example.org/manual"]}))["error"] == {
        "code": "idempotency_conflict"
    }


def test_supplied_sources_requeue_without_search_and_keep_redirect_identity(monkeypatch):
    original = "https://example.org/redirect"
    final = "https://example.org/manual"
    searches = []
    fetched = []

    def fetch(url, **_kwargs):
        fetched.append(url)
        return {"ok": True, "url": final, "title": "Technical source", "kind": "text", "text": "Synthetic text"}

    monkeypatch.setattr(j1, "search_public", lambda query, **_kwargs: (searches.append(query) or [], "synthetic"))
    monkeypatch.setattr(j1, "fetch_document", fetch)
    created = j1.research_sources(objective="Public sources", source_urls=[original], idempotency_key="request-1")
    job_id = created["job_id"]
    queued = j1.research_status(job_id)
    assert queued["acquisition_mode"] == "sources_only"
    assert queued["query_plan"] == queued["query_suggestions"] == []
    assert queued["queries_total"] == queued["queries_remaining"] == 0
    assert queued["pages_pending"] == 1 and queued["pages_remaining"] == j1.MAX_PAGES - 1
    assert j1.research_add_queries(job_id, ["search not allowed"])["error"] == {
        "code": "queries_not_allowed_for_sources_only"
    }
    with j1._db() as conn:
        assert j1._claim_job(conn) == job_id
    j1._work_job(job_id)

    assert searches == [] and fetched == [original]
    assert j1.research_status(job_id)["pages_pending"] == 0
    assert j1.research_results(job_id, query="Synthetic")["results"][0]["url"] == final
    report = j1.research_report(job_id)
    assert report["acquisition_mode"] == "sources_only"
    assert report["report"]["search_failures"] == []
    assert "search_access_limited" not in {item["code"] for item in report["report"]["limitations"]}
    duplicated = j1.research_sources(job_id=job_id, source_urls=[original + "?utm_source=two", final])
    assert duplicated["status"] == "completed" and duplicated["added"] == 0
    extended = j1.research_sources(job_id=job_id, source_urls=["https://example.org/second"])
    assert extended["status"] == "queued" and extended["added"] == 1 and extended["sources_total"] == 2
    replay = j1.research_sources(objective="Public sources", source_urls=[original], idempotency_key="request-1")
    assert replay["job_id"] == job_id and replay["replayed"] and replay["sources_total"] == 2


def test_cap_and_nonextendable_jobs_preserve_prior_sources(monkeypatch):
    monkeypatch.setattr(j1, "MAX_PAGES", 2)
    created = j1.research_sources(
        objective="Public sources", source_urls=["https://example.org/first"], idempotency_key="request-1"
    )
    job_id = created["job_id"]
    extra = ["https://example.org/second", "https://example.org/third"]

    assert j1.research_sources(job_id=job_id, source_urls=extra)["error"] == {"code": "source_limit_reached"}
    assert j1.research_status(job_id)["pages_total"] == 1
    assert j1.research_cancel(job_id)["status"] == "cancelled"
    cancelled = j1.research_status(job_id)
    assert cancelled["pages_remaining"] == cancelled["pages_pending"] == 0
    assert j1.research_sources(job_id=job_id, source_urls=extra[:1])["error"] == {"code": "job_not_extendable"}
    with j1._db() as conn:
        expired = (datetime.now(UTC) - timedelta(days=j1.RETENTION_DAYS + 1)).isoformat(timespec="seconds")
        conn.execute("UPDATE jobs SET created_at=? WHERE id=?", (expired, job_id))
        conn.commit()
    assert j1.research_sources(job_id=job_id, source_urls=extra[:1])["error"] == {"code": "job_expired"}


def test_supplied_order_controls_browser_budget(monkeypatch):
    first = "https://example.org/high-priority"
    second = "https://example.org/another-supplier"
    # UUID sort would claim the second document first.
    ids = iter(UUID(int=value) for value in (1, 30, 20))
    fetched = []

    def fetch(url, *, allow_browser):
        fetched.append((url, allow_browser))
        return {"ok": False, "error": "synthetic_missing", "browser_attempted": allow_browser}

    monkeypatch.setattr(j1, "uuid4", lambda: next(ids))
    monkeypatch.setattr(j1, "MAX_BROWSER_PAGES", 1)
    monkeypatch.setattr(j1, "fetch_document", fetch)
    created = j1.research_sources(objective="Public sources", source_urls=[first, second], idempotency_key="request-1")
    job_id = created["job_id"]
    with j1._db() as conn:
        assert j1._claim_job(conn) == job_id
    j1._work_job(job_id)

    assert fetched == [(first, True), (second, False)]


def test_supplied_sources_do_not_extend_the_legacy_query_queue():
    legacy = j1.start_research("Public sources", ["synthetic query"])
    result = j1.research_sources(job_id=legacy["job_id"], source_urls=["https://example.org/manual"])

    assert result["error"] == {"code": "job_not_sources_only"}
    status = j1.research_status(legacy["job_id"])
    assert status["query_plan"] == [{"query": "synthetic query", "intent": "manual"}]
    assert "acquisition_mode" not in status and "acquisition_mode" not in j1.research_report(legacy["job_id"])
