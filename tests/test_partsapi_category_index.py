from __future__ import annotations

from autostop_manager.partsapi_category_index import (
    explain_partsapi_category_for_intent,
    load_partsapi_category_index,
    search_partsapi_category_index,
    validate_partsapi_category_index,
)


def test_category_index_maps_front_brake_pads_to_numeric_cat():
    result = explain_partsapi_category_for_intent("front_brake_pads", query="передние колодки")

    assert result["category_unresolved"] is False
    assert result["selected_category"]["cat_id"].isdigit()
    assert result["selected_category"]["matched_by"]


def test_category_index_search_and_validate_are_safe():
    search = search_partsapi_category_index("стойка стабилизатора", limit=3)
    validation = validate_partsapi_category_index()

    assert search["ok"] is True
    assert search["matches"]
    assert validation["ok"] is True
    assert validation["privacy"]["secret_exposed"] is False


def test_category_index_loader_handles_invalid_payload(tmp_path):
    index_path = tmp_path / "partsapi_category_index.json"
    index_path.write_text("[]", encoding="utf-8")

    loaded = load_partsapi_category_index(index_path)
    search = search_partsapi_category_index("anything", path=index_path)
    validation = validate_partsapi_category_index(path=index_path)

    assert loaded["missing"] is True
    assert loaded["error"] == "invalid_structure"
    assert loaded["error_detail"] == "list"
    assert search["missing"] is True
    assert search["matches"] == []
    assert validation["ok"] is False
    assert validation["category_count"] == 0


def test_category_index_loader_handles_unreadable_payload(tmp_path, monkeypatch):
    index_path = tmp_path / "partsapi_category_index.json"
    index_path.write_text("{}", encoding="utf-8")

    def fake_read_text(self, encoding="utf-8-sig"):
        raise OSError("permission denied")

    monkeypatch.setattr(type(index_path), "read_text", fake_read_text)

    loaded = load_partsapi_category_index(index_path)

    assert loaded["missing"] is True
    assert loaded["error"] == "unreadable"
    assert loaded["error_detail"] == "permission denied"
