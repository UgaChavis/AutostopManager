"""Failure boundaries for the private, read-only offline catalog search."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from autostop_manager import offline_catalogs as catalogs


@pytest.fixture
def catalog_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "catalogs"
    (root / "text").mkdir(parents=True)
    monkeypatch.setenv(catalogs.CATALOG_ROOT_ENV, str(root))
    return root


def _entry(catalog_id: str = "demo", **overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "catalog_id": catalog_id,
        "brand": "Example",
        "title": "Parts",
        "format": "pdf",
        "text_path": f"text/{catalog_id}.txt",
        "text_extract_status": "ok",
    }
    entry.update(overrides)
    return entry


def _install(root: Path, entries: list[dict[str, object]], *, content: str = "\f[PAGE 1]\npart 12345\n") -> None:
    (root / "catalog_index.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")
    for entry in entries:
        catalog_id = str(entry["catalog_id"])
        suffix = ".tsv" if entry.get("format") == "xlsx" else ".txt"
        (root / "text" / f"{catalog_id}{suffix}").write_text(content, encoding="utf-8")


@pytest.mark.parametrize("configured", ["relative/catalogs", "/tmp/../catalogs"])
def test_root_must_be_absolute_without_parent_traversal(monkeypatch: pytest.MonkeyPatch, configured: str) -> None:
    monkeypatch.setenv(catalogs.CATALOG_ROOT_ENV, configured)
    assert catalogs.search_offline_parts_catalogs("12345")["error"] == "catalog_root_invalid"


def test_root_defaults_to_database_sibling(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(catalogs.CATALOG_ROOT_ENV, raising=False)
    monkeypatch.setattr(catalogs, "get_db_path", lambda: tmp_path / "manager.sqlite3")
    assert catalogs._catalog_root() == tmp_path / "offline_parts_catalogs"


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (None, "catalog_index_missing"),
        ("{bad json", "catalog_index_unreadable"),
        ("[]", "catalog_index_invalid"),
        ('{"entries": {}}', "catalog_index_invalid"),
    ],
)
def test_index_errors_are_explicit(catalog_root: Path, payload: str | None, error: str) -> None:
    if payload is not None:
        (catalog_root / "catalog_index.json").write_text(payload, encoding="utf-8")
    result = catalogs.search_offline_parts_catalogs("12345")
    assert result == {"ok": False, "error": error, "results": []}


def test_oversized_index_is_not_read(catalog_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalogs, "MAX_INDEX_BYTES", 2)
    (catalog_root / "catalog_index.json").write_text('{"entries": []}', encoding="utf-8")
    assert catalogs.search_offline_parts_catalogs("12345")["error"] == "catalog_index_too_large"


def test_index_discards_invalid_and_duplicate_catalog_ids(catalog_root: Path) -> None:
    entries = [_entry(), _entry(), _entry("invalid/id"), _entry("second"), {"brand": "No ID"}]
    (catalog_root / "catalog_index.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")
    loaded, error = catalogs._load_entries(catalog_root)
    assert error is None
    assert [entry["catalog_id"] for entry in loaded] == ["demo", "second"]


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"query": "ab"}, "query_invalid"),
        ({"query": "a" * 81}, "query_invalid"),
        ({"query": "part\n123"}, "query_invalid"),
        ({"query": "WDB12345678901234"}, "vin_query_rejected"),
        ({"query": "part", "limit": True}, "limit_invalid"),
        ({"query": "part", "limit": 21}, "limit_invalid"),
        ({"query": "part", "catalog_id": "../demo"}, "catalog_id_invalid"),
        ({"query": "part", "brand": "WDB12345678901234"}, "brand_invalid"),
    ],
)
def test_bad_requests_fail_before_reading_index(kwargs: dict[str, object], error: str) -> None:
    assert catalogs.search_offline_parts_catalogs(**kwargs)["error"] == error  # type: ignore[arg-type]


def test_unknown_catalog_is_distinct_from_empty_search(catalog_root: Path) -> None:
    _install(catalog_root, [_entry()])
    assert catalogs.search_offline_parts_catalogs("12345", catalog_id="other")["error"] == "catalog_not_found"
    result = catalogs.search_offline_parts_catalogs("missing", catalog_id="demo")
    assert result["ok"] is True
    assert result["results"] == []
    assert result["search_incomplete"] is False


def test_text_path_rejects_escape_and_symlink(catalog_root: Path, tmp_path: Path) -> None:
    assert catalogs._safe_text_path(catalog_root, _entry(text_path="../../demo.txt")) == catalog_root / "text/demo.txt"
    assert catalogs._safe_text_path(catalog_root, _entry(text_path="text/other.txt")) is None
    outside = tmp_path / "outside.txt"
    outside.write_text("part 12345", encoding="utf-8")
    (catalog_root / "text" / "demo.txt").symlink_to(outside)
    assert catalogs._safe_text_path(catalog_root, _entry()) is None


def test_missing_and_invalid_catalog_text_are_reported(catalog_root: Path) -> None:
    (catalog_root / "catalog_index.json").write_text(
        json.dumps({"entries": [_entry("missing"), _entry("wrong", text_path="text/mismatch.txt")]}), encoding="utf-8"
    )
    result = catalogs.search_offline_parts_catalogs("12345")
    assert result["scanned_catalogs"] == 0
    assert result["skipped_catalog_ids"] == ["missing", "wrong"]
    assert result["warnings"] == ["catalog_text_missing_or_invalid"]
    assert result["search_incomplete"] is True


def test_file_and_total_byte_limits_skip_without_search(catalog_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(catalog_root, [_entry("first"), _entry("second")], content="part 12345\n")
    monkeypatch.setattr(catalogs, "MAX_FILE_BYTES", 20)
    monkeypatch.setattr(catalogs, "MAX_TOTAL_BYTES", 15)
    monkeypatch.setattr(catalogs, "_rg_matches", lambda *_args, **_kwargs: ([], None))
    result = catalogs.search_offline_parts_catalogs("12345")
    assert result["scanned_catalogs"] == 1
    assert result["skipped_catalog_ids"] == ["second"]
    assert "catalog_size_limit" in result["warnings"]
    monkeypatch.setattr(catalogs, "MAX_FILE_BYTES", 5)
    assert catalogs.search_offline_parts_catalogs("12345")["skipped_catalog_ids"] == ["first", "second"]


def test_count_limit_reports_unsearched_catalogs(catalog_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(catalog_root, [_entry("first"), _entry("second")])
    monkeypatch.setattr(catalogs, "MAX_CATALOGS", 1)
    monkeypatch.setattr(catalogs, "_rg_matches", lambda *_args, **_kwargs: ([], None))
    result = catalogs.search_offline_parts_catalogs("12345")
    assert result["selected_catalogs"] == 2
    assert result["scanned_catalogs"] == 1
    assert result["skipped_catalog_ids"] == ["second"]
    assert "catalog_count_limit" in result["warnings"]


def test_result_limit_reports_unsearched_catalogs(catalog_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(catalog_root, [_entry("first"), _entry("second")])
    monkeypatch.setattr(catalogs, "_rg_matches", lambda *_args, **_kwargs: ([(2, "part 12345")], None))
    result = catalogs.search_offline_parts_catalogs("12345", limit=1)
    assert result["result_limit_reached"] is True
    assert result["skipped_catalog_ids"] == ["second"]
    assert result["scanned_catalogs"] == 1
    assert len(result["results"]) == 1


def test_partial_extraction_and_ocr_page_are_marked(catalog_root: Path) -> None:
    _install(
        catalog_root,
        [_entry(text_extract_status="partial_ocr", empty_pages=[2], ocr_pages=[1, "2", -1])],
    )
    result = catalogs.search_offline_parts_catalogs("12345")
    assert result["partially_extracted_catalog_ids"] == ["demo"]
    assert "catalog_extraction_partial" in result["warnings"]
    assert result["results"][0]["locator"] == {"page": 1}
    assert result["results"][0]["extraction_method"] == "ocr"
    assert result["results"][0]["ocr_unverified"] is True


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (FileNotFoundError(), "ripgrep_unavailable"),
        (subprocess.TimeoutExpired("rg", 1), "search_timeout"),
        (OSError(), "catalog_read_failed"),
    ],
)
def test_ripgrep_process_failures_are_bounded(
    catalog_root: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception, expected: str
) -> None:
    _install(catalog_root, [_entry()])

    def fail(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(catalogs.subprocess, "run", fail)
    result = catalogs.search_offline_parts_catalogs("12345")
    assert result["results"] == []
    assert result["skipped_catalog_ids"] == ["demo"]
    assert result["warnings"] == [expected]


def test_ripgrep_nonstandard_exit_code_and_malformed_output(
    catalog_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(catalog_root, [_entry()])
    path = catalog_root / "text/demo.txt"
    monkeypatch.setattr(
        catalogs.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 2, stdout="", stderr="private error"),
    )
    assert catalogs._rg_matches(path, "12345", 2, time.monotonic() + 10, is_pdf=True) == (
        [],
        "catalog_search_failed",
    )
    monkeypatch.setattr(
        catalogs.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout="bad\n2:part 12345\n", stderr=""),
    )
    assert catalogs._rg_matches(path, "12345", 2, time.monotonic() + 10, is_pdf=True) == (
        [(2, "part 12345")],
        None,
    )


def test_expired_deadline_and_unavailable_locator_are_reported(
    catalog_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(catalog_root, [_entry()])
    path = catalog_root / "text/demo.txt"
    assert catalogs._rg_matches(path, "12345", 1, time.monotonic() - 1, is_pdf=True) == ([], "search_timeout")
    monkeypatch.setattr(catalogs, "_rg_matches", lambda *_args, **_kwargs: ([(2, "part 12345")], None))

    def locator_timeout(*_args: object, **_kwargs: object) -> None:
        raise TimeoutError

    monkeypatch.setattr(catalogs, "_pdf_locators", locator_timeout)
    result = catalogs.search_offline_parts_catalogs("12345")
    assert result["warnings"] == ["catalog_locator_unavailable"]
    assert result["skipped_catalog_ids"] == ["demo"]


def test_search_deadline_expires_before_next_catalog(catalog_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(catalog_root, [_entry("first"), _entry("second")])
    clock = iter([1.0, 1.0, 22.0])
    monkeypatch.setattr(catalogs.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(catalogs, "_rg_matches", lambda *_args, **_kwargs: ([], None))
    result = catalogs.search_offline_parts_catalogs("12345")
    assert result["scanned_catalogs"] == 1
    assert result["skipped_catalog_ids"] == ["second"]
    assert result["warnings"] == ["search_timeout"]


def test_tsv_invalid_row_and_sanitized_result(catalog_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = _entry(
        format="xlsx",
        text_path="text/demo.tsv",
        title="Cross WDB12345678901234",
        source_url="https://example.test/catalog?q=secret#private",
        limits="WDB12345678901234 requires confirmation",
    )
    _install(catalog_root, [entry], content="Sheet\t12\tcode:part 12345\n")
    monkeypatch.setattr(
        catalogs,
        "_rg_matches",
        lambda *_args, **_kwargs: ([(1, "malformed"), (1, "Sheet\t12\tcode:part 12345 WDB12345678901234")], None),
    )
    result = catalogs.search_offline_parts_catalogs("12345")
    assert result["warnings"] == ["catalog_row_invalid"]
    hit = result["results"][0]
    assert hit["locator"] == {"sheet": "Sheet", "row": 12}
    assert hit["source_url"] == "https://example.test/catalog"
    assert "secret" not in json.dumps(hit)
    assert "WDB12345678901234" not in json.dumps(hit)
    assert "[VIN REDACTED]" in hit["excerpt"]
    assert hit["candidate_only"] is True
    assert hit["fitment_status"] == "unverified_catalog_candidate"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:password@example.test/file", "https://[broken"])
def test_source_url_rejects_unsafe_values(url: str) -> None:
    assert catalogs._source_url(url) == ""
