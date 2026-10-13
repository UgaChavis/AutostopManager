from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from autostop_manager import offline_catalogs as catalogs
from autostop_manager.part_market_assessment import assess_part_market


def _offer(host: str, *, region: str = "Красноярск, Россия", price: int = 5000) -> dict:
    return {
        "article": "1712024",
        "brand": "Ford",
        "price_rub": price,
        "source": "Synthetic retail catalog",
        "source_excerpt": f"Ford 1712024: новый, цена {price} ₽",
        "url": f"https://{host}/offer/1712024",
        "observed_at": datetime.now(UTC).date().isoformat(),
        "published_at": datetime.now(UTC).date().isoformat(),
        "offer_kind": "original",
        "condition": "new",
        "region": region,
    }


@pytest.mark.parametrize("host", ["parts.local", "parts.internal", "parts.onion"])
def test_private_source_hostname_cannot_be_public_price_evidence(host):
    result = assess_part_market(article="1712024", brand="Ford", observations=[_offer(host)])
    assert result["accepted_offer_count"] == 0
    assert result["rejected_observations"] == [{"observation_index": 0, "code": "observation_fields_invalid"}]


@pytest.mark.parametrize("region,scope", [("Красноярск, Россия", "krasnoyarsk"), ("Норильск, Россия", "rf")])
def test_distinct_public_prices_keep_existing_region_and_median_contract(region, scope):
    rows = [_offer(f"market-{i}.example.org", region=region, price=5000 + i * 1000) for i in range(3)]
    result = assess_part_market(article="1712024", brand="Ford", observations=rows)
    populated = [row for row in result["segments"] if row["independent_offer_count"]]
    assert len(populated) == 1
    assert populated[0]["region_scope"] == scope
    assert populated[0]["median_price_rub"] == 6000
    assert populated[0]["median_confidence"] == "medium"


def _entry(catalog_id="demo", **updates):
    entry = {
        "catalog_id": catalog_id,
        "format": "pdf",
        "text_path": f"text/{catalog_id}.txt",
        "text_extract_status": "ok",
    }
    entry.update(updates)
    return entry


def _install(tmp_path, monkeypatch, entries):
    root = tmp_path / "catalogs"
    (root / "text").mkdir(parents=True)
    (root / "catalog_index.json").write_text(json.dumps({"entries": entries}))
    monkeypatch.setenv(catalogs.CATALOG_ROOT_ENV, str(root))
    return root


@pytest.mark.parametrize("style", ["synthetic_markers", "legacy_formfeeds"])
def test_literal_pdf_marker_is_document_text_not_a_page_or_ocr_locator(tmp_path, monkeypatch, style):
    root = _install(tmp_path, monkeypatch, [_entry(ocr_pages=[2])])
    start = "\f[PAGE 1]\n" if style == "synthetic_markers" else "Public technical catalog\n"
    following_page = "\f[PAGE 2]\n" if style == "synthetic_markers" else "\f\n"
    (root / "text/demo.txt").write_text(
        start + "Literal [PAGE 2] reference in the document\nFord part 12345\n" + following_page + "Other page text\n"
    )
    result = catalogs.search_offline_parts_catalogs("12345")
    assert result["ok"] is True
    assert result["results"][0]["locator"] == {"page": 1}
    assert result["results"][0]["extraction_method"] == "text"
    assert result["results"][0]["ocr_unverified"] is False


def test_catalog_text_symlink_loop_is_skipped_and_good_catalog_remains_searchable(tmp_path, monkeypatch):
    root = _install(tmp_path, monkeypatch, [_entry(), _entry("good")])
    (root / "text/demo.txt").symlink_to("demo.txt")
    (root / "text/good.txt").write_text("\f[PAGE 1]\nFord part 12345\n")
    result = catalogs.search_offline_parts_catalogs("12345")
    assert result["ok"] is True
    assert result["skipped_catalog_ids"] == ["demo"]
    assert result["search_incomplete"] is True
    assert [row["catalog_id"] for row in result["results"]] == ["good"]


def test_catalog_root_symlink_loop_returns_invalid_root_instead_of_exception(tmp_path, monkeypatch):
    root = tmp_path / "catalogs"
    root.symlink_to("catalogs")
    monkeypatch.setenv(catalogs.CATALOG_ROOT_ENV, str(root))
    result = catalogs.search_offline_parts_catalogs("12345")
    assert result == {"ok": False, "error": "catalog_root_invalid", "results": []}
