"""Private synthetic regressions for bounded summary preview materialization."""

from __future__ import annotations

from copy import deepcopy

import pytest

from autostop_manager import catalog_presentation as presentation

SYNTHETIC_VIN = "A" * 17


@pytest.fixture
def sparse_directory():
    rows = [
        None,
        "not a row",
        7,
        {},
        {"VIN": SYNTHETIC_VIN, "key": "synthetic-secret-only"},
        {"MAKE_NAME": ["non-scalar name"], "title": None},
    ]
    eligible = []
    for index in range(31):
        row = {
            "MAKE_ID": index,
            "MAKE_NAME": f"MAKE {index}: {SYNTHETIC_VIN}",
            "title": f"ITEM {index}",
            "VIN": SYNTHETIC_VIN,
            "key": f"synthetic-secret-{index}",
        }
        eligible.append(row)
        rows.append(row)
        if index % 7 == 0:
            rows.append({"credential": "synthetic-non-whitelisted"})
        if index == 24:
            rows.extend([False, [], {}, {"name": None}])
    # An allowed scalar-only row counts even without any strings to redact.
    scalar_only = {"id": 999, "unlisted": "synthetic-omitted"}
    rows.append(scalar_only)
    eligible.append(scalar_only)
    payload = {
        "ok": True,
        "outcome": "success",
        "payload": {"data": rows},
        "vehicle_profiles": [],
    }
    return payload, rows, eligible


def test_sparse_summary_preserves_full_counts_order_redaction_and_input(sparse_directory):
    payload, rows, eligible = sparse_directory
    before = deepcopy(payload)

    result = presentation.present_catalog(payload, "summary")

    preview = result["data_preview"]
    assert preview["source_total"] == sum(isinstance(row, dict) for row in rows)
    assert preview["previewable_total"] == len(eligible) == 32
    assert len(preview["records"]) == 25
    assert [row["MAKE_ID"] for row in preview["records"]] == list(range(25))
    assert preview["records"] == [
        {
            "MAKE_ID": index,
            "MAKE_NAME": f"MAKE {index}: [REDACTED_IDENTIFIER]",
            "title": f"ITEM {index}",
        }
        for index in range(25)
    ]
    assert result["presentation"]["lists"]["data_preview.records"] == {
        "total": 32,
        "returned": 25,
        "truncated": True,
    }
    assert preview["fitment_confirmed"] is False
    assert "payload" not in result
    assert SYNTHETIC_VIN not in str(preview)
    assert "synthetic-secret" not in str(result)
    assert payload == before
    assert payload["payload"]["data"] is rows


def test_summary_redacts_only_string_fields_that_are_returned(sparse_directory, monkeypatch):
    payload, _rows, eligible = sparse_directory
    original = presentation._VIN
    calls = []

    class RedactionSpy:
        def sub(self, replacement, value):
            calls.append(value)
            return original.sub(replacement, value)

    monkeypatch.setattr(presentation, "_VIN", RedactionSpy())
    result = presentation.present_catalog(payload, "summary")

    assert len(result["data_preview"]["records"]) == 25
    expected_strings = [row[field] for row in eligible[:25] for field in ("MAKE_NAME", "title")]
    # Accurate totals still need a scan of later rows; those rows do not need
    # preview dict construction or VIN redaction when they cannot be returned.
    assert len(calls) == len(expected_strings) == 50
    assert calls == expected_strings


def test_preview_keeps_first_nonempty_wrapper_and_existing_depth_boundary():
    selected = {"MAKE_NAME": "SELECTED"}
    payload = {"payload": {"data": [None, 9], "result": {"items": [selected]}, "array": [{"MAKE_NAME": "IGNORED"}]}}
    assert presentation.present_catalog(payload, "summary")["data_preview"]["records"] == [selected]

    # A nonempty dictionary row list selects its wrapper even when none of its
    # fields are previewable; a later wrapper is not a replacement source.
    payload = {"payload": {"data": [{"unlisted": "synthetic-only"}], "result": [selected]}}
    assert "data_preview" not in presentation.present_catalog(payload, "summary")

    for depth, should_preview in ((8, True), (9, False)):
        raw = [selected]
        for _ in range(depth):
            raw = {"data": raw}
        result = presentation.present_catalog({"payload": raw}, "summary")
        assert ("data_preview" in result) is should_preview


def test_full_identity_and_normalized_suppression_skip_raw_preview(sparse_directory, monkeypatch):
    payload, _rows, _eligible = sparse_directory

    class ForbiddenRedaction:
        def sub(self, *_args):
            pytest.fail("raw preview redaction must not run for full or normalized results")

    monkeypatch.setattr(presentation, "_VIN", ForbiddenRedaction())
    assert presentation.present_catalog(payload, "full") is payload
    payload["vehicle_profiles"] = [{"make": "SYNTHETIC"}]
    before = deepcopy(payload)
    result = presentation.present_catalog(payload, "summary")
    assert "data_preview" not in result
    assert result["vehicle_profiles"] == [{"make": "SYNTHETIC"}]
    assert payload == before


@pytest.mark.parametrize("branch", ["normalized_suppression", "selected_wrapper_without_preview_fields"])
def test_existing_preview_survives_when_no_generated_preview_is_created(branch):
    records = [{"id": index, "name": f"EXISTING {index}"} for index in range(37)]
    existing_preview = {
        "records": records,
        "source_total": 501,
        "previewable_total": 37,
        "fitment_confirmed": False,
        "origin": "existing-preview",
    }
    payload = {"ok": True, "data_preview": existing_preview}
    if branch == "normalized_suppression":
        payload["vehicle_profiles"] = [{"make": "SYNTHETIC"}]
        payload["payload"] = {"data": [{"MAKE_NAME": f"UNUSED RAW {index}"} for index in range(45)]}
    else:
        # This nonempty dictionary list selects data, but has no eligible
        # preview fields. The later wrapper must not generate a replacement.
        payload["payload"] = {
            "data": [{"VIN": SYNTHETIC_VIN, "key": "synthetic-omitted"}],
            "result": [{"MAKE_NAME": "DO NOT FALL BACK"}],
        }
    before = deepcopy(payload)
    original_record_objects = tuple(records)

    result = presentation.present_catalog(payload, "summary")

    assert result["data_preview"] == {**existing_preview, "records": records[:25]}
    assert result["data_preview"]["records"] == [{"id": index, "name": f"EXISTING {index}"} for index in range(25)]
    assert result["data_preview"]["source_total"] == 501
    assert result["data_preview"]["previewable_total"] == 37
    assert result["presentation"]["lists"]["data_preview.records"] == {
        "total": 37,
        "returned": 25,
        "truncated": True,
    }
    assert "payload" not in result
    assert payload == before
    assert payload["data_preview"] is existing_preview
    assert payload["data_preview"]["records"] is records
    assert len(records) == 37
    assert all(row is original for row, original in zip(records, original_record_objects, strict=True))
    assert presentation.present_catalog(payload, "full") is payload
