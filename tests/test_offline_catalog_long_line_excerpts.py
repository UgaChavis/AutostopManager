from __future__ import annotations

import json
import subprocess
import time

import pytest

from autostop_manager import offline_catalogs as catalogs


@pytest.fixture
def install_catalog(tmp_path, monkeypatch):
    root = tmp_path / "catalogs"
    (root / "text").mkdir(parents=True)
    monkeypatch.setenv(catalogs.CATALOG_ROOT_ENV, str(root))

    def install(content, *, format="pdf", suffix=".txt", **metadata):
        path = root / "text" / ("demo" + suffix)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        entry = {
            "catalog_id": "demo",
            "text_path": "text/" + path.name,
            "format": format,
            "text_extract_status": "extracted",
            **metadata,
        }
        (root / "catalog_index.json").write_text(json.dumps({"entries": [entry]}), encoding="utf-8")
        return path

    return install


@pytest.fixture
def rg_output(monkeypatch):
    run = catalogs.subprocess.run
    outputs = []

    def capture(*args, **kwargs):
        result = run(*args, **kwargs)
        outputs.append(result.stdout)
        return result

    monkeypatch.setattr(catalogs.subprocess, "run", capture)
    return outputs


@pytest.mark.parametrize(
    "prefix,format,suffix,locator,metadata",
    [
        ("\f[PAGE 7]\nHeading\n", "pdf", ".txt", {"page": 7}, {}),
        ("First page\n\f", "pdf", ".txt", {"page": 2}, {}),
        ("Sheet1\t12\tNote:", "xlsx", ".tsv", {"sheet": "Sheet1", "row": 12}, {}),
        ("### Cross-reference\nHeading\n", "xlsx", ".txt", {"sheet": "Cross-reference", "row": 2}, {}),
        ("\f[PAGE 7]\nHeading\n", "pdf", ".txt", {"page": 7}, {"ocr_pages": [7]}),
    ],
    ids=["modern-pdf", "legacy-pdf", "xlsx-tsv", "legacy-xlsx", "ocr-pdf"],
)
def test_long_catalog_row_returns_context_around_actual_article(
    install_catalog, rg_output, prefix, format, suffix, locator, metadata
):
    install_catalog(
        prefix + "x" * 21000 + " left ABC123 right " + "y" * 1000 + "\n", format=format, suffix=suffix, **metadata
    )

    result = catalogs.search_offline_parts_catalogs("ABC123", limit=1)

    assert result["ok"] is True
    assert result["warnings"] == []
    assert len(result["results"]) == 1
    hit = result["results"][0]
    assert "left ABC123 right" in hit["excerpt"]
    assert len(hit["excerpt"]) <= catalogs.MAX_EXCERPT_CHARS
    assert hit["locator"] == locator
    assert hit["candidate_only"] is True
    assert hit["fitment_status"] == "unverified_catalog_candidate"
    if format == "pdf":
        assert hit["ocr_unverified"] is bool(metadata)
    assert len(rg_output[0].encode()) < 2048


@pytest.mark.parametrize("bom", ["", "\ufeff"], ids=["plain", "bom"])
def test_long_pdf_row_does_not_let_page_marker_consume_result_limit(install_catalog, bom):
    install_catalog(bom + "\f[PAGE 123]\nHeading\n" + "x" * 21000 + " ABC123 fits listed model\n")

    result = catalogs.search_offline_parts_catalogs("123", limit=1)

    hit = result["results"][0]
    assert hit["locator"] == {"page": 123}
    assert "Heading" in hit["excerpt"]
    assert "ABC123" in hit["excerpt"]
    assert "[PAGE 123]" not in hit["excerpt"]


def test_pdf_previous_context_and_max_length_query_fit_in_one_excerpt(install_catalog):
    query = "PART-" * 15 + "ABCDE"
    assert len(query) == catalogs.MAX_QUERY_CHARS
    install_catalog("\f[PAGE 1]\n" + "\ufb03" * 100 + "\n" + "\ufb03" * 21000 + query + " tail " + "y" * 1000 + "\n")

    result = catalogs.search_offline_parts_catalogs(query)

    hit = result["results"][0]
    assert query in hit["excerpt"]
    assert " | " in hit["excerpt"]
    assert len(hit["excerpt"]) <= catalogs.MAX_EXCERPT_CHARS


def test_unicode_and_regex_punctuation_remain_literal_on_long_rows(install_catalog):
    install_catalog("Sheet1\t2\t" + "Ж" * 21000 + " фильтр.42+[x] хвост\n", format="xlsx", suffix=".tsv")

    result = catalogs.search_offline_parts_catalogs("ФИЛЬТР.42+[X]")
    absent = catalogs.search_offline_parts_catalogs("ФИЛЬТРx42+[X]")

    assert "фильтр.42+[x]" in result["results"][0]["excerpt"]
    assert absent["results"] == []
    assert absent["search_incomplete"] is False


