"""Search and read stored public document fragments through the J1 worker."""

from __future__ import annotations

import shutil

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


def test_pdf_redaction_retains_breaks_and_blank_text_still_uses_ocr(monkeypatch):
    source = "WVWZZZ1KZ\f6W000001\fphone: +7 900\f 000 00 01\fremaining page"
    cleaned = j1_fetch.redact_sensitive(source, preserve_page_breaks=True)
    assert cleaned.count("\f") == source.count("\f")
    assert "WVWZZZ1KZ" not in cleaned and "+7 900" not in cleaned
    monkeypatch.setattr(j1_fetch, "_pdf_to_text", lambda _body: "\n\f\n\f")
    monkeypatch.setattr(j1_ocr, "extract_scanned_pdf", lambda *_args, **_kwargs: {"ok": True, "text": "Scanned text"})
    assert j1_fetch._extract_pdf(b"%PDF")["extraction_method"] == "pdf_ocr"
