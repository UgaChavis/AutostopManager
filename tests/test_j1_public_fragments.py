"""Search and read stored public document fragments through the J1 worker."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import shutil
import subprocess
from threading import Barrier
from unittest.mock import Mock

import pytest

from autostop_manager import j1_fetch, j1_ocr, j1_research as j1, j1_vin_documents


@pytest.fixture(autouse=True)
def isolated_corpus(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "vin"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "0")
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (True, 0))
    monkeypatch.setattr(j1_fetch, "_rate_limit", lambda *_args: None)
    monkeypatch.setattr(j1, "_STOP", False)


def _collect(monkeypatch, sources, *, automotive_context=None):
    def request(url, **_kwargs):
        content_type, body = sources[url]
        return 200, {"content-type": content_type}, body, url

    monkeypatch.setattr(j1_fetch, "_request_public", request)
    receipt = j1.research_sources(
        objective="Review public automotive service instructions",
        source_urls=list(sources),
        idempotency_key="source-fragment-tests-1",
        automotive_context=automotive_context,
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
        lines = " T* ".join(f"({line}) Tj" for line in page_text.split("\n"))
        stream = f"BT /F1 12 Tf 14 TL 50 700 Td {lines} ET".encode()
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


def test_report_keeps_period_in_qualified_html_scope_heading(monkeypatch):
    url = "https://static.nhtsa.gov/odi/tsbs/synthetic/scoped-manual.html"
    heading = "Bulletin No. 2 — Model year 2020"
    html = (
        '<h2 id="current">Bulletin No. 1 — Model year 2021</h2>'
        "<p>Workshop manual applies to Ficta Norvale model year 2021, engine MX204.</p>"
        "<p>P1A28 means a damper fault.</p>"
        f'<h2 id="previous">{heading}</h2><p>P1A27 means a connector fault.</p>'
    ).encode()
    monkeypatch.setattr(
        j1_fetch,
        "_request_public",
        lambda *_args, **_kwargs: (200, {"content-type": "text/html; charset=utf-8"}, html, url),
    )
    job = j1.research_sources(
        objective="Check a synthetic bulletin",
        source_urls=[url],
        idempotency_key="scope-heading-test",
        automotive_context={"make": "Ficta", "model": "Norvale", "year": 2021, "engine": "MX204", "dtc": "P1A27"},
    )["job_id"]
    with j1._db() as conn:
        assert j1._claim_job(conn) == job
    j1._work_job(job)
    with j1._db() as conn:
        document = conn.execute("SELECT id,body FROM documents WHERE job_id=?", (job,)).fetchone()
        passages = list(j1._source_passages(conn, document["id"], url, document["body"]))
    assert [(passage.text, passage.page, passage.source_url) for passage in passages if passage.is_heading][-1] == (
        heading,
        2,
        url + "#previous",
    )
    assert not j1.research_report(job)["report"]["confirmed"]


@pytest.mark.parametrize("mode", ["html", "retained-html", "text", "redacted-html"])
def test_capped_public_text_keeps_display_and_only_completed_evidence(monkeypatch, mode):
    url = "https://static.nhtsa.gov/odi/tsbs/synthetic/capped-manual"
    scope = "Workshop manual applies to Ficta Norvale model year 2021, engine MX204."
    anchor = '<h2 id="diagnostics">Applicable models</h2><p>'
    media_type, opening, closing = ("text/plain", "", "") if mode == "text" else ("text/html", anchor, "</p>")
    tail = "AX204123456789012345" if mode == "redacted-html" else "P1A27-00"
    wanted = tail[:17] if mode == "redacted-html" else "P1A27"

    def source(padding):
        return f"{opening}{scope} P1A28 is a documented fault. {padding} {tail}{closing}".encode()

    probe = j1_fetch._extract_static_content(media_type, source("i"), url, {})["text"]
    pad = "i" * (1 + j1.MAX_DOCUMENT_CHARS - len(wanted) - probe.index(tail))
    body = source(pad)
    extraction = j1_fetch._extract_static_content(media_type, body, url, {})
    assert extraction["body_truncated"] is True and extraction["text"].endswith(wanted)
    monkeypatch.setattr(
        j1_fetch,
        "_request_public",
        lambda *_args, **_kwargs: (200, {"content-type": media_type}, body, url),
    )
    job = j1.research_sources(
        objective="Check a synthetic diagnostic manual",
        source_urls=[url],
        idempotency_key="capped-html-regression",
        automotive_context={"make": "Ficta", "model": "Norvale", "year": 2021, "engine": "MX204", "dtc": "P1A27"},
    )["job_id"]
    with j1._db() as conn:
        assert j1._claim_job(conn) == job
    j1._work_job(job)
    document_id = j1.research_results(job)["results"][0]["document_id"]
    if mode == "retained-html":
        with j1._db() as conn, conn:
            conn.execute("UPDATE documents SET body_truncated=0 WHERE id=?", (document_id,))
    document = j1.research_document(job, document_id, offset=j1.MAX_DOCUMENT_CHARS - 30)
    if mode == "redacted-html":
        assert document["text"].endswith("[redacted]") and document["total_chars"] < j1.MAX_DOCUMENT_CHARS
    else:
        assert document["text"].endswith(wanted)
    assert document.get("body_truncated", False) is (mode != "retained-html")
    assert j1.research_results(job, query="P1A27", query_mode="dtc")["total"] == 0
    exact = j1.research_results(job, query="P1A28", query_mode="dtc")
    assert exact["total"] == 1
    assert exact["results"][0].get("body_truncated", False) is (mode != "retained-html")
    report = j1.research_report(job)["report"]
    assert not report["confirmed"]
    assert report["sources"][0].get("body_truncated", False) is (mode != "retained-html")
    assert ("source_text_truncated" in {item["code"] for item in report["limitations"]}) is (mode != "retained-html")


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


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="pdftotext unavailable")
def test_worker_keeps_pdf_page_offsets_when_final_redaction_changes_tail(monkeypatch):
    url = "https://example.org/revised-manual.pdf"
    prefix = "AX204123456789012"
    pdf = _pdf(
        "Service bulletin instructions for connector retention and inspection before work.",
        "P1A27: check the actuator feedback signal and harness mechanical condition.",
        prefix + "345",
    )
    native = j1_fetch._pdf_to_text(pdf)
    monkeypatch.setattr(j1_fetch, "MAX_PUBLIC_PDF_TEXT_CHARS", native.index(prefix) + len(prefix))
    job = _collect(monkeypatch, {url: ("application/pdf", pdf)})

    hit = j1.research_results(job, query="P1A27")["results"][0]
    assert hit["page"] == 2 and hit["source_url"] == f"{url}#page=2"
    document_id = hit["document_id"]
    page = j1.research_document(job, document_id, page=2)
    assert page["source_url"] == hit["source_url"] and page["text"].startswith("P1A27:")
    tail = j1.research_document(job, document_id, page=3)
    assert tail["text"] == "[redacted]" and tail["fragment_truncated"] is True
    with j1._db(readonly=True) as conn:
        assert prefix not in "\n".join(conn.iterdump())


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="pdftotext unavailable")
@pytest.mark.parametrize("limit", ["page", "text"])
def test_pdf_reports_limit_when_extraction_stops_on_complete_page_boundary(monkeypatch, limit):
    url = "https://example.org/three-page-manual.pdf"
    pdf = _pdf(
        "Service bulletin instructions for connector retention and inspection before work.",
        "Workshop procedure and full second page of the technical reference.",
        "Unprocessed third page containing the final diagnostic procedure.",
    )
    if limit == "page":
        monkeypatch.setattr(j1_fetch, "_PDF_TEXT_PAGES", 2)
    else:
        native = j1_fetch._pdf_to_text(pdf)
        cap = native.index("\f", native.index("\f") + 1) + 1
        monkeypatch.setattr(j1_fetch, "MAX_PUBLIC_PDF_TEXT_CHARS", cap)
    job = _collect(monkeypatch, {url: ("application/pdf", pdf)})
    document_id = j1.research_results(job)["results"][0]["document_id"]
    second = j1.research_document(job, document_id, page=2)
    assert second["ok"] and "fragment_truncated" not in second
    assert second["pdf_limitations"] == [f"pdf_{limit}_limit_reached"]
    missed = j1.research_document(job, document_id, page=3)
    assert missed["error"]["code"] == "document_page_unavailable"
    assert missed["pdf_limitations"] == second["pdf_limitations"]


def test_native_pdf_timeout_reaps_the_parser_process_group(monkeypatch):
    parser = Mock()
    parser.communicate.side_effect = subprocess.TimeoutExpired("pdftotext", 15)
    start = Mock(return_value=parser)
    reap = Mock()
    monkeypatch.setattr(j1_fetch.subprocess, "Popen", start)
    monkeypatch.setattr(j1_vin_documents, "_kill_parser", reap)

    assert j1_fetch._pdf_to_text(b"%PDF-synthetic") == ""
    assert start.call_args.kwargs["start_new_session"] is True
    reap.assert_called_once_with(parser)


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


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="pdftotext unavailable")
@pytest.mark.parametrize("diagnostic_page", [False, True])
def test_pdf_dtc_toc_is_retrievable_but_only_diagnostic_content_is_cited(monkeypatch, diagnostic_page):
    code = "P1765"
    toc = (
        "Table of contents\n- P1765\nDTC P1765, P1766\n"
        "P1765: pressure sensor circuit ................ 81\n"
        "P1765: pressure sensor circuit . . . . . . 81\n"
        r"P1765: pressure sensor circuit \274 81"  # Ellipsis in the fixture's PDF font encoding.
    )
    scope = "This bulletin applies to model year 2022 Arden Vale vehicles with engine EV19."
    procedure = "DTC P1765 indicates a pressure sensor signal fault."
    pages = [toc, scope, procedure] if diagnostic_page else [toc, scope]
    url = "https://static.nhtsa.gov/odi/tsbs/synthetic/contents.pdf"
    context = {"make": "Arden", "model": "Vale", "year": 2022, "engine": "EV19", "dtc": code}
    job = _collect(monkeypatch, {url: ("application/pdf", _pdf(*pages))}, automotive_context=context)
    hits = j1.research_results(job, query=code, query_mode="dtc")["results"]
    assert [hit["page"] for hit in hits] == ([1, 3] if diagnostic_page else [1])
    report = j1.research_report(job)["report"]
    assert bool(report["confirmed"]) == diagnostic_page
    subjects = [item for item in report["sources"][0]["applicability_evidence"] if item["kind"] == "subject"]
    assert subjects == (
        [{"kind": "subject", "page": 3, "source_url": url + "#page=3", "quote": procedure}] if diagnostic_page else []
    )


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="pdftotext unavailable")
@pytest.mark.parametrize("case", ["wrapped-scope", "page-break", "column", "heading", "other-engine", "blank-line"])
def test_pdf_report_combines_prose_scope_wrap_but_not_structural_boundaries(monkeypatch, case):
    base = "This bulletin applies to model year 2022 Arden Vale vehicles"
    continuation = "with engine EV19."
    subject = "DTC B2407 indicates a recirculation flap feedback error."
    documents = {
        "wrapped-scope": [f"{base}\n{continuation}"],
        "page-break": [base, continuation],
        "column": [f"{base.replace('Arden Vale', 'Arden      Vale')}\n{continuation}"],
        "heading": [f"{base.removeprefix('This bulletin applies to ')}\n{continuation}"],
        "other-engine": [f"{base} with engine MX28\n{continuation}"],
        "blank-line": [f"{base}\n\n{continuation}"],
    }
    url = "https://static.nhtsa.gov/odi/tsbs/synthetic/wrapped-scope.pdf"
    context = {"make": "Arden", "model": "Vale", "year": 2022, "engine": "EV19", "dtc": "B2407"}
    pages = documents[case]
    job = _collect(monkeypatch, {url: ("application/pdf", _pdf(*pages, subject))}, automotive_context=context)
    matches = j1.research_results(job, query="B2407", query_mode="dtc")
    assert matches["total"] == 1 and matches["results"][0]["page"] == len(pages) + 1
    report = j1.research_report(job)["report"]
    assert len(report["sources"]) == 1
    source = report["sources"][0]
    if case != "wrapped-scope":
        assert source["applicability"] != "exact" and not report["confirmed"]
        return
    assert source["applicability"] == "exact" and report["confirmed"]
    scope, diagnostic = source["applicability_evidence"]
    assert scope == {
        "kind": "vehicle_scope",
        "quote": f"{base}\n{continuation}",
        "page": 1,
        "source_url": url + "#page=1",
    }
    assert diagnostic == {"kind": "subject", "quote": subject, "page": 2, "source_url": url + "#page=2"}
    stored = j1.research_document(job, source["document_id"], page=1)
    assert stored["ok"] and scope["quote"] in stored["text"]


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


def _collect_automotive_pdf(monkeypatch, body):
    url = "https://static.nhtsa.gov/odi/tsbs/synthetic/repair-manual.pdf"
    monkeypatch.setattr(
        j1_fetch,
        "_request_public",
        lambda *_args, **_kwargs: (200, {"content-type": "application/pdf"}, body, url),
    )
    monkeypatch.setattr(j1, "search_public", lambda *_args, **_kwargs: ([{"url": url}], "fixture"))
    job = j1.start_research(
        objective="Inspect synthetic workshop instructions",
        automotive_context={"make": "Ficta", "model": "Norvale", "year": 2021, "engine": "MX204", "dtc": "P1A27"},
    )["job_id"]
    with j1._db() as conn:
        assert j1._claim_job(conn) == job
    j1._work_job(job)
    return job, url


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="pdftotext unavailable")
@pytest.mark.parametrize("legacy", [False, True])
def test_native_pdf_clip_is_not_exact_dtc_evidence(monkeypatch, legacy):
    pdf = _pdf("Workshop manual applies to Ficta Norvale model year 2021, engine MX204.", "P1A27-00")
    full_text = j1_fetch._pdf_to_text(pdf)
    monkeypatch.setattr(j1_fetch, "MAX_PUBLIC_PDF_TEXT_CHARS", full_text.index("P1A27") + len("P1A27"))
    job, url = _collect_automotive_pdf(monkeypatch, pdf)
    document_id = j1.research_results(job)["results"][0]["document_id"]
    if legacy:
        with j1._db() as conn, conn:
            conn.execute("UPDATE document_fragments SET truncated=0 WHERE document_id=?", (document_id,))
    page = j1.research_document(job, document_id, page=2)
    assert page["text"] == "P1A27" and page["fragment_truncated"] is True
    assert page["source_url"] == f"{url}#page=2"
    if not legacy:
        assert j1.research_results(job, query="P1A27")["results"][0]["fragment_truncated"] is True
    assert j1.research_results(job, query="P1A27", query_mode="dtc")["total"] == 0
    assert not j1.research_report(job)["report"]["confirmed"]


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="pdftotext unavailable")
def test_mixed_pdf_redaction_expansion_does_not_clip_native_tail(monkeypatch):
    pdf = _pdf("Workshop manual applies to Ficta Norvale model year 2021, engine MX204.", "", "P1A27-00")
    full_text = j1_fetch._pdf_to_text(pdf)
    contact = "a@b.co"
    assert len(j1_fetch.redact_sensitive(contact)) > len(contact)
    monkeypatch.setattr(j1_fetch, "MAX_PUBLIC_PDF_TEXT_CHARS", len(full_text) + len(contact))
    monkeypatch.setattr(j1_ocr, "extract_mixed_pdf_page", lambda _body, _page: (contact, None))
    job, url = _collect_automotive_pdf(monkeypatch, pdf)
    document_id = j1.research_results(job)["results"][0]["document_id"]
    page = j1.research_document(job, document_id, page=3)
    assert page["text"] == "P1A27-00" and page["source_url"] == f"{url}#page=3"
    assert page["pdf_limitations"] == ["ocr_text_limit"] and "fragment_truncated" not in page
    skipped = j1.research_document(job, document_id, page=2)
    assert skipped["fragment_truncated"] is True and not skipped["text"]
    assert j1.research_results(job, query="P1A27", query_mode="dtc")["total"] == 0
    assert j1.research_results(job, query="P1A27-00", query_mode="dtc")["results"][0]["page"] == 3
    assert not j1.research_report(job)["report"]["confirmed"]


def test_capped_flat_pdf_ocr_tail_is_not_used_as_exact_evidence(monkeypatch):
    head = "Workshop manual applies to Ficta Norvale model year 2021, engine MX204.\n"
    tail = "\nP1A27"
    capped = head + "i" * (j1.MAX_DOCUMENT_CHARS - len(head) - len(tail)) + tail
    monkeypatch.setattr(j1_fetch, "_pdf_to_text", lambda _body: "")
    monkeypatch.setattr(j1_ocr, "extract_scanned_pdf", lambda *_args, **_kwargs: {"ok": True, "text": capped})
    job, _url = _collect_automotive_pdf(monkeypatch, b"%PDF-synthetic")
    assert j1.research_results(job)["results"][0]["extraction_method"] == "pdf_ocr"
    assert j1.research_results(job, query="P1A27", query_mode="dtc")["total"] == 0
    assert not j1.research_report(job)["report"]["confirmed"]


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
