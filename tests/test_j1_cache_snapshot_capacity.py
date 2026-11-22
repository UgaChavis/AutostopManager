from autostop_manager import j1_research as j1


def test_pinned_read_snapshot_does_not_evict_retained_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "public"))
    jobs = set()
    for index in range(2):
        result = j1.research_sources(
            objective="Technical sources",
            source_urls=["https://example.org/manual"],
            idempotency_key=f"request-{index}",
        )
        jobs.add(result["job_id"])
        with j1._db() as writer:
            writer.execute("UPDATE jobs SET status='completed' WHERE id=?", (result["job_id"],))
            writer.commit()

    with j1._db() as writer, j1._db(readonly=True) as reader:
        writer.execute("PRAGMA busy_timeout=10")
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        monkeypatch.setattr(j1, "MAX_CORPUS_BYTES", j1._cache_size() + 1024)
        reader.execute("BEGIN")
        assert {row[0] for row in reader.execute("SELECT id FROM jobs")} == jobs
        writer.execute("UPDATE jobs SET objective='Updated public question'")
        writer.commit()
        pinned_size = j1._cache_size()
        assert pinned_size > j1.MAX_CORPUS_BYTES

        j1._prune(writer)
        assert {row[0] for row in writer.execute("SELECT id FROM jobs")} == jobs
        assert j1._cache_size() == pinned_size

        reader.commit()
        j1._prune(writer)
        assert {row[0] for row in writer.execute("SELECT id FROM jobs")} == jobs
        assert j1._cache_size() <= j1.MAX_CORPUS_BYTES
