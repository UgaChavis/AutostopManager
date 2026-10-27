"""Supplied overlap decisions survive the ready labor handoff to calculation."""

from copy import deepcopy

import pytest

from autostop_manager import automotive_labor as labor


POLICY = {"basis": "hourly_rate", "version": "synthetic-overlap-v1"}


def row(operation_id="operation", **values):
    return {
        "operation_id": operation_id,
        "operation_name": "DEMO operation",
        "hours": 2,
        "source": {"provider": "synthetic", "locator": "synthetic:overlap-proof"},
        **values,
    }


def calculate(rows):
    response = labor.calculate_work_price(rows, POLICY, hourly_rate=1000)
    assert response["execution"] == {"network_calls": 0, "attempts": []}
    return response


def handoff(rows):
    before = deepcopy((rows, POLICY))
    original = calculate(rows)
    normalized = labor.normalize_labor_time(rows)
    assert normalized["outcome"] == "success"
    assert normalized["execution"] == {"network_calls": 0, "attempts": []}
    normalized_before = deepcopy(normalized)
    actual = calculate(normalized["data"]["labor"])
    assert (rows, POLICY) == before
    assert normalized == normalized_before
    return original, normalized, actual


@pytest.mark.parametrize("resolved", [True, False])
def test_supplied_resolution_preserves_direct_calculation(resolved):
    original, normalized, actual = handoff([row(overlaps_with=["package"], overlap_resolved=resolved)])
    if resolved:
        assert original["outcome"] == "success" and original["data"]["total"] == 2000
    else:
        assert original["outcome"] == "partial"
        assert original["data"]["exclusions"] == [{"operation_id": "operation", "reason": "unresolved_overlap"}]
    assert actual == original
    assert normalized["data"]["labor"][0]["overlap_resolved"] is resolved


def test_absent_resolution_stays_absent_and_unresolved():
    original, normalized, actual = handoff([row(overlaps_with=["package"])])
    assert "overlap_resolved" not in normalized["data"]["labor"][0]
    assert actual == original
    assert actual["outcome"] == "partial" and actual["data"]["total"] is None
    assert actual["missing_fields"] == ["operation.overlap_adjustment"]
    assert actual["data"]["exclusions"] == [{"operation_id": "operation", "reason": "unresolved_overlap"}]


@pytest.mark.parametrize("fields", [{}, {"overlap_resolved": False}, {"overlap_resolved": True}])
def test_resolution_does_not_create_an_overlap(fields):
    original, normalized, actual = handoff([row(**fields)])
    assert actual == original
    assert actual["outcome"] == "success" and actual["data"]["total"] == 2000
    assert actual["data"]["exclusions"] == []
    assert normalized["data"]["overlaps"] == []
    ready = normalized["data"]["labor"][0]
    assert ("overlap_resolved" in ready) == ("overlap_resolved" in fields)
    if fields:
        assert ready["overlap_resolved"] is fields["overlap_resolved"]


def test_raw_time_provenance_and_overlap_summary_remain_unchanged():
    supplied = row(
        0,
        workName="DEMO operation",
        workTime="120",
        unit="minutes",
        catalog_ref="synthetic:operation",
        scope="synthetic scope",
        conditions={"nested": [None, 0, "synthetic"]},
        included_operations=["included"],
        overlaps_with=["package"],
        overlap_resolved=True,
    )
    before = deepcopy(supplied)
    original, normalized, actual = handoff([supplied])
    ready = normalized["data"]["labor"][0]
    assert ready["operation_id"] == "0" and ready["operation_name"] == supplied["workName"]
    assert ready["raw_time"] == "120" and ready["raw_unit"] == "minutes" and ready["hours"] == 2
    for field in ("source", "catalog_ref", "scope", "conditions", "included_operations", "overlaps_with"):
        assert ready[field] == supplied[field]
    assert normalized["data"]["overlaps"] == [
        {"operation_id": "0", "included_operations": ["included"], "overlaps_with": ["package"]}
    ]
    assert supplied == before
    assert labor.normalize_labor_time([supplied]) == normalized
    assert ready["overlap_resolved"] is True
    assert actual == original and actual["data"]["total"] == 2000


def test_mixed_rows_keep_inclusion_duplicates_and_unresolved_exclusions():
    rows = [
        row("package", included_operations=["included"]),
        row("included", hours=1),
        row("package", hours=99),
        row("resolved", hours=1, overlaps_with=["package"], overlap_resolved=True),
        row("unresolved", hours=1, overlaps_with=["package"], overlap_resolved=False),
    ]
    original, _normalized, actual = handoff(rows)
    assert original["data"]["known_subtotal"] == 3000
    assert actual == original
    assert actual["outcome"] == "partial" and actual["data"]["total"] is None
    assert actual["missing_fields"] == ["unresolved.overlap_adjustment"]
    assert [item["operation_id"] for item in actual["data"]["operations"]] == ["package", "resolved"]
    assert actual["data"]["exclusions"] == [
        {"operation_id": "included", "reason": "duplicate_or_included_operation"},
        {"operation_id": "package", "reason": "duplicate_or_included_operation"},
        {"operation_id": "unresolved", "reason": "unresolved_overlap"},
    ]


@pytest.mark.parametrize("value", [None, 0, "true"])
def test_existing_boolean_resolution_validation_is_preserved(value):
    supplied = row(overlaps_with=["package"], overlap_resolved=value)
    before = deepcopy(supplied)
    normalized = labor.normalize_labor_time([supplied])
    calculated = calculate([supplied])
    assert normalized["outcome"] == calculated["outcome"] == "invalid_input"
    assert normalized["missing_fields"] == ["rows.structure"]
    assert calculated["missing_fields"] == ["labor.structure"]
    assert supplied == before
