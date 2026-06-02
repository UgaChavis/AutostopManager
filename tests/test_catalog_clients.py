from __future__ import annotations

from autostop_manager.catalog_clients import (
    build_17vin_signed_request,
    build_17vin_token,
    build_denso_aftermarket_search_request,
    build_mann_filter_catalog_request,
    build_partsapi_request,
    denso_aftermarket_catalog_lookup,
    extract_partsapi_article_candidates,
    extract_partsapi_cross_candidates,
    extract_partsapi_parts_by_vin_candidates,
    extract_partsapi_vehicle_profiles,
    mann_filter_catalog_lookup,
    partsapi_catalog_lookup,
    public_aftermarket_catalog_lookup,
    resolve_partsapi_category,
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


def test_partsapi_lookup_can_use_method_specific_test_key(monkeypatch):
    monkeypatch.delenv("PARTSAPI_KEY", raising=False)
    monkeypatch.setenv("PARTSAPI_VINDECODE_KEY", "method-secret")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    result = partsapi_catalog_lookup(
        operation="vin_decode",
        identifier="WAUBH54B11N110542",
        dry_run=True,
    )

    assert result["ok"] is True
    assert result["request_plan"]["configured"] is True
    assert result["request_plan"]["method_key_env_name"] == "PARTSAPI_VINDECODE_KEY"
    assert "method-secret" not in result["request_plan"]["redacted_url"]


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


def test_partsapi_vin_decode_defaults_to_russian_lang(monkeypatch):
    monkeypatch.setenv("PARTSAPI_KEY", "secret-key")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    result = partsapi_catalog_lookup(
        operation="vin_decode",
        identifier="WAUBH54B11N110542",
        dry_run=True,
    )

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["partsapi_method"] == "VINdecode"
    assert result["request_plan"]["params"] == {"vin": "WAU***542", "lang": "ru"}
    assert "method=VINdecode" in result["request_plan"]["redacted_url"]
    assert "lang=ru" in result["request_plan"]["redacted_url"]
    assert "secret-key" not in result["request_plan"]["redacted_url"]


def test_partsapi_parts_by_vin_defaults_to_oem_type(monkeypatch):
    monkeypatch.setenv("PARTSAPI_KEY", "secret-key")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    result = partsapi_catalog_lookup(
        operation="parts_by_vin",
        identifier="XW7BF4FK60S145161",
        category="1191",
        dry_run=True,
    )

    assert result["ok"] is True
    assert result["request_plan"]["params"]["type"] == "oem"
    assert result["request_plan"]["params"]["cat"] == "1191"


def test_resolve_partsapi_category_distinguishes_numeric_and_text_candidates():
    explicit = resolve_partsapi_category("стойка стабилизатора", explicit_category="1191")
    text = resolve_partsapi_category("стойка стабилизатора")
    unknown = resolve_partsapi_category("непонятная редкая деталь")

    assert explicit["category_kind"] == "numeric_id"
    assert explicit["category_unresolved"] is False
    assert text["category_kind"] == "text_candidate"
    assert text["category_unresolved"] is True
    assert "stabilizer link" in text["text_candidates"]
    assert unknown["category_kind"] == "unresolved"


def test_extract_partsapi_vehicle_profiles_handles_vin_decode_payload():
    profiles = extract_partsapi_vehicle_profiles(
        operation="vin_decode",
        payload={
            "result": {
                "0": {
                    "manuName": "Audi",
                    "modelName": "A6",
                    "typeName": "2.8 quattro",
                    "carId": 12345,
                    "motorCodes": "APR",
                    "yearOfConstrFrom": "2000",
                    "yearOfConstrTo": "2005",
                    "vin": "WAUBH54B11N110542",
                }
            }
        },
    )

    assert profiles[0]["make"] == "Audi"
    assert profiles[0]["model"] == "A6"
    assert profiles[0]["modification"] == "2.8 quattro"
    assert profiles[0]["tecdoc_car_id"] == 12345
    assert profiles[0]["redacted_identifier"] == "WAU***542"


def test_extract_partsapi_vehicle_profiles_handles_vin_decode_oe_payload():
    profiles = extract_partsapi_vehicle_profiles(
        operation="vin_decode_oe",
        payload={
            "data": {
                "array": {
                    "FRAME": "FNN15-502358",
                    "brend": "NISSAN",
                    "katalog": "JP",
                    "modely": "Pulsar",
                    "dvigately": "GA15DE",
                    "modifikacii": "CJ-I",
                    "rynok": "Japan",
                    "data_vypuska": "1997-01",
                    "kpp": "AT",
                }
            }
        },
    )

    assert profiles[0]["make"] == "NISSAN"
    assert profiles[0]["catalog"] == "JP"
    assert profiles[0]["model"] == "Pulsar"
    assert profiles[0]["engine"] == "GA15DE"
    assert profiles[0]["transmission"] == "AT"
    assert profiles[0]["redacted_identifier"] == "FNN***358"


def test_extract_partsapi_parts_by_vin_candidates_splits_brand_article_pairs():
    candidates = extract_partsapi_parts_by_vin_candidates(
        payload=[
            {
                "group": "Body",
                "name": "Windshield",
                "shortname": "Windshield",
                "parts": "CITROEN|5610106660|PEUGEOT|9823628180",
            }
        ]
    )

    assert [candidate["part_number"] for candidate in candidates] == ["5610106660", "9823628180"]
    assert candidates[0]["brand"] == "CITROEN"
    assert candidates[0]["name"] == "Windshield"
    assert candidates[0]["fitment_evidence"]["group"] == "Body"
    assert candidates[0]["fitment_evidence"]["is_fit_for_this_vin"] is True
    assert candidates[0]["confidence"] == 0.95


def test_partsapi_parts_by_vin_live_payload_is_normalized(monkeypatch):
    monkeypatch.setenv("PARTSAPI_KEY", "secret-key")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    def fake_urlopen(request, timeout=20.0):
        assert "method=getPartsbyVIN" in request.full_url
        assert "key=secret-key" in request.full_url
        return _FakeResponse(
            [
                {
                    "group": "Body",
                    "name": "Windshield",
                    "shortname": "Windshield",
                    "parts": "CITROEN|5610106660",
                }
            ]
        )

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = partsapi_catalog_lookup(
        operation="parts_by_vin",
        identifier="XW7BF4FK60S145161",
        part_type="oem",
        category="1191",
    )

    assert result["ok"] is True
    assert result["oem_candidates"][0]["part_number"] == "5610106660"
    assert result["oem_candidates"][0]["brand"] == "CITROEN"
    assert result["oem_candidates"][0]["fitment_evidence"]["is_fit_for_this_vin"] is True
    assert "secret-key" not in result["request_plan"]["redacted_url"]


def test_partsapi_oe_applicability_allows_empty_payload(monkeypatch):
    monkeypatch.setenv("PARTSAPI_KEY", "secret-key")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    def fake_urlopen(request, timeout=20.0):
        assert "method=getOEApplicability" in request.full_url
        assert "query=5610106660" in request.full_url
        return _FakeResponse(None)

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = partsapi_catalog_lookup(operation="oe_applicability", part_number="5610106660")

    assert result["ok"] is True
    assert result["payload"] is None
    assert result["empty_payload"] is True
    assert result["oem_candidates"] == []
    assert "secret-key" not in result["request_plan"]["redacted_url"]


def test_extract_partsapi_cross_candidates_handles_with_brand_payload():
    candidates = extract_partsapi_cross_candidates(
        operation="crosses_with_brand",
        payload=[
            {
                "brand": "NPS",
                "partNumber": "D735005",
                "crossBrand": "KYB",
                "crossNumber": "341123",
            }
        ],
    )

    assert candidates[0]["relationship"] == "cross"
    assert candidates[0]["source_brand"] == "NPS"
    assert candidates[0]["source_part_number"] == "D735005"
    assert candidates[0]["brand"] == "KYB"
    assert candidates[0]["part_number"] == "341123"
    assert candidates[0]["fitment_evidence"]["fitment_confirmed"] is False
    assert candidates[0]["confidence"] == 0.6


def test_partsapi_crosses_with_brand_uses_cross_candidates_not_oem(monkeypatch):
    monkeypatch.setenv("PARTSAPI_KEY", "secret-key")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    def fake_urlopen(request, timeout=20.0):
        assert "method=getCrossesWithBrand" in request.full_url
        assert "number=D735005" in request.full_url
        assert "brand=NPS" in request.full_url
        return _FakeResponse(
            [
                {
                    "brand": "NPS",
                    "partNumber": "D735005",
                    "crossBrand": "KYB",
                    "crossNumber": "341123",
                }
            ]
        )

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = partsapi_catalog_lookup(
        operation="crosses_with_brand",
        part_number="D735005",
        brand="NPS",
    )

    assert result["ok"] is True
    assert result["oem_candidates"] == []
    assert result["cross_candidates"][0]["brand"] == "KYB"
    assert result["cross_candidates"][0]["part_number"] == "341123"
    assert "secret-key" not in result["request_plan"]["redacted_url"]


def test_partsapi_crosses_uses_cross_candidates_not_oem(monkeypatch):
    monkeypatch.setenv("PARTSAPI_KEY", "secret-key")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    def fake_urlopen(request, timeout=20.0):
        assert "method=getCrosses" in request.full_url
        assert "number=D735005" in request.full_url
        return _FakeResponse(
            [
                {
                    "brand": "NPS",
                    "partNumber": "D735005",
                    "crossBrand": "KYB",
                    "crossNumber": "341123",
                }
            ]
        )

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = partsapi_catalog_lookup(operation="crosses", part_number="D735005")

    assert result["ok"] is True
    assert result["oem_candidates"] == []
    assert result["cross_candidates"][0]["source_brand"] == "NPS"
    assert result["cross_candidates"][0]["source_part_number"] == "D735005"
    assert result["cross_candidates"][0]["brand"] == "KYB"
    assert result["cross_candidates"][0]["part_number"] == "341123"
    assert "secret-key" not in result["request_plan"]["redacted_url"]


def test_extract_partsapi_article_candidates_handles_search_articles_payload():
    candidates = extract_partsapi_article_candidates(
        payload=[
            {
                "ART_ID": 3122568,
                "ART_ARTICLE_NR": "40219",
                "ART_SUP_BRAND": "3RG",
                "ART_PRODUCT_NAME": "Подвеска, двигатель",
                "FOUND_VIA": "IAMNumber",
            }
        ],
    )

    assert candidates[0]["article_id"] == 3122568
    assert candidates[0]["part_number"] == "40219"
    assert candidates[0]["brand"] == "3RG"
    assert candidates[0]["product_name"] == "Подвеска, двигатель"
    assert candidates[0]["found_via"] == "IAMNumber"
    assert candidates[0]["fitment_evidence"]["fitment_confirmed"] is False
    assert candidates[0]["confidence"] == 0.5


def test_partsapi_search_articles_uses_article_candidates_not_oem(monkeypatch):
    monkeypatch.setenv("PARTSAPI_KEY", "secret-key")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    def fake_urlopen(request, timeout=20.0):
        assert "method=searchArticles" in request.full_url
        assert "SEARCH_NUMBER=1900" in request.full_url
        assert "LANG=16" in request.full_url
        return _FakeResponse(
            [
                {
                    "ART_ID": 3122568,
                    "ART_ARTICLE_NR": "40219",
                    "ART_SUP_BRAND": "3RG",
                    "ART_PRODUCT_NAME": "Подвеска, двигатель",
                    "FOUND_VIA": "IAMNumber",
                }
            ]
        )

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = partsapi_catalog_lookup(operation="search_articles", part_number="1900")

    assert result["ok"] is True
    assert result["oem_candidates"] == []
    assert result["article_candidates"][0]["article_id"] == 3122568
    assert result["article_candidates"][0]["brand"] == "3RG"
    assert result["article_candidates"][0]["part_number"] == "40219"
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
