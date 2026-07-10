from __future__ import annotations

import os

import autostop_manager.partsapi_category_index as category_index_module
from autostop_manager.partsapi_category_index import (
    build_partsapi_category_index_plan,
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

    loaded = load_partsapi_category_index(index_path, allowed_root=tmp_path)
    search = search_partsapi_category_index("anything", path=index_path, allowed_root=tmp_path)
    validation = validate_partsapi_category_index(path=index_path, allowed_root=tmp_path)

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

    loaded = load_partsapi_category_index(index_path, allowed_root=tmp_path)
    validation = validate_partsapi_category_index(path=index_path, allowed_root=tmp_path)

    assert loaded["missing"] is True
    assert loaded["error"] == "invalid_categories"
    assert loaded["error_detail"] == "str"
    assert validation["ok"] is False
    assert validation["category_count"] == 0


def test_category_index_loader_handles_unreadable_payload(tmp_path, monkeypatch):
    index_path = tmp_path / "partsapi_category_index.json"
    index_path.write_text("{}", encoding="utf-8")

    def fake_read_bytes(_path):
        raise OSError("permission denied")

    monkeypatch.setattr(category_index_module, "_read_index_bytes", fake_read_bytes)

    loaded = load_partsapi_category_index(index_path, allowed_root=tmp_path)

    assert loaded["missing"] is True
    assert loaded["error"] == "unreadable"
    assert loaded["error_detail"] == "permission denied"


def test_category_index_rejects_path_outside_canonical_root(tmp_path):
    index_path = tmp_path / "partsapi_category_index.json"
    index_path.write_text('{"categories": []}', encoding="utf-8")

    loaded = load_partsapi_category_index(index_path)

    assert loaded["missing"] is True
    assert loaded["error"] == "outside_allowed_root"


def test_category_index_rejects_symlink_and_non_json_file(tmp_path):
    target = tmp_path / "target.json"
    target.write_text('{"categories": []}', encoding="utf-8")
    link = tmp_path / "index.json"
    link.symlink_to(target)
    text_file = tmp_path / "index.txt"
    text_file.write_text('{"categories": []}', encoding="utf-8")

    linked = load_partsapi_category_index(link, allowed_root=tmp_path)
    wrong_extension = load_partsapi_category_index(text_file, allowed_root=tmp_path)

    assert linked["error"] == "symlink_not_allowed"
    assert wrong_extension["error"] == "json_extension_required"


def test_category_index_rejects_special_and_oversized_files(tmp_path):
    fifo = tmp_path / "index.json"
    os.mkfifo(fifo)
    special = load_partsapi_category_index(fifo, allowed_root=tmp_path)
    fifo.unlink()
    oversized_path = tmp_path / "oversized.json"
    with oversized_path.open("wb") as stream:
        stream.truncate(2 * 1024 * 1024 + 1)
    oversized = load_partsapi_category_index(oversized_path, allowed_root=tmp_path)

    assert special["error"] == "not_regular_file"
    assert oversized["error"] == "file_too_large"


def test_category_index_rejects_unbounded_or_nested_category_structure(tmp_path):
    too_many_path = tmp_path / "too_many.json"
    too_many_path.write_text(
        '{"categories":[' + ",".join("{}" for _ in range(5_001)) + "]}",
        encoding="utf-8",
    )
    nested_path = tmp_path / "nested.json"
    nested_path.write_text('{"categories":[{"cat_id":{"nested":"value"}}]}', encoding="utf-8")

    too_many = load_partsapi_category_index(too_many_path, allowed_root=tmp_path)
    nested = load_partsapi_category_index(nested_path, allowed_root=tmp_path)

    assert too_many["error"] == "too_many_categories"
    assert nested["error"] == "invalid_category_value"


def test_category_index_build_plan_clamps_forwarded_network_budget():
    plan = build_partsapi_category_index_plan(
        live=True,
        type_id="1404",
        timeout=12_345,
        max_attempts=99_999,
    )

    assert plan["request"]["timeout"] == 20.0
    assert plan["request"]["max_attempts"] == 3
    assert plan["request"]["timeout"] * plan["request"]["max_attempts"] == 60.0
