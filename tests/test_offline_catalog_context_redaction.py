"""Bounded metadata and previous-PDF context redaction before their raw cuts."""

from __future__ import annotations

import json
import socket
import sqlite3
import time

import pytest

from autostop_manager import offline_catalogs as catalogs

LABEL = "[VIN REDACTED]"


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    root = tmp_path / "catalogs"
    (root / "text").mkdir(parents=True)
    monkeypatch.setenv(catalogs.CATALOG_ROOT_ENV, str(root))

    def denied(*_args, **_kwargs):
        raise AssertionError("Context redaction tests cannot use network or runtime storage")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(sqlite3, "connect", denied)
    monkeypatch.setattr(catalogs, "get_db_path", denied)
    run = catalogs.subprocess.run

    def actual_rg(command, **kwargs):
        assert command[0] == "rg" and command[-2] == "--"
        assert command[command.index("--max-columns") + 1] == "20000"
        assert command[command.index("--max-count") + 1] == "8"
        assert not kwargs.get("shell", False) and 0 < kwargs["timeout"] <= catalogs.MAX_SEARCH_SECONDS
        result = run(command, **{**kwargs, "timeout": min(kwargs["timeout"], 3)})
        assert result.returncode == 0 and len(result.stdout.encode()) < 2048
        return result

    monkeypatch.setattr(catalogs.subprocess, "run", actual_rg)

    def install(content, *, format="pdf", **metadata):
        path = root / "text/demo.txt"
        path.write_text(content, encoding="utf-8")
        entry = {
            "catalog_id": "demo",
            "format": format,
            "text_path": "text/demo.txt",
            "text_extract_status": "extracted",
            **metadata,
        }
        (root / "catalog_index.json").write_text(json.dumps({"entries": [entry]}), encoding="utf-8")
        return path

    return install


def _hit(path, query="DEMO-PART", *, locator=None):
    response = catalogs.search_offline_parts_catalogs(query)
    assert response["ok"] is True and response["warnings"] == []
    assert response["search_incomplete"] is False and response["result_limit_reached"] is False
    assert response["scanned_catalogs"] == 1 and response["scanned_bytes"] == path.stat().st_size
    assert response["skipped_catalog_ids"] == [] and len(response["results"]) == 1
    hit = response["results"][0]
    assert query in hit["excerpt"] and len(hit["excerpt"]) <= catalogs.MAX_EXCERPT_CHARS
    assert hit["candidate_only"] is True and hit["fitment_status"] == "unverified_catalog_candidate"
    if locator is not None:
        assert hit["locator"] == locator
    return hit


def _previous(catalog, value):
    path = catalog("\f[PAGE 7]\n" + value + "\nDEMO-PART found\n")
    page, previous = catalogs._pdf_locators(path, {3}, time.monotonic() + 3)[3]
    assert page == 7
    return previous


@pytest.mark.parametrize("visible", [1, 16])
def test_title_masks_a_right_boundary_vin_with_a_whole_label(catalog, visible):
    title = "t" * (239 - visible) + " " + "A" * 17 + " tail"
    path = catalog("### Sheet\nDEMO-PART found\n", format="xlsx", title=title)

    hit = _hit(path, locator={"sheet": "Sheet", "row": 1})

    assert hit["title"].endswith(LABEL) and len(hit["title"]) <= 240
    assert "A" not in hit["title"].replace(LABEL, "")


def test_pdf_previous_masks_both_edges_of_the_original_selected_context(catalog):
    previous = "p" * 250 + " " + "A" * 17 + " " + "." * 74 + " " + "B" * 17 + " tail"
    path = catalog("\f[PAGE 7]\n" + previous + "\nDEMO-PART found\n")

    hit = _hit(path, locator={"page": 7})

    context = hit["excerpt"].split(" | ", 1)[0]
    assert context.count(LABEL) == 2 and len(context) <= 100
    assert "A" not in context.replace(LABEL, "") and "B" not in context.replace(LABEL, "")


