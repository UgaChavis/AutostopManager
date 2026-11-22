from autostop_manager import j1_research as j1
from autostop_manager import j1_vin_store


def test_repeated_discovery_after_redirect_preserves_the_last_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "private"))
    monkeypatch.setattr(j1_vin_store, "prune", lambda: None)
    monkeypatch.setattr(j1, "_STOP", False)
    original = "https://example.org/bulletin"
    final = "https://example.org/current-bulletin"
    independent = "https://example.org/manual"
    fetched = []

    def search(query, **_kwargs):
        urls = [original] if query == "initial query" else [original + "?utm_source=two", final, independent]
        return [{"url": url} for url in urls], "synthetic"

    def fetch(url, **_kwargs):
        fetched.append(url)
        target = final if url.startswith(original) else url
        return {"ok": True, "url": target, "kind": "text", "text": "Evidence from " + target}

    monkeypatch.setattr(j1, "search_public", search)
    monkeypatch.setattr(j1, "fetch_document", fetch)
    job_id = j1.start_research("Read independent sources", ["initial query", "follow up query"], max_pages=2)["job_id"]
    with j1._db() as conn:
        assert j1._claim_job(conn) == job_id
    j1._work_job(job_id)

    assert fetched == [original, independent]
    assert {item["url"] for item in j1.research_results(job_id)["results"]} == {final, independent}
