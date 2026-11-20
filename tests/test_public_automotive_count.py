from __future__ import annotations

from copy import deepcopy
import json
from urllib.error import URLError

import pytest

from autostop_manager import public_automotive_evidence as evidence


CONTEXT = {"make": "SyntheticMake", "model": "SyntheticModel", "model_year": 2020, "limit": 1}
COUNT_ERROR = "NHTSA recall response contains an invalid Count"
RESULTS_ERROR = "NHTSA recall response contains invalid results"


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
    "payload",
    [
        b"null",
        b"[]",
        b'"synthetic"',
        b'{"message":"synthetic-upstream-detail"}',
        b'{"Count":5}',
        b'{"Count":5,"results":{}}',
        b'{"Count":1,"results":["synthetic-upstream-detail"]}',
        b'{"Count":1,"results":[]}',
    ],
)
def test_malformed_results_are_source_failure_not_evidence_of_no_recalls(monkeypatch, payload):
    attempts = []

    def fetch(url, **_kwargs):
        attempts.append(url)
        return payload

    monkeypatch.setattr(evidence, "_safe_get_bytes", fetch)
    result = evidence.lookup_public_automotive_evidence(**CONTEXT, topics=["recalls"])

    assert result["ok"] is False
    assert result["confidence"] == "low"
    (source,) = result["evidence"]
    assert source["ok"] is False
    assert source["source_id"] == "nhtsa_datasets_apis"
    assert source["records"] == []
    assert source["error"] == RESULTS_ERROR
    assert "record_count" not in source
    assert "synthetic-upstream-detail" not in json.dumps(result)
    assert len(attempts) == 1


@pytest.mark.parametrize("payload", [b'{"Count":0,"results":[]}', b'{"results":[]}'])
def test_explicit_empty_results_are_successful(monkeypatch, payload):
    monkeypatch.setattr(evidence, "_safe_get_bytes", lambda *_args, **_kwargs: payload)
    result = evidence.lookup_public_automotive_evidence(**CONTEXT, topics=["recalls"])

    assert result["ok"] is True
    (source,) = result["evidence"]
    assert source["ok"] is True
    assert source["record_count"] == 0
    assert source["records"] == []


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
