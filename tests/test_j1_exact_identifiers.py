from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from autostop_manager import j1_fetch, j1_identifiers, j1_research as j1, j1_vin_store


@pytest.fixture(autouse=True)
def public_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "private"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "0")
    monkeypatch.setattr(j1_vin_store, "prune", lambda: None)
    monkeypatch.setattr(j1, "_STOP", False)


def _install_sources(monkeypatch, sources):
    def fetch(url, **kwargs):
        kind, text = sources[url]
        result = j1_fetch._extract_static_content(
            kind, text.encode(), url, {"content-type": "text/html; charset=utf-8"}
        )
        return {**result, "url": url}

    monkeypatch.setattr(j1, "fetch_document", fetch)


def test_exact_parts_keep_boundaries_quotes_and_refresh_after_append(monkeypatch):
    guide = "https://example.org/guide"
    title = "https://example.org/title"
    extra = "https://example.org/extra"
    sources = {
        guide: (
            "text/html",
            '<h2 id="neighbor">Other model</h2><p>Neighbor only, AX-241-MP or AX-240-MP-B.</p>'
            '<h2 id="target">Applicable part</h2><p>Model reference AX 240 MP for the selected option.</p>',
        ),
        title: ("text/html", "<title>AX-240-MP</title><h2>Notes</h2><p>See the workshop reference for selection.</p>"),
        extra: ("text/plain", "A separately retained catalog lists AX‑240‑MP."),
    }
    _install_sources(monkeypatch, sources)
    job = j1.research_sources(
        source_urls=[guide, title], objective="Synthetic catalog test", idempotency_key="exact-parts-test"
    )["job_id"]
    j1.run_worker(once=True)
    first = j1.research_results(job, "AX240MP", limit=1, query_mode="part_number")
    assert first["total"] == 2 and first["next_cursor"] == 1
    hit = first["results"][0]
    assert hit["match_kind"] == "body" and (hit["page"], hit["source_url"]) == (2, guide + "#target")
    assert "AX 240 MP" in hit["excerpt"] and "Neighbor only" not in hit["excerpt"]

    same_query = j1.research_results(job, "AX‑240‑MP", cursor=1, query_mode="part_number")
    title_hit = same_query["results"][0]
    assert title_hit["match_kind"] == "title" and title_hit["url"] == title and "page" not in title_hit
    assert j1.research_results(job, "AX-241-MP", query_mode="part_number")["total"] == 1
    assert j1.research_results(job, "AX24", query_mode="part_number")["total"] == 0

    assert j1.research_sources(job_id=job, source_urls=[extra])["ok"]
    j1.run_worker(once=True)
    refreshed = j1.research_results(job, "AX240MP", query_mode="part_number")
    assert refreshed["total"] == 3 and {item["url"] for item in refreshed["results"]} == {guide, extra, title}


def test_spaced_hyphenated_part_matches_only_whole_identifier(monkeypatch):
    url = "https://example.org/catalog"
    body = "Catalog: AX - 240 - MP. Related item — BX ‑ 150 ‑ RT — for order."
    _install_sources(monkeypatch, {url: ("text/plain", body)})
    job = j1.research_sources(
        source_urls=[url], objective="Synthetic catalog test", idempotency_key="spaced-exact-parts"
    )["job_id"]
    j1.run_worker(once=True)

    for query in ("AX240MP", "BX150RT"):
        result = j1.research_results(job, query, query_mode="part_number")
        assert result["total"] == 1 and result["results"][0]["match_kind"] == "body"
    for query in ("AX240", "240MP", "BX150", "150RT"):
        assert j1.research_results(job, query, query_mode="part_number")["total"] == 0


def test_space_delimited_parts_match_only_complete_number(monkeypatch):
    url = "https://example.org/parts"
    _install_sources(
        monkeypatch,
        {
            url: (
                "text/html",
                "<p>Part number: AX 240 MP.</p><p>Article: 18 450 621.</p>"
                "<p>Part number: BZ320 RS.</p><p>Part number: DJ--19.</p><p>Use WY500 with care.</p>",
            )
        },
    )
    job = j1.research_sources(
        source_urls=[url], objective="Synthetic catalog test", idempotency_key="space-delimited-parts"
    )["job_id"]
    j1.run_worker(once=True)

    for query in ("AX240MP", "18450621", "BZ320RS", "DJ19", "WY500"):
        result = j1.research_results(job, query, query_mode="part_number")
        assert result["total"] == 1 and result["results"][0]["match_kind"] == "body"
    for query in ("AX", "240", "MP", "AX240", "240MP", "450", "18450", "450621", "BZ320", "RS", "DJ", "19"):
        assert j1.research_results(job, query, query_mode="part_number")["total"] == 0


