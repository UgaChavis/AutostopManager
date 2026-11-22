"""Offline checks for the supplied-URL research queue."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from uuid import UUID

import pytest

from autostop_manager import j1_fetch, j1_research as j1
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


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost./manual",
        "https://vehicle.internal./manual",
        "http://127.1/manual",
        "https://invalid%20authority.example.org/manual",
        "https://example.org/path with spaces",
        "https://example.org/manual" + " " * 2048,
        "https://example.org/?vehicle=WBA00000+000000000",
    ],
)
def test_supplied_urls_check_the_public_identity_before_storage(url):
    result = j1.research_sources(objective="Public sources", source_urls=[url], idempotency_key="request-1")
    assert result["error"]["code"] == "source_urls_invalid_or_sensitive"
    assert not j1._db_path().parent.exists()


def test_source_objective_is_safe_after_whitespace_normalization():
    result = j1.research_sources(
        objective="Compare WBA00000  000000000",
        source_urls=["https://example.org/manual"],
        idempotency_key="request-1",
    )
    assert result["error"]["code"] == "objective_invalid_or_sensitive"
    assert not j1._db_path().parent.exists()


def test_supplied_sources_only_allow_unicast_addresses(monkeypatch):
    address = "224.0.0.1"
    response = j1.research_sources(
        objective="Public sources", source_urls=[f"http://{address}/manual"], idempotency_key="request-1"
    )
    assert response["error"]["code"] == "source_urls_invalid_or_sensitive"
    monkeypatch.setattr(
        j1_fetch.socket, "getaddrinfo", lambda *_args, **_kwargs: [(None, None, None, "", (address, 80))]
    )
    with pytest.raises(ValueError, match="unsafe_dns_answer"):
        j1_fetch._public_address("example.org", 80)


def test_expired_job_advertises_no_follow_up_budget_before_pruning():
    job_id = j1.research_sources(
        objective="Public sources", source_urls=["https://example.org/manual"], idempotency_key="request-1"
    )["job_id"]
    with j1._db() as conn:
        expired = (datetime.now(UTC) - timedelta(days=j1.RETENTION_DAYS + 1)).isoformat(timespec="seconds")
        conn.execute("UPDATE jobs SET created_at=? WHERE id=?", (expired, job_id))
        conn.commit()
    status = j1.research_status(job_id)
    assert status["pages_pending"] == 1 and status["pages_remaining"] == 0
    assert j1.research_sources(job_id=job_id, source_urls=["https://example.org/new"])["error"]["code"] == "job_expired"


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


def test_international_source_aliases_share_one_slot_and_preserve_the_submitted_receipt():
    iri = "https://пример.рф:80/сборка/%2F?q=РФ&version=%C0"
    host = "пример.рф".encode("idna").decode()
    uri = f"https://{host}:80/{quote('сборка')}/%2F?q={quote('РФ')}&version=%C0"
    aliases = [iri, uri.replace(quote("сборка"), quote("сборка").lower()), uri]
    request = {"objective": "Public sources", "idempotency_key": "request-1", "source_urls": aliases}
    receipt = j1.research_sources(**request)
    assert receipt["ok"] and receipt["added"] == receipt["sources_total"] == 1
    assert j1.research_sources(**request)["replayed"]
    assert j1.research_sources(**(request | {"source_urls": [uri]}))["error"]["code"] == "idempotency_conflict"

    job_id = receipt["job_id"]
    distinct = [uri.replace("/%2F?", "//?"), uri.replace("%C0", "%C1")]
    addition = j1.research_sources(job_id=job_id, source_urls=[uri, *distinct])
    assert addition["added"] == 2 and addition["sources_total"] == 3
    with j1._db(readonly=True) as conn:
        row = conn.execute(
            "SELECT url,canonical_url FROM documents WHERE job_id=? ORDER BY rowid", (job_id,)
        ).fetchone()
        assert tuple(row) == (iri, uri)


def test_appending_to_retained_job_recognizes_both_international_redirect_keys():
    source = "https://пример.рф/узел"
    final = "https://другой.рф/текст"
    job_id = j1.research_sources(objective="Public sources", idempotency_key="request-1", source_urls=[source])[
        "job_id"
    ]
    with j1._db() as conn:
        # The previous writer stored unquoted paths and Unicode DNS names after a redirect.
        conn.execute(
            "UPDATE documents SET requested_canonical_url=?,canonical_url=?,url=?,status='fetched' WHERE job_id=?",
            (source, final, final, job_id),
        )
        conn.execute("UPDATE jobs SET status='completed' WHERE id=?", (job_id,))
        conn.commit()
    wire_urls = [
        f"https://{host.encode('idna').decode()}/{quote(path)}"
        for host, path in (("пример.рф", "узел"), ("другой.рф", "текст"))
    ]
    addition = j1.research_sources(job_id=job_id, source_urls=wire_urls)
    assert addition["added"] == 0 and addition["sources_total"] == 1 and addition["status"] == "completed"


def test_supplied_sources_do_not_extend_the_legacy_query_queue():
    legacy = j1.start_research("Public sources", ["synthetic query"])
    result = j1.research_sources(job_id=legacy["job_id"], source_urls=["https://example.org/manual"])

    assert result["error"] == {"code": "job_not_sources_only"}
    status = j1.research_status(legacy["job_id"])
    assert status["query_plan"] == [{"query": "synthetic query", "intent": "manual"}]
    assert "acquisition_mode" not in status and "acquisition_mode" not in j1.research_report(legacy["job_id"])


@pytest.mark.parametrize("read", ["research_status", "research_report"])
def test_snapshot_does_not_mix_completed_job_with_concurrent_followup(monkeypatch, read):
    job_id = j1.research_sources(
        objective="Public sources", source_urls=["https://example.org/first"], idempotency_key="request-1"
    )["job_id"]
    original_db = j1._db
    with original_db() as conn:
        conn.execute("UPDATE documents SET status='fetched',body='Original source' WHERE job_id=?", (job_id,))
        conn.execute("UPDATE jobs SET status='completed' WHERE id=?", (job_id,))
        conn.commit()

    updates = []

    def followup(sql):
        if updates or "from documents" not in sql.lower():
            return
        updates.append(None)
        updates[0] = j1.research_sources(job_id=job_id, source_urls=["https://example.org/second"])
        with original_db() as writer:
            writer.execute("UPDATE documents SET status='failed' WHERE job_id=? AND status='pending'", (job_id,))
            writer.commit()

    @contextmanager
    def interleaved_db(**kwargs):
        with original_db(**kwargs) as conn:
            conn.set_trace_callback(followup)
            yield conn

    monkeypatch.setattr(j1, "_db", interleaved_db)
    result = getattr(j1, read)(job_id)
    assert updates and updates[0]["added"] == 1
    assert result["status"] == "completed"
    if read == "research_status":
        assert result["pages_total"] == 1
    else:
        assert result["report"]["unavailable"] == []
    assert j1.research_status(job_id)["pages_total"] == 2


def test_capacity_prune_preserves_reopened_job_and_its_searchable_evidence(monkeypatch):
    jobs = []
    for n in range(2):
        job_id = j1.research_sources(
            objective="Public sources", source_urls=[f"https://example.org/doc{n}"], idempotency_key=f"request-{n}"
        )["job_id"]
        with j1._db() as conn:
            created_at = (datetime.now(UTC) - timedelta(hours=2 - n)).isoformat(timespec="seconds")
            conn.execute("UPDATE jobs SET status='completed', created_at=? WHERE id=?", (created_at, job_id))
            conn.execute("UPDATE documents SET status='fetched',body='Actuator' WHERE job_id=?", (job_id,))
            conn.execute(
                "INSERT INTO documents_fts(job_id,document_id,title,body) SELECT job_id,id,title,body "
                "FROM documents WHERE job_id=?",
                (job_id,),
            )
            conn.commit()
        jobs.append(job_id)

    def compact(conn):
        if conn.execute("SELECT 1 FROM jobs WHERE id=?", (jobs[0],)).fetchone() is None:
            followup = j1.research_sources(job_id=jobs[1], source_urls=["https://example.org/new"])
            assert followup["ok"] and followup["status"] == "queued"
        return True

    monkeypatch.setattr(j1, "_compact_cache", compact)
    monkeypatch.setattr(j1, "_cache_size", lambda: j1.MAX_CORPUS_BYTES + 1)
    with j1._db() as conn:
        j1._prune(conn)
    assert j1.research_status(jobs[1])["status"] == "queued"
    assert j1.research_results(jobs[1], "Actuator")["total"] == 1
