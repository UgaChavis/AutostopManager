"""Public-search regressions for bounded VIN evidence at the left capture edge."""

from __future__ import annotations

import json
import socket
import sqlite3

import pytest

from autostop_manager import offline_catalogs as catalogs


@pytest.fixture
def search_catalog(tmp_path, monkeypatch):
    root = tmp_path / "catalogs"
    (root / "text").mkdir(parents=True)
    monkeypatch.setenv(catalogs.CATALOG_ROOT_ENV, str(root))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Synthetic catalog tests cannot access network or runtime storage")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(catalogs, "get_db_path", forbidden)
    run = catalogs.subprocess.run

    def bounded_rg(command, **kwargs):
        assert command[0] == "rg" and command[-2] == "--"
        assert command[command.index("--max-columns") + 1] == "20000"
        assert command[command.index("--max-count") + 1] == "8"
        assert 0 < kwargs["timeout"] <= catalogs.MAX_SEARCH_SECONDS
        assert not kwargs.get("shell", False)
        result = run(command, **{**kwargs, "timeout": min(kwargs["timeout"], 3)})
        assert result.returncode == 0 and len(result.stdout.encode()) < 2048
        return result

    monkeypatch.setattr(catalogs.subprocess, "run", bounded_rg)

    def search(body, *, kind="pdf", query="DEMO-PART", sheet="Sheet"):
        suffix = ".tsv" if kind == "tsv" else ".txt"
        if kind == "tsv":
            prefix, format, locator = sheet + "\t12\t", "xlsx", {"sheet": sheet, "row": 12}
        elif kind == "txt":
            prefix, format, locator = "### Sheet\n", "xlsx", {"sheet": "Sheet", "row": 1}
        else:
            prefix, format, locator = "\f[PAGE 7]\n", "pdf", {"page": 7}
        path = root / "text" / ("demo" + suffix)
        path.write_text(prefix + body + "\n", encoding="utf-8")
        entry = {
            "catalog_id": "demo",
            "format": format,
            "text_path": "text/" + path.name,
            "text_extract_status": "extracted",
        }
        (root / "catalog_index.json").write_text(json.dumps({"entries": [entry]}), encoding="utf-8")

        result = catalogs.search_offline_parts_catalogs(query)

        assert result["ok"] is True and result["warnings"] == []
        assert result["search_incomplete"] is False and result["result_limit_reached"] is False
        assert result["scanned_catalogs"] == 1 and result["scanned_bytes"] == path.stat().st_size
        assert result["skipped_catalog_ids"] == [] and len(result["results"]) == 1
        hit = result["results"][0]
        assert hit["locator"] == locator and query in hit["excerpt"]
        assert hit["candidate_only"] is True and hit["fitment_status"] == "unverified_catalog_candidate"
        assert len(hit["excerpt"]) <= catalogs.MAX_EXCERPT_CHARS
        return hit["excerpt"]

    return search


@pytest.mark.parametrize("kind", ["txt", "tsv", "pdf"])
@pytest.mark.parametrize("visible", [1, 9, 16])
def test_left_split_vin_is_masked_before_window_is_returned(search_catalog, kind, visible):
    body = "x" * 400 + " " + "A" * 17 + " " + "." * (99 - visible) + "DEMO-PART tail"

    excerpt = search_catalog(body, kind=kind)

    assert excerpt.startswith("[VIN REDACTED] ")
    assert "A" not in excerpt.replace("[VIN REDACTED]", "").replace("DEMO-PART", "")


@pytest.mark.parametrize("padding", [0, 50, 90])
def test_leading_pdf_vin_keeps_its_first_character_as_redaction_evidence(search_catalog, padding):
    excerpt = search_catalog("A" * 17 + " " + "." * padding + "DEMO-PART tail")

    assert excerpt.startswith("[VIN REDACTED] ")
    assert "A" not in excerpt.replace("[VIN REDACTED]", "").replace("DEMO-PART", "")


@pytest.mark.parametrize("kind,padding", [("txt", 152), ("tsv", 152), ("pdf", 49)])
def test_both_boundary_labels_fit_without_clipping_the_max_length_query(search_catalog, kind, padding):
    query = "PART-" * 15 + "ABCDE"
    assert len(query) == catalogs.MAX_QUERY_CHARS
    body = "x" * 400 + " " + "A" * 17 + " " + "." * 98 + query + "." * padding + " " + "B" * 17 + " tail"

    excerpt = search_catalog(body, kind=kind, query=query)

    assert excerpt.count("[VIN REDACTED]") == 2
    assert excerpt.startswith("[VIN REDACTED] ") and excerpt.endswith("[VIN REDACTED]")


@pytest.mark.parametrize("kind", ["txt", "tsv", "pdf"])
def test_left_halo_retains_alphanumeric_neighbor_that_disqualifies_vin(search_catalog, kind):
    body = "x" * 400 + " Z" + "A" * 17 + " " + "." * 90 + "DEMO-PART tail"

    excerpt = search_catalog(body, kind=kind)

    assert excerpt.startswith("A" * 9 + " ")
    assert "[VIN REDACTED]" not in excerpt


def test_distant_first_pdf_character_is_not_joined_to_a_sixteen_character_token(search_catalog):
    body = "A" + "x" * 400 + " " + "A" * 16 + " " + "." * 83 + "DEMO-PART tail"

    excerpt = search_catalog(body)

    assert excerpt.startswith("A" * 16 + " ")
    assert "[VIN REDACTED]" not in excerpt


def test_left_halo_only_vin_does_not_add_a_label_or_hidden_text(search_catalog):
    excerpt = search_catalog("x" * 400 + " " + "A" * 17 + "." * 100 + "DEMO-PART tail")

    assert excerpt == "." * 100 + "DEMO-PART tail"


def test_left_redaction_keeps_non_ascii_literal_query(search_catalog):
    query = "ФИЛЬТР.42+[X]"
    body = "x" * 400 + " " + "A" * 17 + " " + "." * 90 + query + " tail"

    excerpt = search_catalog(body, query=query)

    assert excerpt.startswith("[VIN REDACTED] ")
    assert query in excerpt


def test_wire_separator_sequences_in_tsv_locator_and_content_are_not_private_fields(search_catalog):
    left_separator = "\x1d" + "\x1e" * 17
    right_separator = "\x1f" + "\x1e" * 17
    sheet = "Sheet" + left_separator + "Suffix" + right_separator
    body = (
        "x" * 400
        + " "
        + "A" * 17
        + " "
        + "." * 90
        + "DEMO-PART left"
        + left_separator
        + "right"
        + right_separator
        + "tail"
    )

    excerpt = search_catalog(body, kind="tsv", sheet=sheet)

    assert excerpt.startswith("[VIN REDACTED] ")
    assert "DEMO-PART left right tail" in excerpt


@pytest.mark.parametrize("prefix", ["", "-"])
def test_marked_pdf_keeps_the_first_query_occurrence(search_catalog, prefix):
    excerpt = search_catalog(prefix + "DEMO-PART early " + "x" * 400 + " DEMO-PART late")

    assert "early" in excerpt and "late" not in excerpt


def test_full_vin_within_window_still_uses_the_existing_label(search_catalog):
    excerpt = search_catalog("x" * 400 + " " + "A" * 17 + " " + "." * 50 + "DEMO-PART tail", kind="txt")

    assert "[VIN REDACTED]" in excerpt and "A" * 9 not in excerpt