def test_labeled_part_field_binds_components_without_merging_separate_numbers(monkeypatch):
    catalog, stock = "https://example.org/catalog", "https://example.org/stock"
    _install_sources(
        monkeypatch,
        {
            catalog: (
                "text/html",
                "<p>Part number: ax1 240b is available.</p><p>Артикул: ZXTDQ 8895.</p>",
            ),
            stock: ("text/plain", "Two stocked codes AX24 BX34."),
        },
    )
    job = j1.research_sources(
        source_urls=[catalog, stock], objective="Synthetic lookup", idempotency_key="catalog-component-boundaries"
    )["job_id"]
    j1.run_worker(once=True)

    for code, expected in {
        "AX1240B": [catalog],
        "ZXTDQ8895": [catalog],
        "AX1": [],
        "240B": [],
        "8895": [],
        "AX24": [stock],
        "BX34": [stock],
        "AX24BX34": [],
    }.items():
        result = j1.research_results(job, code, query_mode="part_number")
        assert result["ok"] and [item["url"] for item in result["results"]] == expected


def test_dtc_match_distinguishes_cohort_counts_from_suffixes(monkeypatch):
    count, extended, variant = (f"https://example.org/{name}" for name in ("count", "extended", "variant"))
    _install_sources(
        monkeypatch,
        {
            count: ("text/plain", "DTC P1A27 — 12 / 70 vehicles in the cohort."),
            extended: ("text/plain", "DTC P1A27-002 is a distinct manufacturer variant."),
            variant: ("text/plain", "Use P1A27 – 12 of the intermittent fault group."),
        },
    )
    job = j1.research_sources(
        source_urls=[count, extended, variant], objective="Synthetic DTC variants", idempotency_key="dtc-counts"
    )["job_id"]
    j1.run_worker(once=True)

    for code, expected in {"P1A27": [count], "P1A27-12": [variant], "P1A27-00": []}.items():
        result = j1.research_results(job, code, query_mode="dtc")
        assert result["ok"] and [item["url"] for item in result["results"]] == expected


@pytest.mark.parametrize("prefix", ("", "synthetic-document-identifier@example.org "))
def test_clipped_title_cannot_manufacture_a_base_dtc_match(monkeypatch, prefix):
    url = "https://example.org/long-title"
    title_prefix = prefix + "Workshop guide P1A26. "
    raw_title = title_prefix + "x" * (200 - len(title_prefix) - len(" P1A27")) + " P1A27-00"
    _install_sources(
        monkeypatch,
        {url: ("text/html", f"<title>{raw_title}</title><h2 id='guide'>Workshop guide</h2><p>Read the guide.</p>")},
    )
    job = j1.research_sources(source_urls=[url], objective="Synthetic clipped title", idempotency_key="clipped-title")[
        "job_id"
    ]
    j1.run_worker(once=True)

    base = j1.research_results(job, "P1A27", query_mode="dtc")
    assert base["ok"] and base["total"] == 0
    subcode = j1.research_results(job, "P1A27-00", query_mode="dtc")
    assert subcode["ok"] and subcode["total"] == int(bool(prefix))
    complete = j1.research_results(job, "P1A26", query_mode="dtc")
    assert complete["ok"] and complete["results"][0]["match_kind"] == "title"


def test_retained_offsets_from_older_matcher_are_rebuilt(monkeypatch):
    url = "https://example.org/retained-catalog"
    _install_sources(monkeypatch, {url: ("text/plain", "Part number: AX - 240 - MP.")})
    job = j1.research_sources(
        source_urls=[url], objective="Synthetic catalog test", idempotency_key="retained-offsets"
    )["job_id"]
    j1.run_worker(once=True)
    with j1._db() as conn, conn:
        document_id = conn.execute("SELECT id FROM documents WHERE job_id=?", (job,)).fetchone()[0]
        conn.execute("ALTER TABLE identifier_queries DROP COLUMN matcher_version")
        conn.execute("INSERT INTO identifier_queries VALUES(?,?,?,?)", (job, "part_number", "240MP", 1))
        conn.execute("INSERT INTO identifier_matches VALUES(?,?,?,?,?,?)", (job, "part_number", document_id, 0, 0, 18))

    for _ in range(2):
        assert j1.research_results(job, "240MP", query_mode="part_number")["total"] == 0
    assert j1.research_results(job, "AX240MP", query_mode="part_number")["total"] == 1