def test_tsv_sheet_name_is_not_a_data_match(install_catalog):
    install_catalog("August Sheet\t2\t" + "x" * 21000 + " another article\n", format="xlsx", suffix=".tsv")

    result = catalogs.search_offline_parts_catalogs("August", limit=1)

    assert result["results"] == []
    assert result["search_incomplete"] is False


def test_many_query_occurrences_in_one_row_keep_stdout_bounded(install_catalog, rg_output):
    install_catalog("Sheet1\t2\t" + "ABC123 " * 10000 + "\nSheet1\t3\tABC123\n", format="xlsx", suffix=".tsv")

    result = catalogs.search_offline_parts_catalogs("ABC123", limit=1)

    assert len(result["results"]) == 1
    assert result["results"][0]["locator"] == {"sheet": "Sheet1", "row": 2}
    assert len([line for line in rg_output[0].split("\n") if line]) == 1
    assert len(rg_output[0].encode()) < 2048


def test_physical_matching_rows_still_obey_result_limit(install_catalog, rg_output):
    content = "".join(f"Sheet1\t{row}\t" + "x" * 21000 + " ABC123 tail\n" for row in range(1, 5))
    install_catalog(content, format="xlsx", suffix=".tsv")

    result = catalogs.search_offline_parts_catalogs("ABC123", limit=2)

    assert [hit["locator"]["row"] for hit in result["results"]] == [1, 2]
    assert all("ABC123" in hit["excerpt"] for hit in result["results"])
    assert len([line for line in rg_output[0].split("\n") if line]) == 2


@pytest.mark.parametrize("sheet_size", [19950, 30000])
def test_oversized_locator_keeps_output_guard_and_reports_incomplete_search(install_catalog, rg_output, sheet_size):
    install_catalog("S" * sheet_size + "\t2\t" + "x" * 21000 + " ABC123 tail\n", format="xlsx", suffix=".tsv")

    result = catalogs.search_offline_parts_catalogs("ABC123")

    assert result["ok"] is True
    assert result["results"] == []
    assert result["search_incomplete"] is True
    assert result["warnings"] == ["catalog_excerpt_truncated"]
    assert result["skipped_catalog_ids"] == ["demo"]
    assert len(rg_output[0].encode()) < 20200


def test_missing_private_marker_is_not_accepted_as_a_complete_match(install_catalog, monkeypatch):
    install_catalog("Sheet1\t2\tABC123\n", format="xlsx", suffix=".tsv")
    monkeypatch.setattr(
        catalogs.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout="1:Sheet1\t2\tABC123\n", stderr=""),
    )

    result = catalogs.search_offline_parts_catalogs("ABC123")

    assert result["results"] == []
    assert result["search_incomplete"] is True
    assert result["warnings"] == ["catalog_excerpt_truncated"]


def test_only_added_marker_is_removed_from_the_matched_source_content(install_catalog):
    content = "ABC123 actual source\x1e"
    path = install_catalog(content + "\n")

    matches, error = catalogs._rg_matches(path, "ABC123", 1, time.monotonic() + 5, is_pdf=False)

    assert error is None
    assert matches == [(1, content)]


def test_legacy_literal_match_survives_invalid_utf8_before_long_context(install_catalog):
    install_catalog(b"\xff" + b"x" * 21000 + b" ABC123 tail\n")

    result = catalogs.search_offline_parts_catalogs("ABC123")

    assert result["warnings"] == []
    assert "ABC123 tail" in result["results"][0]["excerpt"]
    assert result["results"][0]["locator"] == {"page": 1}


def test_long_row_excerpt_keeps_vin_redaction(install_catalog):
    vin = "WDB12345678901234"
    install_catalog("Sheet1\t2\t" + "x" * 21000 + f" ABC123 crossref {vin} tail\n", format="xlsx", suffix=".tsv")

    result = catalogs.search_offline_parts_catalogs("ABC123")

    excerpt = result["results"][0]["excerpt"]
    assert "ABC123" in excerpt
    assert vin not in excerpt
    assert "[VIN REDACTED]" in excerpt


