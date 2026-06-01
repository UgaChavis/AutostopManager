from __future__ import annotations

from autostop_manager.catalog_clients import (
    build_17vin_signed_request,
    build_17vin_token,
    build_denso_aftermarket_search_request,
    build_mann_filter_catalog_request,
    build_partsapi_request,
    denso_aftermarket_catalog_lookup,
    mann_filter_catalog_lookup,
    partsapi_catalog_lookup,
    public_aftermarket_catalog_lookup,
    vin17_decode_vehicle,
)


class _FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        import json

        return json.dumps(self.payload).encode("utf-8")


def test_build_17vin_token_matches_documented_algorithm():
    token = build_17vin_token(
        user="myusername",
        secret="mypassword",
        url_parameters="/?vin=LFMGJE720DS070251",
    )

    assert token == "92882f5ad20cf8f3330e970af12b4214"


def test_17vin_signed_request_redacts_secret_and_token():
    request = build_17vin_signed_request(
        params={"vin": "LFMGJE720DS070251"},
        user="myusername",
        secret="mypassword",
    )

    assert request["ok"] is True
    assert request["secret_exposed"] is False
    assert "mypassword" not in request["redacted_url"]
    assert "token=***" in request["redacted_url"]
    assert request["url_parameters"] == "/?vin=LFMGJE720DS070251"


def test_vin17_decode_reports_missing_credentials_without_calling_network(monkeypatch):
    monkeypatch.delenv("VIN17_ACCOUNT", raising=False)
    monkeypatch.delenv("VIN17_SECRET", raising=False)

    result = vin17_decode_vehicle("LFMGJE720DS070251")

    assert result["ok"] is False
    assert result["missing_env_names"] == ["VIN17_ACCOUNT", "VIN17_SECRET"]
    assert result["redacted_identifier"] == "LFM***251"


def test_partsapi_request_redacts_key(monkeypatch):
    request = build_partsapi_request(
        method="VINdecodeOE",
        params={"vin": "MR41S123456"},
        key="secret-key",
        base_url="https://partsapi.example.test/api",
    )

    assert request["ok"] is True
    assert "secret-key" not in request["redacted_url"]
    assert "key=%2A%2A%2A" in request["redacted_url"]
    assert "method=VINdecodeOE" in request["redacted_url"]
    assert request["secret_exposed"] is False


def test_partsapi_lookup_reports_missing_env(monkeypatch):
    monkeypatch.delenv("PARTSAPI_KEY", raising=False)
    monkeypatch.delenv("PARTSAPI_BASE_URL", raising=False)

    result = partsapi_catalog_lookup(operation="vin_decode_oe", identifier="MR41S123456", dry_run=True)

    assert result["ok"] is False
    assert result["missing_env_names"] == ["PARTSAPI_KEY", "PARTSAPI_BASE_URL"]
    assert result["redacted_identifier"] == "MR4***456"
    assert result["request_plan"]["secret_exposed"] is False


def test_partsapi_lookup_dry_run_with_configured_env(monkeypatch):
    monkeypatch.setenv("PARTSAPI_KEY", "secret-key")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    result = partsapi_catalog_lookup(
        operation="crosses_with_brand",
        part_number="04465-60280",
        brand="Toyota",
        dry_run=True,
    )

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["partsapi_method"] == "getCrossesWithBrand"
    assert "secret-key" not in result["request_plan"]["redacted_url"]


def test_mann_filter_request_uses_public_graphql_without_secret():
    request = build_mann_filter_catalog_request(part_number="C 2029", page_size=50)

    assert request["ok"] is True
    assert request["provider"] == "mann_filter_catalog"
    assert request["store"] == "pcat_mf_us_store_en"
    assert request["variables"]["pageSize"] == 25
    assert "product_search_name" in request["url"]
    assert request["secret_exposed"] is False


def test_mann_filter_lookup_normalizes_graphql_payload(monkeypatch):
    def fake_urlopen(request, timeout=20.0):
        assert request.headers["Store"] == "pcat_mf_us_store_en"
        return _FakeResponse(
            {
                "data": {
                    "productSearch": {
                        "totalCount": 1,
                        "pageInfo": {"currentPage": 1, "pageSize": 5, "totalPages": 1},
                        "items": [
                            {
                                "product": {
                                    "sku": "C2029_MANN-FILTER",
                                    "name": "C2029_MANN-FILTER",
                                    "stockStatus": "OUT_OF_STOCK",
                                    "urlKey": "c2029_mann-filter",
                                    "oeNumbers": [{"label": "TOYOTA", "value": ["17801-0M020"]}],
                                    "comparisonNumbers": [{"label": "MAHLE/KNECHT", "value": ["LX 2108"]}],
                                }
                            }
                        ],
                    }
                }
            }
        )

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = mann_filter_catalog_lookup(part_number="C 2029")

    assert result["ok"] is True
    assert result["total_count"] == 1
    assert result["items"][0]["sku"] == "C2029_MANN-FILTER"
    assert result["items"][0]["comparison_numbers"][0]["values"] == ["LX 2108"]


def test_denso_request_uses_public_api_without_secret():
    request = build_denso_aftermarket_search_request(part_number="90919-01275")

    assert request["ok"] is True
    assert request["provider"] == "denso_aftermarket_catalog"
    assert request["endpoint"] == "https://www.denso-am.eu/api/v1/search"
    assert "90919-01275" in request["url"]
    assert request["secret_exposed"] is False


def test_denso_lookup_normalizes_search_and_detail_payload(monkeypatch):
    def fake_urlopen(request, timeout=20.0):
        url = request.full_url
        if "/api/v1/search?" in url:
            return _FakeResponse(
                {
                    "status": "success",
                    "data": {
                        "parts": [
                            {
                                "key": 8888,
                                "val": "DENSO Spark plugs: IXEH20TT",
                                "url": "https://www.denso-am.eu/catalog/part/IXEH20TT",
                                "type": "part",
                                "image": "https://assets.example.test/part.jpg",
                                "description": "IXEH20TT (90919-01275)",
                                "part_name": "IXEH20TT",
                            }
                        ]
                    },
                    "total": 1,
                    "offset": 0,
                }
            )
        assert "/api/v1/parts/IXEH20TT?" in url
        return _FakeResponse(
            {
                "status": "success",
                "data": [
                    {
                        "tid": 8888,
                        "name": "IXEH20TT",
                        "title": "Spark Plug",
                        "generic_article": "686",
                        "criteria": [{"label": "Electrode Gap [mm]", "val": "1.0", "vals": ["1.0"]}],
                    }
                ],
            }
        )

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = denso_aftermarket_catalog_lookup(part_number="90919-01275")

    assert result["ok"] is True
    assert result["items"][0]["part_name"] == "IXEH20TT"
    assert result["details"][0]["items"][0]["title"] == "Spark Plug"
    assert result["details"][0]["items"][0]["criteria"][0]["label"] == "Electrode Gap [mm]"


def test_public_aftermarket_catalog_lookup_rejects_unknown_provider():
    result = public_aftermarket_catalog_lookup(provider="unknown", part_number="123")

    assert result["ok"] is False
    assert "mann_filter_catalog" in result["available_providers"]
