"""Exercise the bounded automotive follow-up with an offline public worker."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from autostop_manager import j1_research as j1
from autostop_manager import j1_vin_store


CONTEXT = {
    "make": "Ficta",
    "model": "Norvale",
    "year": 2021,
    "engine": "MX204",
    "system": "actuator feedback",
    "dtc": "P1A27",
}
FOLLOW_UP = "actuator feedback service manual"
MANUAL = "https://static.nhtsa.gov/odi/tsbs/synthetic/follow-up-manual"
SCOPE = "This bulletin applies to model year 2021 Ficta Norvale vehicles with engine MX204."
SUBJECT = "P1A27 means a connector feedback fault. First inspect the actuator terminal lock."


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "vin"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "0")
    monkeypatch.setattr(j1_vin_store, "prune", lambda: None)
    monkeypatch.setattr(j1, "_STOP", False)


def _start(max_pages=4, queries=None):
    started = j1.start_research("Investigate the synthetic actuator", queries, max_pages, automotive_context=CONTEXT)
    assert started["ok"], started
    return started["job_id"]


def _run(job_id):
    with j1._db() as conn:
        assert j1._claim_job(conn) == job_id
    j1._work_job(job_id)
    return j1.research_status(job_id)


def _follow_ups(status):
    return [row["query"] for row in status["query_plan"] if row["intent"] == "automatic_follow_up"]


def _hit(url):
    return ([{"url": url, "source": "searxng"}], "searxng")


def _manual(url):
    return {"ok": True, "url": url, "kind": "text", "text": f"{SCOPE} {SUBJECT}"}


@pytest.mark.parametrize("strong_initial", [False, True])
def test_automotive_follow_up_can_use_last_slot_after_initial_hits_fill_capacity(monkeypatch, strong_initial):
    searches, downloads = [], []

    def search(query, **_kwargs):
        searches.append(query)
        if query == FOLLOW_UP or (strong_initial and len(searches) == 1):
            return _hit(MANUAL)
        return _hit(f"https://example.org/failed-{len(searches)}")

    def fetch(url, **_kwargs):
        downloads.append(url)
        return _manual(url) if url == MANUAL else {"ok": False, "error": "access_restricted"}

    monkeypatch.setattr(j1, "search_public", search)
    monkeypatch.setattr(j1, "fetch_document", fetch)
    job_id = _start()
    initial = [row["query"] for row in j1.research_status(job_id)["query_plan"]]
    status = _run(job_id)
    assert status["status"] == "completed"
    assert (status["pages_total"], status["pages_unavailable"], status["pages_fetched"]) == (4, 3, 1)
    assert searches == (initial if strong_initial else [*initial[:3], FOLLOW_UP])
    assert downloads.index(MANUAL) == (0 if strong_initial else 3)
    assert _follow_ups(status) == ([] if strong_initial else [FOLLOW_UP])
    report = j1.research_report(job_id)["report"]
    assert report["evidence_outcome"]["state"] == "relevant"
    assert report["fitment_confirmed"] is False


@pytest.mark.parametrize("max_pages", [1, 2, 3])
def test_small_full_budgets_keep_every_initial_page(max_pages, monkeypatch):
    searches = []

    def search(query, **_kwargs):
        searches.append(query)
        return _hit(f"https://example.org/failed-{len(searches)}")

    monkeypatch.setattr(j1, "search_public", search)
    monkeypatch.setattr(j1, "fetch_document", lambda *_args, **_kwargs: {"ok": False})
    job_id = _start(max_pages)
    initial = [row["query"] for row in j1.research_status(job_id)["query_plan"]]
    status = _run(job_id)
    assert status["pages_total"] == max_pages
    assert searches == initial[:max_pages]
    assert _follow_ups(status) == []


def test_four_explicit_queries_can_use_all_four_pages(monkeypatch):
    searches = []
    explicit = [f"Check synthetic terminal procedure {number}" for number in range(4)]

    def search(query, **_kwargs):
        searches.append(query)
        return _hit(MANUAL if query == explicit[-1] else f"https://example.org/unavailable-{len(searches)}")

    monkeypatch.setattr(j1, "search_public", search)
    monkeypatch.setattr(j1, "fetch_document", lambda url, **_: _manual(url) if url == MANUAL else {"ok": False})
    status = _run(_start(4, explicit))

    assert searches == explicit
    assert (status["pages_total"], status["pages_fetched"], status["queries_skipped"]) == (4, 1, 1)
    assert _follow_ups(status) == []


@pytest.mark.parametrize("action", ["cancel", "add_query"])
def test_follow_up_releases_write_lock_and_rechecks_concurrent_user_action(monkeypatch, action):
    entered, release = Event(), Event()
    original_assessment = j1.assess_applicability
    weak_document = "https://static.nhtsa.gov/odi/tsbs/synthetic/other-model-year"
    user_query = "Synthetic actuator wiring diagram with terminal locations"
    searches = []

    def assess(context, passages):
        retained = tuple(passages)
        assert retained
        entered.set()
        assert release.wait(10), "Test did not release the evidence scan"
        return original_assessment(context, retained)

    def search(query, **_kwargs):
        searches.append(query)
        if len(searches) == 1:
            return _hit(weak_document)
        if query in {user_query, FOLLOW_UP}:
            return _hit(MANUAL)
        return _hit(f"https://example.org/unavailable-{len(searches)}")

    def fetch(url, **_kwargs):
        if url == weak_document:
            return {**_manual(url), "text": f"{SCOPE.replace('2021', '2020')} {SUBJECT}"}
        return _manual(url) if url == MANUAL else {"ok": False}

    monkeypatch.setattr(j1, "assess_applicability", assess)
    monkeypatch.setattr(j1, "search_public", search)
    monkeypatch.setattr(j1, "fetch_document", fetch)
    job_id = _start(4)
    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(_run, job_id)
        try:
            assert entered.wait(5), "Worker did not start the follow-up evidence scan"
            if action == "cancel":
                response = pool.submit(j1.research_cancel, job_id).result(timeout=3)
                assert response["status"] == "cancelled"
            else:
                response = pool.submit(j1.research_add_queries, job_id, [user_query]).result(timeout=3)
                assert response["added"] == 1
            assert not worker.done()
        finally:
            release.set()
        status = worker.result(timeout=5)

    assert _follow_ups(status) == []
    if action == "cancel":
        assert (status["status"], status["pages_total"], len(searches)) == ("cancelled", 3, 3)
    else:
        assert (status["status"], status["pages_total"], status["pages_fetched"]) == ("completed", 4, 2)
        assert len(searches) == 4
        assert searches[-1] == user_query


@pytest.mark.parametrize("mode", ["legacy", "duplicate", "full_query_budget", "cancelled"])
def test_follow_up_preserves_stored_jobs_duplicate_queries_query_cap_and_cancellation(mode, monkeypatch):
    searches = []
    job_id = _start()

    def search(query, **_kwargs):
        searches.append(query)
        if mode == "cancelled":
            assert j1.research_cancel(job_id)["status"] == "cancelled"
        return [], "search_no_results"

    monkeypatch.setattr(j1, "search_public", search)
    if mode == "legacy":
        with j1._db() as conn:
            conn.execute("ALTER TABLE jobs DROP COLUMN automatic_follow_up_query")
    if mode == "duplicate":
        assert j1.research_add_queries(job_id, [FOLLOW_UP.upper()])["added"] == 1
    if mode == "full_query_budget":
        total = j1.research_status(job_id)["queries_total"]
        manual = [f"Additional service evidence request {index}" for index in range(j1.AUTOMOTIVE_MAX_QUERIES - total)]
        assert j1.research_add_queries(job_id, manual)["queries_total"] == j1.AUTOMOTIVE_MAX_QUERIES

    status = _run(job_id)
    assert _follow_ups(status) == []
    assert FOLLOW_UP not in searches
    assert status["status"] == ("cancelled" if mode == "cancelled" else "completed")
    assert len(searches) == (1 if mode == "cancelled" else status["queries_total"])


@pytest.mark.parametrize(("reserve", "new_manual"), [(False, False), (True, False), (True, True)])
def test_automatic_query_resumes_with_a_reserved_or_unused_page(monkeypatch, reserve, new_manual):
    searches = []
    extra = "Synthetic connector service reference"

    def search(query, **_kwargs):
        searches.append(query)
        if query == FOLLOW_UP and searches.count(query) == 1:
            j1._STOP = True
            return [], "search_interrupted"
        if query == FOLLOW_UP or (new_manual and query == extra):
            return _hit(MANUAL)
        if reserve:
            return _hit(f"https://example.org/failed-{len(searches)}")
        return [], "search_no_results"

    def fetch(url, **_kwargs):
        return _manual(url) if url == MANUAL else {"ok": False, "error": "access_restricted"}

    monkeypatch.setattr(j1, "search_public", search)
    monkeypatch.setattr(j1, "fetch_document", fetch)
    job_id = _start(4 if reserve else 2)
    initial_pages = 3 if reserve else 0
    status = _run(job_id)
    assert (status["status"], status["pages_total"]) == ("queued", initial_pages)
    assert _follow_ups(status) == [FOLLOW_UP]

    if new_manual:
        assert j1.research_add_queries(job_id, [extra])["added"] == 1
    monkeypatch.setattr(j1, "_STOP", False)
    status = _run(job_id)
    assert (status["status"], status["pages_total"]) == ("completed", initial_pages + 1)
    assert _follow_ups(status) == [FOLLOW_UP]
    assert searches.count(FOLLOW_UP) == (1 if new_manual else 2)
    assert searches[-2:] == [FOLLOW_UP, extra if new_manual else FOLLOW_UP]
    if not reserve:
        assert j1.research_add_queries(job_id, [extra])["added"] == 1
        after = _run(job_id)
        assert _follow_ups(after) == [FOLLOW_UP]
        assert searches[-2:] == [FOLLOW_UP, extra]
