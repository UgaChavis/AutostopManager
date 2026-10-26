from __future__ import annotations

from copy import deepcopy

import pytest

from autostop_manager import automotive_parts as parts


def _edge(name, origin, target, *, direction="from_to", origin_brand="DEMO", target_brand="DEMO", kind="supersession"):
    row = {
        "row": name,
        "from": {"number": origin, "brand": origin_brand, "tag": name + ".from"},
        "to": {"number": target, "brand": target_brand, "tag": name + ".to"},
        "type": kind,
        "direction": direction,
        "qualifiers": ["fixture:" + name],
    }
    row["evidence"] = [
        {
            "document_kind": "manufacturer_supersession",
            "provider": "SYNTHETIC manufacturer fixture",
            "primary_lineage": "synthetic.publication.v1",
            "method": "document_read",
            "locator": "https://docs.example.invalid/supersession",
            "fetched_at": "2026-01-01T00:00:00Z",
            "from": deepcopy(row["from"]),
            "to": deepcopy(row["to"]),
            "direction": direction,
        }
    ]
    return row


def _expected_relation(row, relation_type=None):
    # Fixture numbers are already uppercase alphanumeric canonical values.
    return {
        **row,
        "type": row["type"] if relation_type is None else relation_type,
        "from": {**row["from"], "normalized_number": row["from"]["number"]},
        "to": {**row["to"], "normalized_number": row["to"]["number"]},
        "fitment_confirmed": False,
    }


def _branch(origin, *alternatives):
    return {"code": "supersession_branches", "from": origin, "alternatives": list(alternatives)}


def test_interleaved_origins_preserve_complete_conflict_order_and_duplicate_alternatives():
    rows = [
        _edge("r0", "A1", "B1"),
        _edge("r1", "X1", "Y1"),
        _edge("r2", "A1", "C1"),
        _edge("r3", "A1", "B1"),
        _edge("r4", "X1", "Z1"),
        _edge("r5", "T1", "U1"),
    ]
    before = deepcopy(rows)
    expected = [_expected_relation(row) for row in before]

    response = parts.compare_part_relations(rows)

    assert response["ok"] is True
    assert response["outcome"] == "partial"
    assert response["missing_fields"] == []
    assert response["data"] == {"relations": expected}
    assert response["conflicts"] == [
        _branch(expected[0]["from"], expected[0]["to"], expected[2]["to"]),
        _branch(expected[1]["from"], expected[1]["to"], expected[4]["to"]),
        _branch(expected[2]["from"], expected[2]["to"], expected[0]["to"], expected[3]["to"]),
        _branch(expected[3]["from"], expected[3]["to"], expected[2]["to"]),
        _branch(expected[4]["from"], expected[4]["to"], expected[1]["to"]),
    ]
    assert response["execution"]["network_calls"] == 0
    assert rows == before


def test_effective_reverse_direction_and_brand_identity_preserve_exact_conflicts():
    rows = [
        _edge("r0", "A1", "B1"),
        _edge("r1", "C1", "A1", direction="to_from", origin_brand="DeMo", target_brand="demo"),
        _edge("r2", "A1", "D1", origin_brand="OTHER", target_brand="OTHER"),
        _edge("r3", "A1", "B1", target_brand="OTHER"),
    ]
    before = deepcopy(rows)
    expected = [_expected_relation(row) for row in before]

    response = parts.compare_part_relations(rows)

    assert response["ok"] is True
    assert response["outcome"] == "partial"
    assert response["missing_fields"] == []
    assert response["data"] == {"relations": expected}
    assert response["conflicts"] == [
        _branch(expected[0]["from"], expected[0]["to"], expected[1]["from"], expected[3]["to"]),
        _branch(expected[1]["to"], expected[1]["from"], expected[0]["to"], expected[3]["to"]),
        _branch(expected[3]["from"], expected[3]["to"], expected[0]["to"], expected[1]["from"]),
    ]
    assert response["execution"]["network_calls"] == 0
    assert rows == before


def test_invalid_evidence_in_branch_batch_preserves_atomic_validation():
    rows = [_edge("r0", "A1", "B1"), _edge("r1", "A1", "C1"), _edge("bad", "P1", "Q1")]
    rows[2]["evidence"] = {"invalid": "shape"}
    before = deepcopy(rows)

    response = parts.compare_part_relations(rows)

    assert response["ok"] is False
    assert response["outcome"] == "invalid_input"
    assert response["data"] == {}
    assert response["missing_fields"] == ["relation_evidence"]
    assert response["conflicts"] == []
    assert response["execution"]["network_calls"] == 0
    assert rows == before


def test_only_normalized_verified_supersessions_participate_in_branch_detection():
    rows = [
        _edge("verified", "A1", "B1"),
        _edge("unverified", "A1", "C1"),
        _edge("cross", "A1", "D1", kind="cross"),
        _edge("analog", "A1", "E1", kind="analog"),
        _edge("oe", "A1", "F1", kind="oe_reference"),
        _edge("unknown", "A1", "G1", kind="not_a_relation"),
    ]
    rows[1]["evidence"] = []
    before = deepcopy(rows)
    types = ["supersession", "unverified_supersession", "cross", "analog", "oe_reference", "unknown"]

    response = parts.compare_part_relations(rows)

    assert response["ok"] is True
    assert response["outcome"] == "partial"
    assert response["data"] == {
        "relations": [_expected_relation(row, relation_type) for row, relation_type in zip(before, types, strict=True)]
    }
    assert response["missing_fields"] == [
        "relations[1].supersession_direction_and_primary_evidence",
        "relations[5].relation_type",
    ]
    assert response["conflicts"] == []
    assert response["execution"]["network_calls"] == 0
    assert rows == before


@pytest.mark.parametrize("count", [10, 500], ids=["N10", "N500"])
def test_distinct_effective_origins_use_a_linear_endpoint_key_budget(monkeypatch, count):
    rows = [
        _edge(f"r{index}", f"ORIGIN{index}", f"TARGET{index}")
        if index % 2 == 0
        else _edge(f"r{index}", f"TARGET{index}", f"ORIGIN{index}", direction="to_from")
        for index in range(count)
    ]
    before = deepcopy(rows)
    original_key = parts._relation_endpoint_key
    key_calls = 0

    def counted_key(endpoint):
        nonlocal key_calls
        key_calls += 1
        return original_key(endpoint)

    monkeypatch.setattr(parts, "_relation_endpoint_key", counted_key)

    response = parts.compare_part_relations(rows)

    assert response["ok"] is True
    assert response["outcome"] == "success"
    assert response["data"] == {"relations": [_expected_relation(row) for row in before]}
    assert len(response["data"]["relations"]) == count
    assert all(row["type"] == "supersession" for row in response["data"]["relations"])
    assert all(row["fitment_confirmed"] is False for row in response["data"]["relations"])
    assert response["missing_fields"] == []
    assert response["conflicts"] == []
    assert key_calls <= 4 * count
    assert response["execution"]["network_calls"] == 0
    assert rows == before