def test_right_capture_edge_preserves_existing_full_vin_redaction(install_catalog):
    vin = "A" * 17
    body = "x" * 21000 + " NEEDLE " + "r" * 120 + " " + vin + " tail"
    install_catalog("\f[PAGE 1]\nHeading\n" + body + "\n")
    previous_excerpt = catalogs._excerpt("Heading | " + body, "NEEDLE")
    assert "[VIN REDACTED]" in previous_excerpt
    assert vin[:16] not in previous_excerpt

    result = catalogs.search_offline_parts_catalogs("NEEDLE")

    excerpt = result["results"][0]["excerpt"]
    assert "NEEDLE" in excerpt
    assert "[VIN REDACTED]" in excerpt
    assert vin[:16] not in excerpt
    assert len(excerpt) <= catalogs.MAX_EXCERPT_CHARS
    assert result["warnings"] == []


@pytest.mark.parametrize(
    "prefix,format,suffix,padding",
    [
        ("\f[PAGE 1]\nHeading\n", "pdf", ".txt", 122),
        ("\f[PAGE 1]\nHeading\n", "pdf", ".txt", 107),
        ("Sheet1\t2\t", "xlsx", ".tsv", 225),
        ("Sheet1\t2\t", "xlsx", ".tsv", 210),
    ],
    ids=["pdf-one-visible", "pdf-sixteen-visible", "tsv-one-visible", "tsv-sixteen-visible"],
)
def test_right_boundary_vin_is_masked_before_visible_window_is_returned(
    install_catalog, prefix, format, suffix, padding
):
    install_catalog(
        prefix + "x" * 21000 + " NEEDLE " + "r" * padding + " " + "A" * 17 + " tail\n", format=format, suffix=suffix
    )

    result = catalogs.search_offline_parts_catalogs("NEEDLE")

    excerpt = result["results"][0]["excerpt"]
    assert "NEEDLE" in excerpt
    assert "[VIN REDACTED]" in excerpt
    assert "A" not in excerpt.replace("[VIN REDACTED]", "")
    assert len(excerpt) <= catalogs.MAX_EXCERPT_CHARS


@pytest.mark.parametrize("padding", [122, 107], ids=["one-visible", "sixteen-visible"])
def test_right_halo_retains_alphanumeric_neighbor_that_disqualifies_vin(install_catalog, padding):
    body = "x" * 21000 + " NEEDLE " + "r" * padding + " " + "A" * 17 + "Z tail\n"
    install_catalog("\f[PAGE 1]\nHeading\n" + body)

    result = catalogs.search_offline_parts_catalogs("NEEDLE")

    excerpt = result["results"][0]["excerpt"]
    assert "NEEDLE" in excerpt
    assert "[VIN REDACTED]" not in excerpt
    assert excerpt.endswith("A" if padding == 122 else "A" * 16)


@pytest.mark.parametrize("padding", [48, 33], ids=["one-visible", "sixteen-visible"])
def test_max_query_pdf_previous_context_and_complete_redaction_share_budget(install_catalog, padding):
    query = "PART-" * 15 + "ABCDE"
    body = "x" * 21000 + " " + query + " " + "r" * padding + " " + "1" * 17 + " tail\n"
    install_catalog("\f[PAGE 1]\n" + "\ufb03" * 100 + "\n" + body)

    result = catalogs.search_offline_parts_catalogs(query)

    excerpt = result["results"][0]["excerpt"]
    assert query in excerpt
    assert " | " in excerpt
    assert "[VIN REDACTED]" in excerpt
    assert "1" not in excerpt
    assert len(excerpt) <= catalogs.MAX_EXCERPT_CHARS


def test_halo_only_identifier_is_not_appended_to_the_visible_excerpt(install_catalog):
    install_catalog("\f[PAGE 1]\nHeading\n" + "x" * 21000 + " NEEDLE " + "r" * 127 + " " + "A" * 17 + " tail\n")

    result = catalogs.search_offline_parts_catalogs("NEEDLE")

    excerpt = result["results"][0]["excerpt"]
    assert "NEEDLE" in excerpt
    assert "[VIN REDACTED]" not in excerpt
    assert "A" not in excerpt


def test_vin_redaction_does_not_rewrite_tsv_locator(install_catalog):
    sheet = "A" * 17
    install_catalog(sheet + "\t2\t" + "x" * 21000 + " NEEDLE " + "B" * 17 + " tail\n", format="xlsx", suffix=".tsv")

    result = catalogs.search_offline_parts_catalogs("NEEDLE")

    hit = result["results"][0]
    assert hit["locator"] == {"sheet": sheet, "row": 2}
    assert "[VIN REDACTED]" in hit["excerpt"]
    assert "B" * 17 not in hit["excerpt"]


def test_source_separator_sequence_is_preserved_when_private_wire_fields_are_removed(install_catalog):
    content = "ABC123 actual source\x1f" + "\x1e" * 17
    path = install_catalog(content + "\n")

    matches, error = catalogs._rg_matches(path, "ABC123", 1, time.monotonic() + 5, is_pdf=False)

    assert error is None
    assert matches == [(1, content)]
