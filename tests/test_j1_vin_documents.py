from __future__ import annotations

import json
import os
from pathlib import Path
import resource
import shutil
import stat
import subprocess
import sys
import time
from typing import Any

import pytest

from autostop_manager import j1_vin_documents as documents

_PYTHON = str(Path(sys.executable).resolve())


def _pdf(page_count: int) -> bytes:
    """Generate a public synthetic PDF without external fixture dependencies."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(f'{4 + 2 * i} 0 R' for i in range(page_count))}] /Count {page_count} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index in range(page_count):
        stream_id = 5 + 2 * index
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {stream_id} 0 R >>".encode()
        )
        lines = [f"PAGE {index + 1} PUBLIC DOCUMENT LINE {line} BACKGROUND DATA" for line in range(8)]
        if index == 455:
            lines.append("PAGE 456 SYNTHETIC 1HGCM82673A000000")
        content = b"BT /F1 10 Tf 40 730 Td " + b" ".join(f"({line}) Tj 0 -15 Td".encode() for line in lines) + b" ET"
        objects.append(f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream")
    pieces = [b"%PDF-1.4\n"]
    offsets = [0]
    length = len(pieces[0])
    for number, obj in enumerate(objects, 1):
        offsets.append(length)
        piece = f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
        pieces.append(piece)
        length += len(piece)
    pieces.append(f"xref\n0 {len(offsets)}\n".encode() + b"0000000000 65535 f \n")
    pieces.extend(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    pieces.append(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{length}\n%%EOF\n".encode())
    return b"".join(pieces)


def _workspace(tmp_path: Path) -> Path:
    result = tmp_path / "private"
    result.mkdir(mode=0o700)
    return result


def _fake_pdf(monkeypatch: pytest.MonkeyPatch, output: bytes, count: int | None = None) -> None:
    monkeypatch.setattr(
        documents.shutil, "which", lambda name: f"/usr/bin/{name}" if name != "pdfinfo" or count else None
    )

    def parser(argv: list[str], *_args: Any, **_kwargs: Any) -> tuple[str, bytes]:
        if "pdfinfo" in argv[0]:
            return "ok", f"Pages: {count}\n".encode()
        assert argv[argv.index("-l") + 1] == "1000"
        return "ok", output

    monkeypatch.setattr(documents, "_run_parser", parser)


def test_html_visible_text_title_charset_and_unredacted_vin(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    workspace = _workspace(tmp_path)
    body = """<html><head><title>Технический документ</title><script>hidden VIN</script></head>
    <body><p>1HGCM82673A000000</p><div hidden>not visible</div>
    <span style="display: none">css hidden</span><span aria-hidden="true">aria hidden</span>
    <style>secret style</style><template>template hidden</template><noscript>noscript hidden</noscript>
    <div>Useful &amp; public<br/>text</div><footer>Visible footer</footer></body></html>""".encode("cp1251")
    result = documents.extract_content(body, "text/html; charset=windows-1251", "https://example.org", workspace)
    assert result["ok"] and result["title"] == "Технический документ"
    text = result["pages"][0]["text"]
    assert "1HGCM82673A000000" in text and "Useful & public" in text and "Visible footer" in text
    assert "hidden" not in text and "Технический" not in text
    assert result["pages"][0]["page"] == 1 and result["page_count"] == 1
    assert list(workspace.iterdir()) == [] and "1HGCM82673A000000" not in caplog.text
    fallback = documents.extract_content(b"<p>Fallback</p>", "text/html; charset=missing-codec", "", workspace)
    assert fallback["pages"][0]["text"] == "Fallback"


def test_html_structure_and_text_are_bounded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(documents, "MAX_TEXT_BYTES", 16)
    result = documents.extract_content(b"<p>" + "ж".encode() * 50 + b"</p>", "text/html", "", tmp_path)
    assert result["truncated"] and len(result["pages"][0]["text"].encode()) <= 16
    nested = documents.extract_content(b"<div>" * 600 + b"ignored", "text/html", "", tmp_path)
    assert nested["truncated"] and "html_structure_limit" in nested["limitations"]
    assert nested["pages"][0]["text"] == ""


@pytest.mark.parametrize(
    "prefix,element,suffix",
    [
        ("<ul>", "<li>", "</ul>"),
        ("<table><tbody>", "<tr><td>", "</table>"),
        ("<table><tr>", "<td>", "</table>"),
        ("<table><tr>", "<th>", "</table>"),
        ("", "<p>", ""),
    ],
)
def test_html_optional_end_tags_preserve_large_flat_documents(
    tmp_path: Path, prefix: str, element: str, suffix: str
) -> None:
    body = prefix + "".join(f"{element}PUBLIC ROW {number}" for number in range(700)) + suffix
    body += "<p>AFTER DOCUMENT 1HGCM82673A000000</p>"
    result = documents.extract_content(body.encode(), "text/html", "", tmp_path)
    text = result["pages"][0]["text"]
    assert text.count("PUBLIC ROW") == 700 and "PUBLIC ROW 699" in text
    assert "AFTER DOCUMENT 1HGCM82673A000000" in text
    assert not result["truncated"] and "html_structure_limit" not in result["limitations"]


@pytest.mark.parametrize("head_end", ["", "</head>"])
@pytest.mark.parametrize("body_text", ["<body><p>{text}</p></body>", "<p>{text}</p>", "{text}"])
def test_html_body_closes_optional_head_without_hiding_visible_vin(
    tmp_path: Path, head_end: str, body_text: str
) -> None:
    visible = "VISIBLE VIN 1HGCM82673A000000"
    body = (
        f"<html><head><title>Public document</title><meta charset=utf-8>{head_end}"
        + body_text.format(text=visible)
        + "</html>"
    ).encode()
    result = documents.extract_content(body, "text/html", "", tmp_path)
    assert result["pages"][0]["text"] == visible
    assert result["title"] == "Public document" and not result["truncated"]


def test_html_optional_definition_ends_preserve_flat_terms_and_tail(tmp_path: Path) -> None:
    body = (
        "<dl>"
        + "".join(f"<dt>TERM {number}<dd>VALUE {number}" for number in range(700))
        + "</dl><p>AFTER DEFINITIONS VIN 1HGCM82673A000000</p>"
    ).encode()
    result = documents.extract_content(body, "text/html", "", tmp_path)
    text = result["pages"][0]["text"]
    assert text.count("TERM ") == 700 and text.count("VALUE ") == 700
    assert "AFTER DEFINITIONS VIN 1HGCM82673A000000" in text
    assert not result["truncated"] and "html_structure_limit" not in result["limitations"]


def test_html_definition_scope_retains_hidden_ancestors_and_nested_lists(tmp_path: Path) -> None:
    body = (
        b"<dl hidden><dt>secret term<dd>secret value<dt>secret next term<dd>secret next value</dl>"
        b"<dl><dt hidden>secret sibling<dd>visible value"
        b"<dl hidden><dt>secret nested term<dd>secret nested value</dl>"
        b"<dt>visible term<dd>visible next value</dl>"
        b"<template><dl><dt>secret template term<dd>secret template value</dl></template>"
    )
    result = documents.extract_content(body, "text/html", "", tmp_path)
    text = result["pages"][0]["text"]
    assert "secret" not in text
    assert all(value in text for value in ("visible value", "visible term", "visible next value"))
    assert not result["truncated"]


def test_html_definition_optional_ends_keep_real_nesting_limit(tmp_path: Path) -> None:
    body = ("<p>visible before</p>" + "<dl><dd>" * 300 + "ignored deep text" + "</dl>" * 300).encode()
    result = documents.extract_content(body, "text/html", "", tmp_path)
    assert result["pages"][0]["text"] == "visible before"
    assert result["truncated"] and "html_structure_limit" in result["limitations"]


def test_html_implicit_head_and_unmatched_end_tags_do_not_escape_hidden_contexts(tmp_path: Path) -> None:
    body = (
        b"<div hidden><head><meta><body><p>secret ancestor</p></body></div>"
        b"<div><template></div><head><meta><body><p>secret template</p></body></template></div>"
        b"<p>visible after</p>"
    )
    result = documents.extract_content(body, "text/html", "", tmp_path)
    assert result["pages"][0]["text"] == "visible after" and not result["truncated"]


def test_html_optional_end_tags_keep_hidden_ancestors_and_close_hidden_siblings(tmp_path: Path) -> None:
    body = (
        b"<ul hidden><li>secret ancestor one<li>secret ancestor two</ul>"
        b"<ul><li hidden>secret item<template><li>secret template</li></template><li>visible item</ul>"
        b"<p hidden>secret paragraph<div>visible block</div>"
        b"<table hidden><tr><td>secret table one<tr><td>secret table two</table>"
        b"<table><tr hidden><td>secret cell<tr><td>visible cell</table>"
    )
    result = documents.extract_content(body, "text/html", "", tmp_path)
    text = result["pages"][0]["text"]
    assert "secret" not in text
    assert all(part in text for part in ("visible item", "visible block", "visible cell"))
    assert not result["truncated"]


@pytest.mark.parametrize("boundary", ["button", "section", "fieldset", "select"])
def test_html_list_optional_end_tag_does_not_escape_hidden_special_elements(tmp_path: Path, boundary: str) -> None:
    body = (
        f"<ul><li>visible before<{boundary} hidden>secret before<li>secret nested</li>"
        f"</{boundary}><li>visible after</ul>"
    ).encode()
    result = documents.extract_content(body, "text/html", "", tmp_path)
    text = result["pages"][0]["text"]
    assert "secret" not in text and "visible before" in text and "visible after" in text
    assert not result["truncated"]


@pytest.mark.parametrize("nested,closing", [("<ul><li>", "</li></ul>"), ("<table><tr><td>", "</td></tr></table>")])
def test_html_optional_end_tags_do_not_collapse_real_nested_structures(
    tmp_path: Path, nested: str, closing: str
) -> None:
    body = "<p>visible before</p>" + nested * 300 + "ignored deep text" + closing * 300 + "<p>ignored after</p>"
    result = documents.extract_content(body.encode(), "text/html", "", tmp_path)
    assert result["pages"][0]["text"] == "visible before"
    assert result["truncated"] and "html_structure_limit" in result["limitations"]


def test_real_pdf_page_456_survives_50k_and_private_parent(tmp_path: Path) -> None:
    if not shutil.which("pdftotext"):
        pytest.skip("poppler is not installed")
    workspace = _workspace(tmp_path)
    result = documents.extract_content(_pdf(456), "application/pdf", "https://example.org/manual.pdf", workspace)
    assert result["ok"], result
    assert result["page_count"] == 456 and len(result["pages"]) == 456
    assert sum(len(row["text"]) for row in result["pages"][:455]) > 50_000
    assert result["pages"][455]["page"] == 456
    assert "1HGCM82673A000000" in result["pages"][455]["text"]
    assert not result["truncated"]
    assert stat.S_IMODE(workspace.stat().st_mode) == 0o700 and list(workspace.iterdir()) == []


def test_pdf_empty_pages_retain_indices_and_optional_count(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_pdf(monkeypatch, b"first\f\fthird\f")
    workspace = _workspace(tmp_path)
    result = documents.extract_content(b"%PDF synthetic", "", "https://example.org/file", workspace)
    assert result["pages"] == [{"page": 1, "text": "first"}, {"page": 2, "text": ""}, {"page": 3, "text": "third"}]
    assert result["page_count"] == 3 and "page_count_inferred" in result["limitations"]
    assert list(workspace.iterdir()) == []


def test_pdf_page_and_utf8_text_limits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_pdf(monkeypatch, ("ж" * 100 + "\f").encode() * 1001, count=1001)
    monkeypatch.setattr(documents, "MAX_TEXT_BYTES", 100)
    monkeypatch.setattr(documents, "MAX_PAGE_TEXT_BYTES", 60)
    result = documents.extract_content(b"%PDF synthetic", "application/pdf", "", _workspace(tmp_path))
    assert len(result["pages"]) == 1000 and result["page_count"] == 1001
    assert sum(len(row["text"].encode()) for row in result["pages"]) <= 100
    assert result["truncated"] and {"page_limit", "text_limit", "page_text_limit"}.issubset(result["limitations"])


@pytest.mark.parametrize(
    "outcome,error",
    [("timeout", "pdf_timeout"), ("failed", "pdf_extract_failed"), ("unavailable", "pdf_extract_failed")],
)
def test_pdf_errors_clean_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: str, error: str) -> None:
    monkeypatch.setattr(documents.shutil, "which", lambda name: None if name == "pdfinfo" else "/usr/bin/pdftotext")
    monkeypatch.setattr(documents, "_run_parser", lambda *_args, **_kwargs: (outcome, b""))
    workspace = _workspace(tmp_path)
    result = documents.extract_content(b"%PDF synthetic", "application/pdf", "", workspace)
    assert not result["ok"] and result["error"] == error and list(workspace.iterdir()) == []


def test_pdf_output_limit_keeps_marked_partial_text(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(documents.shutil, "which", lambda name: None if name == "pdfinfo" else "/usr/bin/pdftotext")
    monkeypatch.setattr(documents, "_run_parser", lambda *_args, **_kwargs: ("output_limit", b"partial text"))
    result = documents.extract_content(b"%PDF synthetic", "application/pdf", "", _workspace(tmp_path))
    assert result["ok"] and result["truncated"] and "text_limit" in result["limitations"]


def test_input_and_tool_limits_before_parser(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(documents, "MAX_PDF_BYTES", 20)
    monkeypatch.setattr(documents, "MAX_HTML_BYTES", 20)
    monkeypatch.setattr(documents.shutil, "which", lambda _name: None)
    assert (
        documents.extract_content(b"%PDF" + b"x" * 20, "application/pdf", "", tmp_path)["error"]
        == "document_body_too_large"
    )
    assert documents.extract_content(b"x" * 21, "text/html", "", tmp_path)["error"] == "document_body_too_large"
    assert documents.extract_content(b"not PDF", "application/pdf", "", tmp_path)["error"] == "invalid_pdf"
    assert documents.extract_content(b"%PDF", "application/pdf", "", tmp_path)["error"] == "pdf_tool_unavailable"
    assert documents.extract_content(b"binary", "image/png", "", tmp_path)["error"] == "unsupported_content_type"
    assert documents.extract_content(None, "", "", tmp_path)["error"] == "invalid_body"  # type: ignore[arg-type]
    assert documents.ocr_pages(b"%PDF", [1], tmp_path)["error"] == "ocr_tool_unavailable"
    assert documents.ocr_pages(b"%PDF" + b"x" * 20, [1], tmp_path)["error"] == "document_body_too_large"


@pytest.mark.parametrize("selection", [[], [0], [1001], [True], [1, 1], list(range(1, 14)), ["1"]])
def test_ocr_selection_rejects_invalid_or_excess_pages(selection: list[int], tmp_path: Path) -> None:
    assert documents.ocr_pages(b"%PDF synthetic", selection, tmp_path)["error"] == "ocr_invalid_pages"


def _fake_ocr(
    monkeypatch: pytest.MonkeyPatch, *, fail_page: int | None = None, languages: bytes = b"eng\nrus\n"
) -> list[int]:
    selections: list[int] = []
    monkeypatch.setattr(documents.shutil, "which", lambda name: f"/usr/bin/{name}")

    def parser(argv: list[str], directory: Path, _descriptor: int, **kwargs: Any) -> tuple[str, bytes]:
        if "--list-langs" in argv:
            return "ok", languages
        if "pdftoppm" in argv[0]:
            page = int(argv[argv.index("-f") + 1])
            assert argv[argv.index("-l") + 1] == str(page)
            assert kwargs["payload"].startswith(b"%PDF")
            assert kwargs["file_limit"] == documents.MAX_IMAGE_BYTES
            assert kwargs["output_limit"] == documents.MAX_IMAGE_BYTES
            assert argv[-1] == "-"  # No output prefix: image goes to stdout.
            selections.append(page)
            if page == fail_page:
                return "timeout", b""
            return "ok", b"\x89PNG\r\n\x1a\nsynthetic image"
        assert argv[argv.index("-l") + 1] == "rus+eng"
        assert argv[1:3] == ["stdin", "stdout"]
        assert kwargs["payload"].startswith(b"\x89PNG\r\n\x1a\n")
        assert list(directory.iterdir()) == []
        return "ok", f"OCR SELECTED PAGE {selections[-1]} 1HGCM82673A000000".encode()

    monkeypatch.setattr(documents, "_run_parser", parser)
    return selections


def test_ocr_selected_page_mapping_and_cleanup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    selections = _fake_ocr(monkeypatch)
    workspace = _workspace(tmp_path)
    result = documents.ocr_pages(b"%PDF synthetic", [456, 3], workspace)
    assert result["ok"] and selections == [456, 3]
    assert [row["page"] for row in result["pages"]] == [456, 3]
    assert "PAGE 456" in result["pages"][0]["text"] and "1HGCM82673A000000" in result["pages"][1]["text"]
    assert list(workspace.iterdir()) == []


def test_real_ocr_uses_inherited_private_cwd_and_cleans_images(tmp_path: Path) -> None:
    if not shutil.which("pdftoppm") or not shutil.which("tesseract"):
        pytest.skip("OCR system tools are not installed")
    workspace = _workspace(tmp_path)
    result = documents.ocr_pages(_pdf(1), [1], workspace)
    if result.get("error") == "ocr_languages_unavailable":
        pytest.skip("rus+eng language data is not installed")
    assert result["ok"], result
    assert result["pages"][0]["page"] == 1 and "PUBLIC DOCUMENT" in result["pages"][0]["text"]
    assert stat.S_IMODE(workspace.stat().st_mode) == 0o700 and list(workspace.iterdir()) == []


def test_ocr_partial_failure_retains_exact_selected_indices(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_ocr(monkeypatch, fail_page=456)
    workspace = _workspace(tmp_path)
    result = documents.ocr_pages(b"%PDF synthetic", [3, 456], workspace)
    assert not result["ok"] and result["error"] == "ocr_timeout"
    assert [row["page"] for row in result["pages"]] == [3] and result["limitations"] == ["partial_ocr"]
    assert list(workspace.iterdir()) == []


def test_ocr_languages_invalid_pdf_and_text_caps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _fake_ocr(monkeypatch, languages=b"eng\n")
    assert documents.ocr_pages(b"%PDF synthetic", [1], workspace)["error"] == "ocr_languages_unavailable"
    assert documents.ocr_pages(b"bad", [1], workspace)["error"] == "invalid_pdf"
    _fake_ocr(monkeypatch)
    monkeypatch.setattr(documents, "MAX_TEXT_BYTES", 30)
    result = documents.ocr_pages(b"%PDF synthetic", [1, 2], workspace)
    assert result["truncated"] and sum(len(row["text"].encode()) for row in result["pages"]) == 30
    assert "text_limit" in result["limitations"] and list(workspace.iterdir()) == []


@pytest.mark.parametrize("image", [b"", b"not a PNG"])
def test_ocr_rejects_invalid_renderer_stdout_without_starting_tesseract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, image: bytes
) -> None:
    _fake_ocr(monkeypatch)
    original = documents._run_parser

    def parser(argv: list[str], *args: Any, **kwargs: Any) -> tuple[str, bytes]:
        if argv[0].endswith("pdftoppm"):
            return "ok", image
        if "--list-langs" not in argv:
            raise AssertionError("Invalid image bytes must not reach Tesseract")
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(documents, "_run_parser", parser)
    workspace = _workspace(tmp_path)
    assert documents.ocr_pages(b"%PDF synthetic", [1], workspace)["error"] == "ocr_render_failed"
    assert list(workspace.iterdir()) == []


def test_workspace_rejects_open_permissions_and_symlinks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_pdf(monkeypatch, b"text\f")
    workspace = _workspace(tmp_path)
    workspace.chmod(0o755)
    assert (
        documents.extract_content(b"%PDF synthetic", "application/pdf", "", workspace)["error"]
        == "workspace_unavailable"
    )
    workspace.chmod(0o700)
    link = tmp_path / "alias"
    link.symlink_to(workspace, target_is_directory=True)
    assert documents.extract_content(b"%PDF synthetic", "application/pdf", "", link)["error"] == "workspace_unavailable"
    assert list(workspace.iterdir()) == []


def test_real_parser_inherited_cwd_privilege_and_resource_caps(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    script = (
        "import json,os,resource\n"
        "blocked=False\n"
        "try:\n open('parser-marker','w').write('synthetic')\n"
        "except PermissionError:\n blocked=True\n"
        "print(json.dumps({'uid':os.geteuid(),'cpu':resource.getrlimit(resource.RLIMIT_CPU),"
        "'memory':resource.getrlimit(resource.RLIMIT_AS),'file':resource.getrlimit(resource.RLIMIT_FSIZE),"
        "'core':resource.getrlimit(resource.RLIMIT_CORE),'tmp':os.environ['TMPDIR'],'write_blocked':blocked}))"
    )
    with documents._parser_workspace(workspace) as (directory, descriptor):
        outcome, output = documents._run_parser(
            [_PYTHON, "-c", script], directory, descriptor, timeout=3, output_limit=4096
        )
        assert outcome == "ok"
        report = json.loads(output)
        assert report["cpu"] == [documents.PARSER_CPU_SECONDS] * 2
        assert report["memory"] == [documents.PARSER_MEMORY_BYTES] * 2
        assert report["file"] == [4096, 4096] and report["core"] == [0, 0] and report["tmp"] == "."
        if os.geteuid() == 0:
            assert report["uid"] != 0 and report["write_blocked"] and not (directory / "parser-marker").exists()
        else:
            assert report["uid"] == os.geteuid()
        assert directory.stat().st_uid == os.geteuid() and stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(workspace.stat().st_mode) == 0o700 and workspace.stat().st_uid == os.geteuid()
    assert list(workspace.iterdir()) == []


def test_real_parser_deadline_failure_and_file_output_limit(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    with documents._parser_workspace(workspace) as (directory, descriptor):
        outcome, output = documents._run_parser(
            [_PYTHON, "-c", "import time;time.sleep(10)"], directory, descriptor, timeout=0.05, output_limit=100
        )
        assert (outcome, output) == ("timeout", b"")
        assert (
            documents._run_parser(
                [_PYTHON, "-c", "raise SystemExit(3)"], directory, descriptor, timeout=2, output_limit=100
            )[0]
            == "failed"
        )
        outcome, output = documents._run_parser(
            [_PYTHON, "-c", "import os;os.write(1,b'x'*1000)"],
            directory,
            descriptor,
            timeout=2,
            output_limit=100,
        )
        assert outcome == "output_limit" and len(output) == 100
        assert (
            documents._run_parser(["/missing/parser"], directory, descriptor, timeout=2, output_limit=100)[0]
            == "unavailable"
        )
        assert documents._run_parser([_PYTHON], directory, descriptor, timeout=0, output_limit=100)[0] == "timeout"
    assert list(workspace.iterdir()) == []


def test_restrict_child_limits_and_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    limits: dict[int, tuple[int, int]] = {}
    monkeypatch.setattr(documents.resource, "setrlimit", lambda kind, values: limits.update({kind: values}))
    monkeypatch.setattr(documents.os, "geteuid", lambda: 0)
    changed: list[tuple[str, Any]] = []
    monkeypatch.setattr(documents.os, "setgroups", lambda values: changed.append(("groups", values)))
    monkeypatch.setattr(documents.os, "setgid", lambda value: changed.append(("gid", value)))
    monkeypatch.setattr(documents.os, "setuid", lambda value: changed.append(("uid", value)))
    documents._restrict_child(1234)
    assert limits[resource.RLIMIT_CPU] == (10, 10) and limits[resource.RLIMIT_FSIZE] == (1234, 1234)
    assert limits[resource.RLIMIT_CORE] == (0, 0)
    assert [name for name, _value in changed] == ["groups", "gid", "uid"]


def test_pdfinfo_failed_and_ocr_extract_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(documents.shutil, "which", lambda name: f"/usr/bin/{name}")
    with documents._parser_workspace(workspace) as (directory, descriptor):
        monkeypatch.setattr(documents, "_run_parser", lambda *_args, **_kwargs: ("failed", b"metadata"))
        assert documents._pdf_page_count(b"%PDF", directory, descriptor, 1e20) is None
    _fake_ocr(monkeypatch)
    original = documents._run_parser

    def parser(argv: list[str], *args: Any, **kwargs: Any) -> tuple[str, bytes]:
        if argv[0].endswith("tesseract") and "--list-langs" not in argv:
            return "failed", b""
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(documents, "_run_parser", parser)
    assert documents.ocr_pages(b"%PDF", [1], workspace)["error"] == "ocr_extract_failed"
    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize("remaining", [0.0, -1.0, float("nan"), float("inf"), float("-inf")])
def test_invalid_remaining_budget_fails_before_starting_parser(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remaining: float
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("A depleted budget must not launch a parser")

    monkeypatch.setattr(documents, "_run_parser", forbidden)
    workspace = _workspace(tmp_path)
    assert (
        documents.extract_content(b"%PDF synthetic", "application/pdf", "", workspace, timeout_seconds=remaining)[
            "error"
        ]
        == "pdf_timeout"
    )
    assert documents.ocr_pages(b"%PDF synthetic", [1], workspace, timeout_seconds=remaining)["error"] == "ocr_timeout"
    assert (
        documents.extract_content(b"<p>public</p>", "text/html", "", workspace, timeout_seconds=remaining)["error"]
        == "document_timeout"
    )
    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize("kind", ["pdf", "ocr"])
def test_small_remaining_budget_kills_real_parser_and_cleans_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(documents.shutil, "which", lambda name: None if name == "pdfinfo" else f"/usr/bin/{name}")
    original = documents._run_parser
    timeouts: list[float] = []

    def slow_parser(_argv: list[str], *args: Any, **kwargs: Any) -> tuple[str, bytes]:
        timeouts.append(kwargs["timeout"])
        return original([_PYTHON, "-c", "import time;time.sleep(10)"], *args, **kwargs)

    monkeypatch.setattr(documents, "_run_parser", slow_parser)
    started = time.monotonic()
    if kind == "pdf":
        result = documents.extract_content(b"%PDF synthetic", "application/pdf", "", workspace, timeout_seconds=0.05)
    else:
        result = documents.ocr_pages(b"%PDF synthetic", [1], workspace, timeout_seconds=0.05)
    assert not result["ok"] and result["error"] == f"{kind}_timeout"
    assert timeouts and 0 < timeouts[0] <= 0.05
    assert time.monotonic() - started < 1.0 and list(workspace.iterdir()) == []


def test_requested_budget_cannot_extend_default_parser_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_pdf(monkeypatch, b"text\f")
    original = documents._run_parser
    timeouts: list[float] = []

    def parser(*args: Any, **kwargs: Any) -> tuple[str, bytes]:
        timeouts.append(kwargs["timeout"])
        return original(*args, **kwargs)

    monkeypatch.setattr(documents, "_run_parser", parser)
    result = documents.extract_content(
        b"%PDF synthetic", "application/pdf", "", _workspace(tmp_path), timeout_seconds=1000
    )
    assert result["ok"] and 0 < timeouts[0] <= documents.PDF_TIMEOUT_SECONDS


def test_pdf_metadata_and_text_share_one_remaining_budget(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(documents.shutil, "which", lambda name: f"/usr/bin/{name}")
    clock = [100.0]
    monkeypatch.setattr(documents.time, "monotonic", lambda: clock[0])
    budgets: list[float] = []

    def parser(argv: list[str], *_args: Any, **kwargs: Any) -> tuple[str, bytes]:
        budgets.append(kwargs["timeout"])
        if argv[0].endswith("pdfinfo"):
            clock[0] += 0.08
            return "ok", b"Pages: 1\n"
        return "ok", b"public text\f"

    monkeypatch.setattr(documents, "_run_parser", parser)
    result = documents.extract_content(
        b"%PDF synthetic", "application/pdf", "", _workspace(tmp_path), timeout_seconds=0.1
    )
    assert result["ok"] and budgets == pytest.approx([0.1, 0.02])


def test_real_pdf_and_ocr_with_service_uid_gid_capabilities_only() -> None:
    """Reproduce the worker's capability set without changing any service."""
    if os.geteuid() != 0 or not shutil.which("setpriv"):
        pytest.skip("capability bounding test requires root and setpriv")
    if not all(shutil.which(name) for name in ("pdftotext", "pdftoppm", "tesseract")):
        pytest.skip("system PDF/OCR tools are not installed")
    if not Path("/dev/shm").is_dir():
        pytest.skip("private tmpfs workspace is unavailable")
    script = r'''
import json, os, re, stat, sys, tempfile
from pathlib import Path
from autostop_manager import j1_vin_documents as documents
body = sys.stdin.buffer.read()
status = Path('/proc/self/status').read_text()
capabilities = {name: int(re.search(r'^' + name + r':\s*([0-9a-f]+)', status, re.M).group(1), 16)
                for name in ('CapBnd', 'CapEff', 'CapAmb')}
probe = """import json,os
blocked=False
try:
 os.mkdir('child-owned-cache',0o700)
except PermissionError:
 blocked=True
capabilities={line.split(':')[0]:int(line.split()[1],16)
              for line in open('/proc/self/status') if line.startswith(('CapEff:', 'CapPrm:', 'CapAmb:'))}
print(json.dumps({'uid':os.geteuid(),'write_blocked':blocked,'capabilities':capabilities}))
"""
with tempfile.TemporaryDirectory(prefix='j1-vin-low-cap-', dir='/dev/shm') as directory:
    workspace = Path(directory)
    workspace.chmod(0o700)
    with documents._parser_workspace(workspace) as (parser_dir, descriptor):
        probe_status, probe_output = documents._run_parser(
            [str(Path(sys.executable).resolve()), '-I', '-c', probe], parser_dir, descriptor,
            timeout=3, output_limit=4096)
    decoded = documents.extract_content(body, 'application/pdf', 'https://example.org/synthetic.pdf', workspace)
    recognized = documents.ocr_pages(body, [456], workspace)
    print(json.dumps({
        'capabilities': capabilities, 'probe_status': probe_status,
        'child': json.loads(probe_output) if probe_status == 'ok' else None,
        'pdf_ok': decoded.get('ok'), 'pdf_error': decoded.get('error'),
        'page_count': decoded.get('page_count'),
        'late_page_found': decoded.get('ok') and len(decoded['pages']) == 456
                           and 'SYNTHETIC' in decoded['pages'][455]['text'],
        'ocr_ok': recognized.get('ok'), 'ocr_error': recognized.get('error'),
        'ocr_page': recognized['pages'][0]['page'] if recognized.get('pages') else None,
        'ocr_text_found': any('PUBLIC DOCUMENT' in page['text'] for page in recognized.get('pages', [])),
        'workspace_mode': stat.S_IMODE(workspace.stat().st_mode),
        'workspace_owner': workspace.stat().st_uid, 'remaining_files': len(list(workspace.iterdir()))
    }))
'''
    result = subprocess.run(
        [
            "setpriv",
            "--bounding-set=-all,+setuid,+setgid",
            "--inh-caps=-all,+setuid,+setgid",
            "--ambient-caps=-all,+setuid,+setgid",
            sys.executable,
            "-c",
            script,
        ],
        input=_pdf(456),
        capture_output=True,
        timeout=30,
        check=False,
        env={
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "PYTHONPATH": str(Path(documents.__file__).resolve().parents[1]),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONSAFEPATH": "1",
        },
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    report = json.loads(result.stdout)
    assert report["capabilities"] == {"CapBnd": 0xC0, "CapEff": 0xC0, "CapAmb": 0xC0}
    assert report["probe_status"] == "ok" and report["child"]["uid"] != 0 and report["child"]["write_blocked"]
    assert report["child"]["capabilities"] == {"CapEff": 0, "CapPrm": 0, "CapAmb": 0}
    assert report["pdf_ok"] and report["page_count"] == 456 and report["late_page_found"], report
    if report["ocr_error"] == "ocr_languages_unavailable":
        pytest.skip("rus+eng language data is not installed")
    assert report["ocr_ok"] and report["ocr_page"] == 456 and report["ocr_text_found"], report
    assert report["workspace_mode"] == 0o700 and report["workspace_owner"] == 0 and report["remaining_files"] == 0
