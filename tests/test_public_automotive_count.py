from __future__ import annotations

from copy import deepcopy
import json
from urllib.error import URLError

import pytest

from autostop_manager import public_automotive_evidence as evidence


CONTEXT = {"make": "SyntheticMake", "model": "SyntheticModel", "model_year": 2020, "limit": 1}
COUNT_ERROR = "NHTSA recall response contains an invalid Count"


def _payload(count_json):
    rows = [{"NHTSACampaignNumber": "synthetic-1"}, {"NHTSACampaignNumber": "synthetic-2"}]
    prefix = "" if count_json is None else '"Count":' + count_json + ","
    return ("{" + prefix + '"results":' + json.dumps(rows) + "}").encode()


@pytest.mark.parametrize("count_json", ['"synthetic-private-count"', '["synthetic-private-count"]', '{"x":1}', "1e309"])
@pytest.mark.parametrize("composite", [False, True], ids=["source", "composite"])
def test_invalid_count_is_a_source_failure_and_keeps_other_evidence(monkeypatch, count_json, composite):
    payloads = [_payload(count_json), _payload('"3"')]
    fetches = []
    routes = [{"source_id": "synthetic-local-route", "source_url": "https://synthetic.invalid/reference"}]
    before = deepcopy(routes)

    def fetch(url, *, timeout):
        fetches.append((url, timeout))
        return payloads[len(fetches) - 1]

    monkeypatch.setattr(evidence, "_safe_get_bytes", fetch)
    monkeypatch.setattr(evidence, "official_fluid_reference_routes", lambda **_kwargs: routes)
    monkeypatch.setattr(evidence, "_utc_now", lambda: "2026-01-01T00:00:00Z")

    def call():
        if composite:
            return evidence.lookup_public_automotive_evidence(**CONTEXT, topics=["recalls", "fluids"])
        return evidence.lookup_nhtsa_recalls(**CONTEXT)

    result = call()
    assert result["ok"] is False
    failed = result["evidence"][0] if composite else result
    assert failed["ok"] is False
    assert failed["error"] == COUNT_ERROR
    assert failed["source_id"] == "nhtsa_datasets_apis"
    assert failed["scope"] == "US model-level"
    assert failed["records"] == []
    assert "record_count" not in failed
    assert failed["checked_at"] == "2026-01-01T00:00:00Z"
    assert "make=SyntheticMake" in failed["request_url"]
    assert "synthetic-private-count" not in json.dumps(result, allow_nan=False)
    assert len(fetches) == 1  # Schema failure does not retry acquisition.
    if composite:
        assert result["confidence"] == "low"
        assert result["evidence"][1]["records"] == routes
        assert result["evidence"][1]["source_id"] == "official_fluid_reference_routes"

    healthy = call()
    assert healthy["ok"] is True
    recovered = healthy["evidence"][0] if composite else healthy
    assert recovered["record_count"] == 3
    assert len(recovered["records"]) == 1
    if composite:
        assert healthy["confidence"] == "medium"
        assert healthy["evidence"][1] == result["evidence"][1]
    assert routes == before
    assert len(fetches) == 2


@pytest.mark.parametrize(
    ("count_json", "expected"),
    [
        (None, 2),
        ("null", 2),
        ("0", 2),
        ("false", 2),
        ('""', 2),
        ("[]", 2),
        ("{}", 2),
        ('"3"', 3),
        ("3", 3),
        ("2.9", 2),
        ("true", 1),
    ],
)
def test_existing_count_coercion_fallback_and_display_limit_are_preserved(monkeypatch, count_json, expected):
    monkeypatch.setattr(evidence, "_safe_get_bytes", lambda *_args, **_kwargs: _payload(count_json))
    result = evidence.lookup_nhtsa_recalls(**CONTEXT)
    assert result["ok"] is True
    assert result["record_count"] == expected
    assert len(result["records"]) == 1
    assert result["records"][0]["campaign_number"] == "synthetic-1"


@pytest.mark.parametrize("failure", ["network", "utf8", "json"])
def test_existing_retrieval_and_decode_errors_remain_source_failures(monkeypatch, failure):
    def fetch(*_args, **_kwargs):
        if failure == "network":
            raise URLError("synthetic upstream unavailable")
        return b"\xff" if failure == "utf8" else b"{"

    monkeypatch.setattr(evidence, "_safe_get_bytes", fetch)
    result = evidence.lookup_nhtsa_recalls(**CONTEXT)
    assert result["ok"] is False
    assert result["source_id"] == "nhtsa_datasets_apis"
    assert result["records"] == []
    assert "record_count" not in result
    assert result["error"] and result["error"] != COUNT_ERROR


@pytest.mark.parametrize(
    ("payload", "expected"),
    [(b"null", 0), (b"[]", 0), (b'"synthetic"', 0), (b'{"Count":5,"results":{}}', 5)],
)
def test_existing_non_object_payload_and_non_list_results_tolerance_is_preserved(monkeypatch, payload, expected):
    monkeypatch.setattr(evidence, "_safe_get_bytes", lambda *_args, **_kwargs: payload)
    result = evidence.lookup_nhtsa_recalls(**CONTEXT)
    assert result["ok"] is True
    assert result["record_count"] == expected
    assert result["records"] == []


def test_count_fallback_uses_raw_rows_before_filtering_and_display_limit(monkeypatch):
    payload = b'{"Count":0,"results":[null,{},7]}'
    monkeypatch.setattr(evidence, "_safe_get_bytes", lambda *_args, **_kwargs: payload)
    result = evidence.lookup_nhtsa_recalls(**CONTEXT)
    assert result["ok"] is True
    assert result["record_count"] == 3
    assert len(result["records"]) == 1


def test_invalid_caller_limit_is_not_reclassified_as_an_upstream_count_error(monkeypatch):
    monkeypatch.setattr(evidence, "_safe_get_bytes", lambda *_args, **_kwargs: _payload("1"))
    with pytest.raises(ValueError):
        evidence.lookup_nhtsa_recalls(**(CONTEXT | {"limit": "synthetic-invalid-limit"}))
