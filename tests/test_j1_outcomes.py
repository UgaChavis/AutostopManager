"""Verify collection and evidence outcomes on retained public research jobs."""

from __future__ import annotations

from pathlib import Path

import pytest

from autostop_manager import j1_research as j1

URL = "https://static.nhtsa.gov/odi/tsbs/synthetic/outcome-coverage"
CONTEXT = {"make": "Ficta", "model": "Norvale", "year": 2021, "engine": "MX204", "dtc": "P1A27"}
SCOPE = "This bulletin applies to model year 2021 Ficta Norvale vehicles with engine MX204."
SUBJECT = "P1A27 means a connector feedback fault."


@pytest.fixture(autouse=True)
def isolated_outcomes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "vin"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "0")
    monkeypatch.setenv("AUTOSTOP_J1_SEARXNG_URL", "")
    monkeypatch.setattr(j1, "_STOP", False)
    monkeypatch.setattr(j1.signal, "signal", lambda *_args: None)


def _read_outcomes(job_id: str, *, lifecycle: str, collection: tuple[str, str], evidence: tuple[str, str]):
    status = j1.research_status(job_id)
    report = j1.research_report(job_id)
    assert status["ok"] and report["ok"]
    assert status["status"] == report["status"] == lifecycle
    assert (
        status["collection_outcome"]
        == report["collection_outcome"]
        == {
            "state": collection[0],
            "reason": collection[1],
        }
    )
    assert "evidence_outcome" not in status
    assert report["report"]["evidence_outcome"] == {"state": evidence[0], "reason": evidence[1]}
    return status, report["report"]


@pytest.mark.parametrize(
    ("query_results", "collection", "failure"),
    [
        (
            ("search_unavailable", "search_timeout"),
            ("search_failed", "all_recorded_searches_failed"),
            True,
        ),
        (("search_no_results", "search_no_results"), ("no_sources", "search_no_results"), False),
        (("search_no_results", "search_timeout"), ("no_sources", "search_no_results_with_failures"), True),
    ],
)
def test_public_outcome_distinguishes_failed_searches_from_clean_empty_results(
    monkeypatch, query_results, collection, failure
):
    queries = ["synthetic first query", "synthetic second query"]
    outcomes = dict(zip(queries, query_results, strict=True))
    monkeypatch.setattr(j1, "search_public", lambda query, **_kwargs: ([], outcomes[query]))
    started = j1.start_research("Check public evidence", queries)
    assert started["ok"], started
    j1.run_worker(once=True)
    status, report = _read_outcomes(
        started["job_id"],
        lifecycle="completed",
        collection=collection,
        evidence=("not_retrieved", "no_public_documents_retrieved"),
    )
    assert status["queries_failed"] == len(queries)
    assert bool(status["search_failures"]) is failure
    assert status["search_failures"] == report["search_failures"]
    assert not any(entry["reason"] == "search_no_results" for entry in report["search_failures"])
    assert any(entry["code"] == "search_access_limited" for entry in report["limitations"]) is failure


def _source_job(*, url: str = URL, context: dict | None = None) -> str:
    started = j1.research_sources(
        source_urls=[url],
        objective="Check the synthetic bulletin",
        idempotency_key="outcome-source-1",
        automotive_context=context,
    )
    assert started["ok"], started
    return started["job_id"]


@pytest.mark.parametrize(
    ("lifecycle", "collection"),
    [
        ("queued", ("in_progress", "collection_not_finished")),
        ("running", ("in_progress", "collection_not_finished")),
        ("cancelled", ("cancelled", "job_cancelled")),
        ("failed", ("failed", "job_failed")),
    ],
)
def test_pending_or_stopped_jobs_are_not_reported_as_empty_search(lifecycle, collection):
    job = _source_job()
    if lifecycle == "cancelled":
        assert j1.research_cancel(job)["ok"]
    elif lifecycle != "queued":
        with j1._db() as conn:
            assert j1._claim_job(conn) == job
            if lifecycle == "failed":
                conn.execute("UPDATE jobs SET status='failed',error='worker_failed' WHERE id=?", (job,))
                conn.commit()
    _read_outcomes(
        job, lifecycle=lifecycle, collection=collection, evidence=("not_retrieved", "no_public_documents_retrieved")
    )


def test_known_sources_unavailable_are_distinct_from_empty_search(monkeypatch):
    monkeypatch.setattr(j1, "fetch_document", lambda *_args, **_kwargs: {"ok": False, "error": "requires_human"})
    job = _source_job()
    j1.run_worker(once=True)
    status, report = _read_outcomes(
        job,
        lifecycle="completed",
        collection=("sources_unavailable", "known_sources_could_not_be_read"),
        evidence=("not_retrieved", "no_public_documents_retrieved"),
    )
    assert status["pages_unavailable"] == 1
    assert status["queries_failed"] == 0 and report["search_failures"] == []
    assert report["unavailable"][0]["reason"] == "requires_human"


@pytest.mark.parametrize(
    ("url", "context", "scope", "evidence"),
    [
        (URL, None, SCOPE, ("not_assessed", "no_automotive_context")),
        (URL, CONTEXT, SCOPE, ("relevant", "qualified_source_with_exact_context")),
        (URL, CONTEXT, SCOPE.replace("2021", "2020"), ("weak", "no_exact_context_match")),
        ("https://example.org/fixture", CONTEXT, SCOPE, ("weak", "no_qualified_exact_source")),
        (URL, CONTEXT, SCOPE[:-1] + " VIN WBA00000000000000.", ("weak", "no_exact_context_match")),
    ],
)
def test_retrieval_and_evidence_outcomes_are_separate(monkeypatch, url, context, scope, evidence):
    monkeypatch.setattr(
        j1,
        "fetch_document",
        lambda *_args, **_kwargs: {"ok": True, "url": url, "kind": "text", "text": f"{scope} {SUBJECT}"},
    )
    job = _source_job(url=url, context=context)
    j1.run_worker(once=True)
    _, report = _read_outcomes(
        job, lifecycle="completed", collection=("retrieved", "public_documents_retrieved"), evidence=evidence
    )
    assert report["evidence_confidence"]["retrieved_sources"] == 1
    assert report["fitment_confirmed"] is False
    assert "WBA00000000000000" not in str(report)
