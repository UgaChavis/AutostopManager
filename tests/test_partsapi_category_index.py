from __future__ import annotations

from autostop_manager.partsapi_category_index import (
    explain_partsapi_category_for_intent,
    load_partsapi_category_index,
    search_partsapi_category_index,
    validate_partsapi_category_index,
)


def test_legacy_category_index_does_not_select_cat_for_current_contract():
    result = explain_partsapi_category_for_intent("front_brake_pads", query="передние колодки")

    assert result["active_for_current_contract"] is False
    assert result["category_unresolved"] is True
    assert result["selected_category"] is None
    assert result["historical_match"]["cat_id"].isdigit()
    assert result["historical_match"]["matched_by"]


def test_category_index_rejects_generic_token_match_for_another_intent():
    result = explain_partsapi_category_for_intent("brake_disc", query="тормозные диски")

    assert result["selected_category"] is None
    assert result["category_unresolved"] is True


def test_category_index_search_and_validate_are_safe():
    search = search_partsapi_category_index("стойка стабилизатора", limit=3)
    validation = validate_partsapi_category_index()

    assert search["ok"] is True
    assert search["matches"]
    assert search["active_for_current_contract"] is False
    assert validation["ok"] is True
    assert validation["active_for_current_contract"] is False
    assert validation["privacy"]["secret_exposed"] is False


def test_category_index_search_rejects_negative_limit():
    search = search_partsapi_category_index("стойка стабилизатора", limit=-1)

    assert search["count"] == 0
    assert search["matches"] == []


def test_custom_historical_index_cannot_reactivate_removed_method(tmp_path):
    index_path = tmp_path / "historical_category_index.json"
    index_path.write_text(
        '{"schema":"PartsApiCategoryIndexV1","categories":['
        '{"cat_id":"1191","names_ru":["колодки"],"intent_ids":["front_brake_pads"]}]}',
        encoding="utf-8",
    )

    result = explain_partsapi_category_for_intent("front_brake_pads", query="колодки", path=index_path)

    assert result["active_for_current_contract"] is False
    assert result["historical_match"]["cat_id"] == "1191"
    assert result["selected_category"] is None
    assert result["category_unresolved"] is True


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


def test_category_index_loader_rejects_non_list_categories(tmp_path):
    index_path = tmp_path / "partsapi_category_index.json"
    index_path.write_text('{"schema":"PartsApiCategoryIndexV1","categories":"bad"}', encoding="utf-8")

    loaded = load_partsapi_category_index(index_path)
    validation = validate_partsapi_category_index(path=index_path)

    assert loaded["missing"] is True
    assert loaded["error"] == "invalid_categories"
    assert loaded["error_detail"] == "str"
    assert validation["ok"] is False
    assert validation["category_count"] == 0


def test_category_index_loader_handles_unreadable_payload(tmp_path, monkeypatch):
    index_path = tmp_path / "partsapi_category_index.json"
    index_path.write_text("{}", encoding="utf-8")

    def fake_read_text(self, *args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(type(index_path), "read_text", fake_read_text)

    loaded = load_partsapi_category_index(index_path)

    assert loaded["missing"] is True
    assert loaded["error"] == "unreadable"
    assert loaded["error_detail"] == "permission denied"
