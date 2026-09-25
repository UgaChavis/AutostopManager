"""Behavioral checks for the private, read-only offline catalog search."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from autostop_manager import offline_catalogs as catalogs


@pytest.fixture
def cache_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "catalogs"
    (root / "text").mkdir(parents=True)
    monkeypatch.setenv(catalogs.CATALOG_ROOT_ENV, str(root))
    return root


def _entry(
    root: Path,
    catalog_id: str,
    content: str | None,
    *,
    format: str = "pdf",
    suffix: str = ".txt",
    **metadata: object,
) -> dict[str, object]:
    text_path = f"text/{catalog_id}{suffix}"
    if content is not None:
        (root / text_path).write_text(content, encoding="utf-8")
    return {
        "catalog_id": catalog_id,
        "text_path": text_path,
        "format": format,
        "brand": "Example Parts",
        "title": "Example catalog",
        "edition": "2026",
        "market_scope": "EU",
        "product_scope": "Filters",
        "limits": "Confirm dimensions and fitment",
        "sha256": "a" * 64,
        **metadata,
    }


def _index(root: Path, *entries: dict[str, object]) -> None:
    (root / "catalog_index.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")


def test_pdf_search_excludes_page_markers_before_result_limit(cache_root: Path) -> None:
    entry = _entry(
        cache_root,
        "pdf_a",
        "\f[PAGE 123]\nHeading\nPart ABC123 fits listed model\n\f[PAGE 124]\nOther part\n",
    )
    _index(cache_root, entry)

    result = catalogs.search_offline_parts_catalogs("123", limit=1)

    assert result["ok"] is True
    assert result["scanned_catalogs"] == 1
    assert result["result_limit_reached"] is True
    assert len(result["results"]) == 1
    hit = result["results"][0]
    assert hit["locator"] == {"page": 123}
    assert hit["extraction_method"] == "text"
    assert hit["ocr_unverified"] is False
    assert "Heading" in hit["excerpt"]
    assert "ABC123" in hit["excerpt"]
    assert "[PAGE 123]" not in hit["excerpt"]
    assert hit["candidate_only"] is True
    assert hit["fitment_status"] == "unverified_catalog_candidate"


def test_pdf_ocr_pages_are_flagged_separately(cache_root: Path) -> None:
    entry = _entry(
        cache_root,
        "pdf_ocr",
        "\f[PAGE 1]\nGMB-900 verified text\n\f[PAGE 2]\n[OCR UNVERIFIED: eng+rus]\nGMB-900 OCR text\n",
        ocr_pages=[2, "2", 0],
    )
    _index(cache_root, entry)

    result = catalogs.search_offline_parts_catalogs("GMB-900")

    assert [(hit["locator"], hit["extraction_method"], hit["ocr_unverified"]) for hit in result["results"]] == [
        ({"page": 1}, "text", False),
        ({"page": 2}, "ocr", True),
    ]
    assert result["search_incomplete"] is False


def test_tsv_search_uses_data_fields_and_reports_sheet_row(cache_root: Path) -> None:
    entry = _entry(
        cache_root,
        "xlsx_a",
        "New Product_August\t1\t\nNew Product_August\t2\tPart: A.B123; OE: 555\n",
        format="xlsx",
        suffix=".tsv",
    )
    _index(cache_root, entry)

    sheet_only = catalogs.search_offline_parts_catalogs("August", limit=1)
    literal_dot = catalogs.search_offline_parts_catalogs("A.B123", limit=1)
    wrong_dot = catalogs.search_offline_parts_catalogs("AXB123", limit=1)

    assert sheet_only["results"] == []
    assert sheet_only["search_incomplete"] is False
    assert wrong_dot["results"] == []
    assert len(literal_dot["results"]) == 1
    hit = literal_dot["results"][0]
    assert hit["locator"] == {"sheet": "New Product_August", "row": 2}
    assert hit["excerpt"] == "Part: A.B123; OE: 555"
    assert "extraction_method" not in hit


def test_legacy_pdf_and_xlsx_text_cache_remain_searchable(cache_root: Path) -> None:
    pdf = _entry(cache_root, "legacy_pdf", "First page\n\fSecond page LEGACY-77\n")
    xlsx = _entry(
        cache_root,
        "legacy_xlsx",
        "### Cross-reference\nHeader\nLEGACY-77 maps to OE-42\n",
        format="xlsx",
    )
    _index(cache_root, pdf, xlsx)

    result = catalogs.search_offline_parts_catalogs("LEGACY-77")

    assert len(result["results"]) == 2
    assert result["results"][0]["locator"] == {"page": 2}
    assert result["results"][1]["locator"] == {"sheet": "Cross-reference", "row": 2}
    assert result["scanned_catalogs"] == 2
    assert result["search_incomplete"] is False


def test_brand_and_catalog_filters_select_only_requested_catalog(cache_root: Path) -> None:
    first = _entry(cache_root, "brake_a", "BP-900 front\n", brand="Brand A")
    second = _entry(cache_root, "brake_b", "BP-900 rear\n", brand="Brand B")
    _index(cache_root, first, second)

    by_brand = catalogs.search_offline_parts_catalogs("BP-900", brand="brand b")
    by_id = catalogs.search_offline_parts_catalogs("BP-900", catalog_id="brake_a")
    absent = catalogs.search_offline_parts_catalogs("BP-900", catalog_id="unknown")

    assert [hit["catalog_id"] for hit in by_brand["results"]] == ["brake_b"]
    assert by_brand["selected_catalogs"] == 1
    assert [hit["catalog_id"] for hit in by_id["results"]] == ["brake_a"]
    assert absent == {"ok": False, "error": "catalog_not_found", "results": []}


def test_result_limit_marks_unscanned_catalogs(cache_root: Path) -> None:
    _index(
        cache_root,
        _entry(cache_root, "first", "ITEM-100\n"),
        _entry(cache_root, "second", "ITEM-100\n"),
    )

    result = catalogs.search_offline_parts_catalogs("ITEM-100", limit=1)

    assert [hit["catalog_id"] for hit in result["results"]] == ["first"]
    assert result["selected_catalogs"] == 2
    assert result["scanned_catalogs"] == 1
    assert result["result_limit_reached"] is True
    assert result["search_incomplete"] is True
    assert result["skipped_catalog_ids"] == ["second"]
    assert result["skipped_catalog_count"] == 1


def test_missing_text_and_partial_extraction_are_visible(cache_root: Path) -> None:
    missing = _entry(cache_root, "missing", None)
    partial = _entry(
        cache_root,
        "partial",
        "\f[PAGE 1]\nARTICLE-10\n",
        text_extract_status="partial_text",
        empty_pages=[2],
    )
    _index(cache_root, missing, partial)

    result = catalogs.search_offline_parts_catalogs("ARTICLE-10")

    assert result["ok"] is True
    assert result["scanned_catalogs"] == 1
    assert result["skipped_catalog_ids"] == ["missing"]
    assert result["partially_extracted_catalog_ids"] == ["partial"]
    assert set(result["warnings"]) == {"catalog_text_missing_or_invalid", "catalog_extraction_partial"}
    assert result["search_incomplete"] is True
    assert result["results"][0]["locator"] == {"page": 1}


def test_unsafe_symlink_is_not_read(cache_root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET-ARTICLE\n", encoding="utf-8")
    (cache_root / "text" / "outside_link.txt").symlink_to(outside)
    _index(cache_root, _entry(cache_root, "outside_link", None))

    result = catalogs.search_offline_parts_catalogs("SECRET-ARTICLE")

    assert result["results"] == []
    assert result["skipped_catalog_ids"] == ["outside_link"]
    assert "catalog_text_missing_or_invalid" in result["warnings"]


def test_url_and_metadata_are_sanitized(cache_root: Path) -> None:
    vin = "WDB12345678901234"
    entry = _entry(
        cache_root,
        "safe_source",
        f"ARTICLE-42 crossref {vin}\n",
        title=f"Catalog for {vin}",
        limits=f"Check vehicle {vin}",
        source_url="https://example.test/catalog.pdf?token=do-not-show#fragment",
    )
    _index(cache_root, entry)

    result = catalogs.search_offline_parts_catalogs("ARTICLE-42")
    hit = result["results"][0]

    assert hit["source_url"] == "https://example.test/catalog.pdf"
    assert hit["title"] == "Catalog for [VIN REDACTED]"
    assert hit["limits"] == "Check vehicle [VIN REDACTED]"
    assert vin not in hit["excerpt"]
    assert "[VIN REDACTED]" in hit["excerpt"]


@pytest.mark.parametrize(
    ("query", "error"),
    [
        (None, "query_invalid"),
        ("AB", "query_invalid"),
        ("X" * 81, "query_invalid"),
        ("ABC\n123", "query_invalid"),
        ("WDB12345678901234", "vin_query_rejected"),
    ],
)
def test_invalid_queries_are_rejected_before_reading_catalog(cache_root: Path, query: object, error: str) -> None:
    assert catalogs.search_offline_parts_catalogs(query)["error"] == error  # type: ignore[arg-type]


@pytest.mark.parametrize("limit", [0, 21, True, "2"])
def test_invalid_limits_are_rejected(cache_root: Path, limit: object) -> None:
    assert catalogs.search_offline_parts_catalogs("ARTICLE", limit=limit)["error"] == "limit_invalid"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"catalog_id": "../escape"}, "catalog_id_invalid"),
        ({"catalog_id": 42}, "catalog_id_invalid"),
        ({"brand": "X" * 81}, "brand_invalid"),
        ({"brand": "WDB12345678901234"}, "brand_invalid"),
        ({"brand": 42}, "brand_invalid"),
    ],
)
def test_invalid_filters_are_rejected(cache_root: Path, kwargs: dict[str, object], error: str) -> None:
    assert catalogs.search_offline_parts_catalogs("ARTICLE", **kwargs)["error"] == error  # type: ignore[arg-type]


def test_relative_catalog_root_is_rejected(cache_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(catalogs.CATALOG_ROOT_ENV, "relative/catalogs")

    assert catalogs.search_offline_parts_catalogs("ARTICLE")["error"] == "catalog_root_invalid"


@pytest.mark.parametrize(
    ("index_content", "error"),
    [
        (None, "catalog_index_missing"),
        ("not JSON", "catalog_index_unreadable"),
        ('{"entries":{}}', "catalog_index_invalid"),
    ],
)
def test_missing_or_invalid_index_is_reported(cache_root: Path, index_content: str | None, error: str) -> None:
    if index_content is not None:
        (cache_root / "catalog_index.json").write_text(index_content, encoding="utf-8")

    assert catalogs.search_offline_parts_catalogs("ARTICLE")["error"] == error


def test_duplicate_and_invalid_index_entries_are_ignored(cache_root: Path) -> None:
    valid = _entry(cache_root, "valid", "ARTICLE-11\n")
    (cache_root / "catalog_index.json").write_text(
        json.dumps({"entries": [None, {"catalog_id": "../bad"}, valid, valid]}), encoding="utf-8"
    )

    result = catalogs.search_offline_parts_catalogs("ARTICLE-11")

    assert result["selected_catalogs"] == 1
    assert result["scanned_catalogs"] == 1
    assert len(result["results"]) == 1


def test_index_and_catalog_size_limits_report_incomplete_search(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _index(cache_root, _entry(cache_root, "large", "ARTICLE-99\n"))
    monkeypatch.setattr(catalogs, "MAX_INDEX_BYTES", 2)
    assert catalogs.search_offline_parts_catalogs("ARTICLE")["error"] == "catalog_index_too_large"

    monkeypatch.setattr(catalogs, "MAX_INDEX_BYTES", 2 * 1024 * 1024)
    monkeypatch.setattr(catalogs, "MAX_FILE_BYTES", 2)
    result = catalogs.search_offline_parts_catalogs("ARTICLE")
    assert result["results"] == []
    assert result["skipped_catalog_ids"] == ["large"]
    assert "catalog_size_limit" in result["warnings"]
    assert result["search_incomplete"] is True


def test_catalog_count_limit_is_explicit(cache_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _index(
        cache_root,
        _entry(cache_root, "one", "ARTICLE-1\n"),
        _entry(cache_root, "two", "ARTICLE-2\n"),
        _entry(cache_root, "three", "ARTICLE-3\n"),
    )
    monkeypatch.setattr(catalogs, "MAX_CATALOGS", 2)

    result = catalogs.search_offline_parts_catalogs("NOMATCH")

    assert result["selected_catalogs"] == 3
    assert result["scanned_catalogs"] == 2
    assert result["skipped_catalog_ids"] == ["three"]
    assert "catalog_count_limit" in result["warnings"]


def test_unavailable_ripgrep_is_reported(cache_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _index(cache_root, _entry(cache_root, "one", "ARTICLE-1\n"))

    def unavailable(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("rg")

    monkeypatch.setattr(catalogs.subprocess, "run", unavailable)

    result = catalogs.search_offline_parts_catalogs("ARTICLE")

    assert result["results"] == []
    assert result["skipped_catalog_ids"] == ["one"]
    assert result["warnings"] == ["ripgrep_unavailable"]


def test_ripgrep_timeout_is_reported(cache_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _index(cache_root, _entry(cache_root, "one", "ARTICLE-1\n"))

    def timed_out(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="rg", timeout=0.1)

    monkeypatch.setattr(catalogs.subprocess, "run", timed_out)

    result = catalogs.search_offline_parts_catalogs("ARTICLE")

    assert result["results"] == []
    assert result["warnings"] == ["search_timeout"]
    assert result["search_incomplete"] is True


def test_expired_deadline_does_not_start_ripgrep(cache_root: Path) -> None:
    path = cache_root / "text" / "sample.txt"
    path.write_text("ARTICLE-1\n", encoding="utf-8")

    matches, error = catalogs._rg_matches(path, "ARTICLE", 1, time.monotonic() - 1, is_pdf=False)

    assert matches == []
    assert error == "search_timeout"
