"""Search and read stored public document fragments through the J1 worker."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import shutil
from threading import Barrier

import pytest

from autostop_manager import j1_fetch, j1_ocr, j1_research as j1


@pytest.fixture(autouse=True)
def isolated_corpus(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "vin"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "0")
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (True, 0))
    monkeypatch.setattr(j1_fetch, "_rate_limit", lambda *_args: None)
    monkeypatch.setattr(j1, "_STOP", False)


def _collect(monkeypatch, sources):
    def request(url, **_kwargs):
        content_type, body = sources[url]
        return 200, {"content-type": content_type}, body, url

    monkeypatch.setattr(j1_fetch, "_request_public", request)
    receipt = j1.research_sources(
        objective="Review public automotive service instructions",
        source_urls=list(sources),
        idempotency_key="source-fragment-tests-1",
    )
    assert receipt["ok"], receipt
    job_id = receipt["job_id"]
    with j1._db() as conn:
        assert j1._claim_job(conn) == job_id
    j1._work_job(job_id)
    assert j1.research_status(job_id)["status"] == "completed"
    return job_id


def test_search_reads_html_sections_and_prunes_duplicate_fragments(monkeypatch):
    private_anchor = "person%40example.test"
    html = f"""
        <nav><h2 id="nav">HiddenNavigation</h2></nav>
        <h2 id="background">Model and market</h2>
        <p>This section describes the affected model and its service history.</p>
        <template><h2>HiddenTemplate</h2><p>Unpublished information.</p></template>
        <h3 id="checking">Diagnostic procedure</h3>
        <p>Code C120702 is covered by this procedure. Verify prior software installation.</p>
        <h4 id="{private_anchor}">Related materials</h4>
        <p>Reference material for the workshop. Contact person@example.test.</p>
    """.encode()
    urls = ["https://example.org/bulletin", "https://example.com/copy"]
    job = _collect(monkeypatch, dict.fromkeys(urls, ("text/html", html)))

    results = j1.research_results(job, query="C120702", limit=1)
    assert results["total"] == 1 and results["next_cursor"] is None
    hit = results["results"][0]
    assert hit["page"] == 2 and hit["fragment_kind"] == "html_section"
    assert hit["section"] == "Diagnostic procedure"
    assert hit["source_url"] == f"{hit['url']}#checking"
    assert "[C120702]" in hit["excerpt"]
    assert j1.research_results(job, query="HiddenNavigation HiddenTemplate")["total"] == 0

    document = j1.research_document(job, hit["document_id"], page=hit["page"], max_chars=20)
    assert document["previous_page"] == 1 and document["next_page"] == 3
    assert document["source_url"] == hit["source_url"]
    assert document["text"] == "Diagnostic procedure"
    remainder = j1.research_document(job, hit["document_id"], page=2, offset=document["next_offset"])
    assert "C120702" in remainder["text"] and "Related materials" not in remainder["text"]
    next_section = j1.research_document(job, hit["document_id"], page=3)
    assert next_section["source_url"] == hit["url"]
    assert "example.test" not in str(next_section)

    with j1._db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents WHERE status='duplicate'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM document_fragments").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0] == 3
        conn.execute("UPDATE jobs SET created_at='2000-01-01T00:00:00+00:00'")
        conn.commit()
        j1._prune(conn)
        assert conn.execute("SELECT COUNT(*) FROM document_fragments").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0] == 0


def _pdf(*page_texts: str) -> bytes:
    pages = " ".join(f"{4 + i * 2} 0 R" for i in range(len(page_texts)))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{pages}] /Count {len(page_texts)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, page_text in enumerate(page_texts):
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
                f"/Contents {5 + i * 2} 0 R >>"
            ).encode()
        )
        stream = f"BT /F1 12 Tf 50 700 Td ({page_text}) Tj ET".encode()
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    pdf = b"%PDF-1.4\n"
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(pdf)
    pdf += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    pdf += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    return pdf + f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def test_html_citations_resolve_to_the_intended_element(monkeypatch):
    url = "https://example.org/catalog"
    body = b"""
        <div id="brakes">Chassis overview</div>
        <h2 id="brakes">Front axle</h2>AB241 applies to the front axle on this model.
        <h2 id="rear" id="unused">Rear axle</h2>XY761 applies to the rear axle.
        <div><template shadowrootmode="open"><h2 id="shadow">Steering</h2>
        <p>ZX913 identifies the steering variant.</p></template></div>
    """
    job = _collect(monkeypatch, {url: ("text/html", body)})
    for term, anchor, heading in (
        ("AB241", "", "Front axle"),
        ("XY761", "#rear", "Rear axle"),
        ("ZX913", "", "Steering"),
    ):
        hit = j1.research_results(job, query=term)["results"][0]
        assert hit["source_url"] == url + anchor and hit["section"] == heading
        assert f"[{term}]" in hit["excerpt"]


@pytest.mark.parametrize("preamble", [b"", b"<p>General service notes</p>"])
def test_html_overflow_keeps_tail_searchable_without_a_false_citation(monkeypatch, preamble):
    monkeypatch.setattr(j1_fetch, "MAX_PUBLIC_FRAGMENTS", 3)
    url = "https://example.org/bulletin"
    body = (
        preamble
        + b"""
        <h2 id="one">First system</h2><p>Alpha notes for the first system.</p>
        <h2 id="two">Second system</h2><p>Beta notes for the second system.</p>
        <h2 id="three">Third system</h2><p>Gamma notes for the third system.</p>
        <h2 id="four">Fourth system</h2><p>Delta notes for the fourth system.</p>
    """
    )
    job = _collect(monkeypatch, {url: ("text/html", body)})
    assert j1.research_results(job, query="Alpha")["results"][0]["source_url"] == url + "#one"
    hit = j1.research_results(job, query="Delta")["results"][0]
    assert (hit["page"], hit["section"], hit["source_url"]) == (3, None, url)
    assert "Fourth system" in j1.research_document(job, hit["document_id"], page=3)["text"]


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="pdftotext unavailable")
def test_pdf_redacts_vin_at_real_poppler_page_break_without_renumbering(monkeypatch):
    url = "https://example.org/bulletin.pdf?download=1"
    pdf = _pdf(
        "Workshop procedure reference for tested automotive components.",
        "WVWZZZ1KZ",
        "6W000001",
        "P0420: compare the diagnostic observations for the selected model and engine.",
    )
    job = _collect(monkeypatch, {url: ("application/octet-stream", pdf)})
    hit = j1.research_results(job, query="P0420")["results"][0]
    assert (hit["title"], hit["page"]) == ("bulletin.pdf", 4)
    assert hit["source_url"] == url + "#page=4"
    assert "[P0420]: compare the diagnostic observations" in hit["excerpt"]
    saved = j1.research_document(job, hit["document_id"])["text"]
    assert "[redacted]" in saved and "WVWZZZ1KZ" not in saved and "6W000001" not in saved


def test_html_bulletin_served_at_pdf_address_keeps_html_sections(monkeypatch):
    url = "https://example.org/workshop.pdf?lang=en"
    html = b'<h2 id="overview">Overview</h2><p>Reference material for the workshop.</p><h2 id="repair">Service</h2><p>P0420 diagnostic procedure.</p>'
    job = _collect(monkeypatch, {url: ("text/html", html)})
    hit = j1.research_results(job, query="P0420")["results"][0]
    assert hit["fragment_kind"] == "html_section" and hit["source_url"] == url + "#repair"


def test_html_title_is_document_metadata_and_table_cells_remain_searchable(monkeypatch):
    url = "https://example.org/torques"
    html = b"""
        <title>AX741 service bulletin</title><h2 id="front">Front axle</h2>
        <p>Published tightening specifications for the front axle assembly.</p>
        <table><tr><th>Fastener</th><th>Torque</th></tr><tr><td>BR218</td><td>125 Nm</td></tr></table>
    """
    job = _collect(monkeypatch, {url: ("text/html", html)})
    title = j1.research_results(job, query="AX741")["results"][0]
    assert "page" not in title and title["url"] == url
    fastener = j1.research_results(job, query="BR218")["results"][0]
    assert fastener["page"] == 1 and fastener["source_url"] == url + "#front"
    fragment = j1.research_document(job, fastener["document_id"], page=1)
    assert "BR218 125 Nm" in fragment["text"] and "AX741" not in fragment["text"]


def test_pdf_with_encoded_international_filename_is_searchable_by_its_title(monkeypatch):
    document = "https://example.org/%D0%A3%D0%B7%D0%B5%D0%BB.pdf"
    monkeypatch.setattr(j1_fetch, "_pdf_to_text", lambda _: "Published service procedure. " * 4 + "\f")
    job = _collect(monkeypatch, {document: ("application/pdf", b"%PDF-test")})
    result = j1.research_results(job, query="Узел")
    assert result["total"] == 1
    hit = result["results"][0]
    assert hit["title"] == "Узел.pdf" and hit["url"] == document and "page" not in hit


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="pdftotext unavailable")
def test_pdf_search_keeps_physical_page_number_across_a_blank_page(monkeypatch):
    url = "https://example.org/repair.pdf?download=1"
    pdf = _pdf(
        "Models covered by this diagnostic bulletin.",
        "",
        "C120702: check the wheel speed sensor and follow the documented procedure.",
        "Related documents and revision history.",
    )
    job = _collect(monkeypatch, {url: ("application/pdf", pdf)})
    results = j1.research_results(job, query="C120702")
    assert results["total"] == 1
    hit = results["results"][0]
    assert hit["page"] == 3 and hit["source_url"] == f"{url}#page=3"
    document = j1.research_document(job, hit["document_id"], page=3)
    assert document["fragment_kind"] == "pdf_page" and document["source_url"] == hit["source_url"]
    assert document["previous_page"] == 1 and document["next_page"] == 4
    assert "fragment_truncated" not in document
    assert document["text"].startswith("C120702:") and "Models covered" not in document["text"]
    blank_page = j1.research_document(job, hit["document_id"], page=2)
    assert blank_page["error"]["code"] == "document_page_unavailable"


def test_pdf_text_beyond_previous_flat_limit_is_indexed(monkeypatch):
    url = "https://example.org/manual.pdf"
    text = ("Technical workshop documentation.\f" * 1600).replace("\f", "", 1599)
    assert len(text) > j1_fetch.MAX_TEXT_CHARS
    monkeypatch.setattr(j1_fetch, "_pdf_to_text", lambda _body: text + "C120702 diagnostic procedure cut short")
    job = _collect(monkeypatch, {url: ("application/pdf", b"%PDF-synthetic")})
    hit = j1.research_results(job, query="C120702")["results"][0]
    assert hit["page"] == 2 and hit["source_url"] == f"{url}#page=2"
    assert j1.research_document(job, hit["document_id"], page=2)["fragment_truncated"] is True


def test_flat_legacy_corpus_keeps_document_offsets_after_schema_upgrade(monkeypatch):
    url = "https://example.org/instructions.txt"
    text = "C120702 diagnostic procedure in a legacy plain document. " * 4
    job = _collect(monkeypatch, {url: ("text/plain", text.encode())})
    with j1._db() as conn:
        conn.execute("DROP TABLE document_fragments")
        conn.commit()
    hit = j1.research_results(job, query="C120702")["results"][0]
    assert "page" not in hit and hit["url"] == url
    document = j1.research_document(job, hit["document_id"], offset=15, max_chars=10)
    assert document["text"] == text[15:25] and document["next_offset"] == 25


@pytest.mark.parametrize("retained", [False, True])
def test_exact_copies_keep_searchable_distinct_titles_without_repeating_the_body(monkeypatch, retained):
    urls = ["https://example.org/AX241.pdf", "https://example.org/AX242.pdf", "https://example.com/AX241.pdf"]
    body = "Use the published assembly instructions for this service variant before selecting its replacement.\f"
    monkeypatch.setattr(j1_fetch, "_pdf_to_text", lambda _pdf: body)
    job = _collect(monkeypatch, dict.fromkeys(urls, ("application/pdf", b"%PDF-test")))

    if retained:
        # Simulate the previous index and two simultaneous first opens after upgrade.
        with j1._db() as conn:
            conn.execute(
                "DELETE FROM documents_fts WHERE document_id IN (SELECT id FROM documents WHERE status='duplicate')"
            )
            conn.execute("PRAGMA user_version=0")
            conn.commit()
    callers = Barrier(2)

    def search_title():
        callers.wait(timeout=10)
        return j1.research_results(job, query="AX242")

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = [pool.submit(search_title) for _ in range(2)]
        for response in responses:
            result = response.result(timeout=15)
            assert result["total"] == 1
            hit = result["results"][0]
            assert hit["url"] == urls[1] and hit["excerpt"] == "AX242.pdf"
            assert hit["status"] == "duplicate" and hit["duplicate"]["url"] == urls[0]
            assert "page" not in hit
    assert j1.research_results(job, query="AX241")["total"] == 1
    bodies = j1.research_results(job, query="assembly")
    assert bodies["total"] == 1 and bodies["results"][0]["url"] == urls[0]
    with j1._db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents_fts WHERE body<>''").fetchone()[0] == 1


def test_pdf_redaction_retains_breaks_and_blank_text_still_uses_ocr(monkeypatch):
    source = "WVWZZZ1KZ\f6W000001\fphone: +7 900\f 000 00 01\fremaining page"
    cleaned = j1_fetch.redact_sensitive(source, preserve_page_breaks=True)
    assert cleaned.count("\f") == source.count("\f")
    assert "WVWZZZ1KZ" not in cleaned and "+7 900" not in cleaned
    monkeypatch.setattr(j1_fetch, "_pdf_to_text", lambda _body: "\n\f\n\f")
    monkeypatch.setattr(j1_ocr, "extract_scanned_pdf", lambda *_args, **_kwargs: {"ok": True, "text": "Scanned text"})
    assert j1_fetch._extract_pdf(b"%PDF")["extraction_method"] == "pdf_ocr"


def test_worker_ocr_indexes_only_empty_pdf_page_and_redacts_across_pages(monkeypatch):
    url = "https://example.org/mixed.pdf"
    pdf = b"%PDF-synthetic"
    native = "Document card.\f \n\f6W000001\nFinal appendix.\f"
    monkeypatch.setattr(j1_fetch, "_pdf_to_text", lambda _body: native)
    calls = []

    def recognize(body, page):
        calls.append((body, page))
        return "Connector CX-402A.\nWVWZZZ1KZ", None

    monkeypatch.setattr(j1_ocr, "extract_mixed_pdf_page", recognize)
    monkeypatch.setattr(
        j1_fetch,
        "_request_public",
        lambda *_args, **_kwargs: (200, {"content-type": "application/pdf"}, pdf, url),
    )
    assert j1_fetch.fetch_document(url)["error"] == "empty_or_dynamic"
    assert not calls

    job = _collect(monkeypatch, {url: ("application/pdf", pdf)})
    assert calls == [(pdf, 2)]
    hit = j1.research_results(job, query="CX-402A")["results"][0]
    assert hit["page"] == 2 and hit["source_url"] == f"{url}#page=2"
    assert hit["extraction_method"] == "pdf_mixed"
    assert hit["fragment_extraction_method"] == "pdf_ocr"
    assert hit["pdf_limitations"] == ["short_pdf_text"]
    doc_id = hit["document_id"]
    original = j1.research_document(job, doc_id, page=1)
    assert original["fragment_extraction_method"] == "pdf_text" and original["text"] == "Document card."
    scanned = j1.research_document(job, doc_id, page=2)
    assert scanned["fragment_extraction_method"] == "pdf_ocr" and "Connector CX-402A." in scanned["text"]
    assert scanned["next_page"] == 3 and "fragment_truncated" not in scanned
    report = j1.research_report(job)["report"]["sources"][0]
    assert report["pdf_limitations"] == ["short_pdf_text"]
    with j1._db(readonly=True) as conn:
        saved = "\n".join(conn.iterdump())
    assert "WVWZZZ1KZ" not in saved and "6W000001" not in saved


def test_mixed_pdf_manual_login_text_preserves_native_evidence_and_challenge_guard(monkeypatch):
    manual = "https://example.org/manual.pdf"
    challenge = "https://example.org/challenge.pdf"
    manual_body, challenge_body = b"%PDF-manual", b"%PDF-challenge"
    native = {
        manual_body: "Service manual with a native procedure for connector retention and harness inspection.\f\f",
        challenge_body: "Please log in to continue before accessing the requested workshop service documentation.\f\f",
    }
    calls = []

    def recognize(body, page):
        calls.append((body, page))
        return "Please log in to the dealer portal after checking the connector.", None

    monkeypatch.setattr(j1_fetch, "_pdf_to_text", native.__getitem__)
    monkeypatch.setattr(j1_ocr, "extract_mixed_pdf_page", recognize)
    job = _collect(
        monkeypatch, {manual: ("application/pdf", manual_body), challenge: ("application/pdf", challenge_body)}
    )

    assert calls == [(manual_body, 2)]
    hit = j1.research_results(job, query="harness inspection")["results"][0]
    assert (hit["page"], hit["source_url"], hit["fragment_extraction_method"]) == (1, f"{manual}#page=1", "pdf_text")
    scanned = j1.research_document(job, hit["document_id"], page=2)
    assert "Please log in to the dealer portal" in scanned["text"]
    report = j1.research_report(job)["report"]
    assert [source["url"] for source in report["sources"]] == [manual]
    assert [(source["url"], source["reason"]) for source in report["unavailable"]] == [(challenge, "requires_human")]


@pytest.mark.parametrize(
    ("cap", "complete"),
    [
        ("document", "Inspect the harness mounting clip."),
        ("parser", "Inspect the harness mounting clip."),
        ("document", ""),
    ],
)
def test_capped_mixed_pdf_ocr_cannot_invent_base_dtc_in_search_or_report(monkeypatch, cap, complete):
    url = "https://static.nhtsa.gov/odi/tsbs/synthetic/mixed.pdf"
    native = (
        "Workshop manual applies to Ficta Norvale model year 2021, engine MX204.\n"
        "Read the scanned connector label on the following page.\f\f"
    )
    ocr_text = (complete + "\n" if complete else "") + "P1A27-00 connector label."
    prefix_len = ocr_text.index("-00")

    def run_parser(argv, *_args, **kwargs):
        if argv[0].endswith("/pdftoppm"):
            return "ok", b"\xff\xd8jpeg-fixture"
        if cap == "parser":
            limit = kwargs["output_limit"]
            return "output_limit", ocr_text.encode()[:limit]
        return "ok", ocr_text.encode()

    monkeypatch.setattr(j1_fetch, "_pdf_to_text", lambda _body: native)
    if cap == "document":
        monkeypatch.setattr(j1_fetch, "MAX_PUBLIC_PDF_TEXT_CHARS", len(native) + prefix_len)
    else:
        monkeypatch.setattr(j1_ocr, "MIXED_PAGE_TEXT_BYTES", prefix_len)
    monkeypatch.setattr(j1_ocr, "_ocr_languages_available", lambda: True)
    monkeypatch.setattr(j1_ocr, "_run_parser", run_parser)
    monkeypatch.setattr(
        j1_fetch,
        "_request_public",
        lambda *_args, **_kwargs: (200, {"content-type": "application/pdf"}, b"%PDF-synthetic", url),
    )
    monkeypatch.setattr(j1, "search_public", lambda *_args, **_kwargs: ([{"url": url}], "fixture"))
    job = j1.start_research(
        objective="Inspect synthetic workshop instructions",
        automotive_context={"make": "Ficta", "model": "Norvale", "year": 2021, "engine": "MX204", "dtc": "P1A27"},
    )["job_id"]
    with j1._db() as conn:
        assert j1._claim_job(conn) == job
    j1._work_job(job)

    assert j1.research_results(job, query="P1A27", query_mode="dtc")["total"] == 0
    report = j1.research_report(job)["report"]
    assert not report["confirmed"]
    source = report["sources"][0]
    assert source["pdf_limitations"] == ["ocr_text_limit"]
    page = j1.research_document(job, source["document_id"], page=2)
    assert page["text"] == complete
    assert page["fragment_truncated"] is True and page["fragment_extraction_method"] == "pdf_ocr"
    if complete:
        hit = j1.research_results(job, query="mounting clip")["results"][0]
        assert hit["page"] == 2 and hit["fragment_truncated"] is True


def test_mixed_pdf_worker_charges_failed_attempts_and_keeps_digital_pages(monkeypatch):
    sources = {}
    attempts = []
    for number in range(4):
        body = f"%PDF-synthetic-{number}".encode()
        sources[f"https://example.org/ocr-{number}.pdf"] = "application/pdf", body

    def recognize(body, page):
        attempts.append((body, page))
        return "", "ocr_timeout"

    monkeypatch.setattr(j1_ocr, "extract_mixed_pdf_page", recognize)
    monkeypatch.setattr(
        j1_fetch,
        "_pdf_to_text",
        lambda body: f"{body.decode()} digital opening.\f\f\f\f\fDigital final page.\f",
    )
    job = _collect(monkeypatch, sources)
    assert [page for _, page in attempts] == [2, 3, 4, 2, 3, 4, 2, 3]
    status = j1.research_status(job)
    assert status["mixed_pdf_ocr_pages_attempted"] == 8 and status["mixed_pdf_ocr_pages_remaining"] == 0
    documents = {row["url"]: row for row in j1.research_results(job)["results"]}
    assert documents["https://example.org/ocr-0.pdf"]["pdf_limitations"] == [
        "ocr_document_budget",
        "ocr_timeout",
        "short_pdf_text",
    ]
    assert documents["https://example.org/ocr-3.pdf"]["pdf_limitations"] == [
        "ocr_document_budget",
        "ocr_job_budget",
        "short_pdf_text",
    ]
    hits = j1.research_results(job, query="Digital final page")["results"]
    assert sum(hit.get("page") == 6 for hit in hits) == 4


def test_mixed_pdf_reservations_survive_requeue_and_respect_cancellation(monkeypatch):
    receipt = j1.research_sources(
        objective="Inspect a synthetic workshop manual",
        source_urls=["https://example.org/recovery.pdf"],
        idempotency_key="mixed-ocr-recovery-test",
    )
    job = receipt["job_id"]
    with j1._db() as conn, conn:
        assert j1._claim_job(conn) == job
        document_id = conn.execute("SELECT id FROM documents WHERE job_id=?", (job,)).fetchone()[0]
        conn.execute("UPDATE documents SET status='running' WHERE id=?", (document_id,))
    assert j1._reserve_mixed_ocr_page(job, document_id) is None
    with j1._db() as conn, conn:
        conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (job,))
        conn.execute("UPDATE documents SET status='pending' WHERE id=?", (document_id,))
    with j1._db() as conn, conn:
        assert j1._claim_job(conn) == job
        conn.execute("UPDATE documents SET status='running' WHERE id=?", (document_id,))
    assert j1._reserve_mixed_ocr_page(job, document_id) is None
    assert j1._reserve_mixed_ocr_page(job, document_id) is None
    assert j1._reserve_mixed_ocr_page(job, document_id) == "ocr_document_budget"
    assert j1.research_cancel(job)["ok"]
    assert j1._reserve_mixed_ocr_page(job, document_id) == "ocr_interrupted"
    assert j1.research_status(job)["mixed_pdf_ocr_pages_attempted"] == 3