def test_cold_exact_scan_does_not_block_worker_or_publish_stale_offsets(monkeypatch):
    first, second = "https://example.org/first", "https://example.org/second"
    _install_sources(
        monkeypatch,
        {
            first: ("text/plain", "Workshop guide P1A27."),
            second: ("text/plain", "Second guide with a different procedure for P1A27."),
        },
    )
    job = j1.research_sources(source_urls=[first], objective="Synthetic diagnostic test", idempotency_key="exact-scan")[
        "job_id"
    ]
    j1.run_worker(once=True)
    scanned = Event()
    release_scan = Event()
    original_positions = j1_identifiers._positions

    def paused_positions(*args):
        scanned.set()
        assert release_scan.wait(timeout=10)
        yield from original_positions(*args)

    def append_source():
        assert j1.research_sources(job_id=job, source_urls=[second])["ok"]
        with j1._db() as conn:
            assert j1._claim_job(conn) == job
        j1._work_job(job)

    monkeypatch.setattr(j1_identifiers, "_positions", paused_positions)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(j1.research_results, job, "P1A27", query_mode="dtc")
        try:
            assert scanned.wait(timeout=5)
            pool.submit(append_source).result(timeout=3)
        finally:
            release_scan.set()
        old_snapshot = reading.result(timeout=5)

    assert [item["url"] for item in old_snapshot["results"]] == [first]
    with j1._db() as conn:
        assert not conn.execute("SELECT 1 FROM identifier_queries WHERE job_id=?", (job,)).fetchone()
    refreshed = j1.research_results(job, "P1A27", query_mode="dtc")
    assert {item["url"] for item in refreshed["results"]} == {first, second}
    with j1._db() as conn:
        assert conn.execute("SELECT document_count FROM identifier_queries WHERE job_id=?", (job,)).fetchone()[0] == 2


def test_cache_write_failure_keeps_exact_snapshot_result(monkeypatch):
    url = "https://example.org/cache-fault"
    _install_sources(monkeypatch, {url: ("text/plain", "Workshop guide P1A27.")})
    job = j1.research_sources(
        source_urls=[url], objective="Synthetic diagnostic test", idempotency_key="exact-cache-fault"
    )["job_id"]
    j1.run_worker(once=True)
    with j1._db() as conn:
        conn.execute(
            """CREATE TRIGGER fail_offset_cache BEFORE INSERT ON identifier_matches
               BEGIN SELECT RAISE(ABORT, 'simulated cache failure'); END"""
        )
    result = j1.research_results(job, "P1A27", query_mode="dtc")
    assert result["ok"] and [item["url"] for item in result["results"]] == [url]
    with j1._db() as conn:
        assert not conn.execute("SELECT 1 FROM identifier_queries WHERE job_id=?", (job,)).fetchone()


def test_exact_dtc_baseline_and_suffixes_keep_automotive_document_variants(monkeypatch):
    documents = {code: f"https://example.org/{code}" for code in ("P1A27", "P1A27-00", "P1A27-01", "P1A28")}
    _install_sources(
        monkeypatch,
        {
            url: ("text/plain", f"Workshop reference: {code} — Diagnostic procedure. " + "Common workshop text. " * 20)
            for code, url in documents.items()
        },
    )
    monkeypatch.setattr(
        j1, "search_public", lambda *_args, **_kwargs: ([{"url": url} for url in documents.values()], "fixture")
    )
    job = j1.start_research(
        objective="Synthetic diagnostic lookup",
        automotive_context={"model": "Ficta", "dtc": "P1A27"},
    )["job_id"]
    j1.run_worker(once=True)
    for code, url in documents.items():
        result = j1.research_results(job, code, query_mode="dtc")
        assert [(row["url"], row["status"]) for row in result["results"]] == [(url, "fetched")]
    assert j1.research_results(job, "P1A27‑00", query_mode="dtc")["total"] == 1
    assert j1.research_results(job, "P1A27-999", query_mode="dtc")["error"]["code"] == "query_invalid_or_sensitive"
    assert j1.research_results(job, "0000‑0000‑0000‑00000", query_mode="part_number")["error"]["code"] == (
        "query_invalid_or_sensitive"
    )
