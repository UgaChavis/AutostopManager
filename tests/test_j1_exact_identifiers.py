from __future__ import annotations

import pytest

from autostop_manager import j1_fetch, j1_research as j1, j1_vin_store


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