def test_metadata_controls_keep_invalid_tokens_and_budget_complete_labels():
    a = "A" * 17
    assert catalogs._metadata_text({"title": "Demo " + a}, "title") == "Demo " + LABEL
    negatives = ["A" * 16, "A" * 8 + "Q" + "A" * 8, "Z" + a, a + "Z"]
    for value in negatives:
        assert catalogs._metadata_text({"title": value}, "title") == value
    for value in ("Z" + a, a + "Z"):
        title = "t" * 223 + " " + value + " tail"
        assert catalogs._metadata_text({"title": title}, "title") == title[:240]
    for limit in (16, 64, 500):
        title = "t" * (limit - 2) + " " + a + " tail"
        output = catalogs._metadata_text({"title": title}, "title", limit)
        assert output.endswith(LABEL) and len(output) <= limit
        assert "A" not in output.replace(LABEL, "")
    assert catalogs._metadata_text({"title": LABEL + " " + a}, "title", 16) == LABEL


def test_previous_keeps_the_ordinary_suffix_of_first360_not_the_entire_line(catalog):
    for size in (99, 100, 101, 359, 360, 361):
        value = "p" * size + " AFTER-PREFIX"
        previous = _previous(catalog, value)
        assert previous[-100:] == value[:360][-100:]
    value = ("Ж \t" * 200) + " AFTER-PREFIX"
    compact = " ".join(value.split())
    assert _previous(catalog, value)[-100:] == compact[:360][-100:]


def test_previous_preserves_real_neighbors_and_does_not_show_halo_only_vins(catalog):
    a = "A" * 17
    for value in ("Z" + a, a + "Z", "A" * 16, "A" * 8 + "Q" + "A" * 8):
        for start in (259, 343):
            raw = "p" * (start - 1) + " " + value + " " + "." * 200
            assert _previous(catalog, raw)[-100:] == raw[:360][-100:]
    outside = "p" * 242 + " " + a + "." * 100 + "B" * 17 + " tail"
    context = _previous(catalog, outside)[-100:]
    assert context == "." * 100 and LABEL not in context


def test_max_query_and_both_main_boundaries_fit_with_two_previous_vins(catalog):
    query = "PART-" * 15 + "ABCDE"
    previous = "p" * 250 + " " + "A" * 17 + " " + "." * 74 + " " + "B" * 17 + " tail"
    body = "x" * 400 + " " + "A" * 17 + " " + "." * 98 + query + "." * 49 + " " + "B" * 17 + " tail"
    path = catalog("\f[PAGE 7]\n" + previous + "\n" + body + "\n")

    hit = _hit(path, query, locator={"page": 7})

    assert hit["excerpt"].count(LABEL) == 4
    assert " | " in hit["excerpt"] and query in hit["excerpt"]


def test_new_pdf_page_resets_previous_context_and_keeps_ocr_locator(catalog):
    path = catalog("\f[PAGE 7]\n" + "A" * 17 + "\n\f[PAGE 8]\nDEMO-PART found\n", ocr_pages=[8])

    hit = _hit(path, locator={"page": 8})

    assert hit["excerpt"] == "DEMO-PART found"
    assert hit["extraction_method"] == "ocr" and hit["ocr_unverified"] is True


def test_additional_redaction_scans_only_bounded_windows_and_evidence(catalog, monkeypatch):
    calls = []
    real = catalogs._redact_match_window

    def observed(window, left, right):
        calls.append((len(window), len(left), len(right)))
        return real(window, left, right)

    monkeypatch.setattr(catalogs, "_redact_match_window", observed)
    assert catalogs._metadata_text({"title": "t" * 21000}, "title") == "t" * 240
    assert calls == [(240, 0, 17)]
    calls.clear()
    _previous(catalog, "p" * 21000)
    assert (100, 17, 17) in calls
    assert all(window <= 100 and left <= 17 and right <= 17 for window, left, right in calls)
