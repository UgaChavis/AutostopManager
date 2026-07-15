from __future__ import annotations

from autostop_manager import config as manager_config
from autostop_manager.catalog_clients import (
    build_17vin_signed_request,
    build_17vin_token,
    build_denso_aftermarket_search_request,
    build_emex_find_detail_request,
    build_exist_price_lookup_request,
    build_mann_filter_catalog_request,
    build_partsapi_request,
    denso_aftermarket_catalog_lookup,
    emex_price_lookup,
    exist_price_lookup,
    extract_partsapi_article_candidates,
    extract_partsapi_cross_candidates,
    extract_partsapi_parts_by_vin_candidates,
    extract_partsapi_vehicle_profiles,
    mann_filter_catalog_lookup,
    parse_emex_find_detail_response,
    parse_exist_catalog_candidates,
    parse_exist_price_page,
    partsapi_catalog_lookup,
    public_aftermarket_catalog_lookup,
    resolve_partsapi_category,
    vin17_decode_vehicle,
)


PARTSAPI_METHOD_ENV_NAMES = [
    "PARTSAPI_VINDECODE_KEY",
    "PARTSAPI_VINDECODE_OE_KEY",
    "PARTSAPI_PARTS_BY_VIN_KEY",
    "PARTSAPI_OE_APPLICABILITY_KEY",
    "PARTSAPI_CROSSES_KEY",
    "PARTSAPI_CROSSES_WITH_BRAND_KEY",
    "PARTSAPI_CROSSES_TITLE_KEY",
    "PARTSAPI_ARTICLE_CROSSES_KEY",
    "PARTSAPI_SEARCH_ARTICLES_KEY",
    "PARTSAPI_GET_ENGINE_KEY",
    "PARTSAPI_SEARCH_TREE_KEY",
    "PARTSAPI_ARTICLES_KEY",
    "PARTSAPI_ARTICLE_KEY",
    "PARTSAPI_ARTICLE_CRITERIA_KEY",
]


def _clear_partsapi_method_env(monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/tmp/autostop-manager-test-empty.env")
    monkeypatch.setattr(manager_config, "_ENV_LOADED", False)
    for name in PARTSAPI_METHOD_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


class _FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return False

    def read(self):
        import json

        return json.dumps(self.payload).encode("utf-8")


class _FakeRawResponse:
    def __init__(self, payload: str | bytes):
        self.payload = payload.encode("utf-8") if isinstance(payload, str) else payload

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return False

    def read(self):
        return self.payload


EXIST_CATALOG_HTML = """
<html><body>
  <a class="cat" href="/Price/?pid=D6C13490"><b>Bosch</b> 9 091 901 164 <dd>Свеча зажигания</dd></a>
  <a class="cat" href="/Price/?pid=78A0DDFF"><b>Denso</b> 909190-1164 <dd>Свеча зажигания</dd></a>
  <a class="cat" href="/Price/?pid=02201730"><b>Toyota</b> 90919-01164 <dd>Свеча зажигания &quot;K16R-U11&quot;</dd></a>
</body></html>
"""


def _exist_price_html() -> str:
    import json

    data = [
        {
            "CatalogName": "Toyota",
            "ProductIdEnc": "02201730",
            "PartNumber": "90919-01164",
            "PartName": 'Свеча зажигания "K16R-U11"',
            "BlockName": "Свечи зажигания",
            "BlockTypeId": 1,
            "PriceCount": 86,
            "MinPriceString": "309 ₽",
            "MinDeliveryDaysString": "Завтра",
            "AggregatedParts": [
                {
                    "price": 310,
                    "priceString": "от 310 ₽",
                    "minutes": 6390,
                    "StatisticHTML": '<a title="06.06.2026">Сб 10:30<span></span></a>',
                    "availString": '<a title="Склад поставщика.Заказывайте в необходимом количестве" class="gal"></a>',
                    "basketHTML": '<a class="basket" href="/Profile/Orders/Basket.aspx?in=SECRET"></a>',
                    "InlineProductId": "secret-inline-id",
                    "notReturn": False,
                    "highlightColor": "D3E8CF",
                }
            ],
            "DirectOffers": [
                {
                    "price": 416,
                    "priceString": "416 ₽",
                    "minutes": 1440,
                    "StatisticHTML": '<a title="03.06.2026">Завтра</a>',
                    "availString": '<span title="Офис Красноярск"></span>',
                    "notReturn": True,
                    "highlightColor": "FFE6ED",
                }
            ],
        }
    ]
    return f"""
    <html><body>
      <input id="hdnPid" value="02201730"/>
      <input id="hfPidHash" value="9435e4a9d3431d285eed09d18eb382a7"/>
      <input id="hfSrcId" value="RawPartNumber"/>
      <div>Нашлось предложений: 99 за 1.0</div>
      <script>var _data = {json.dumps(data, ensure_ascii=False)}; var _favs = [];</script>
    </body></html>
    """


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
    assert "myusername" not in request["redacted_url"]
    assert "LFMGJE720DS070251" not in request["redacted_url"]
    assert "vin=LFM***251" in request["redacted_url"]
    assert "user=my***me" in request["redacted_url"]
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
    assert "MR41S123456" not in request["redacted_url"]
    assert "vin=MR4***456" in request["redacted_url"]
    assert "key=***" in request["redacted_url"]
    assert "method=VINdecodeOE" in request["redacted_url"]
    assert request["secret_exposed"] is False


def test_partsapi_lookup_reports_missing_env(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.delenv("PARTSAPI_KEY", raising=False)
    monkeypatch.delenv("PARTSAPI_BASE_URL", raising=False)

    result = partsapi_catalog_lookup(operation="vin_decode_oe", identifier="MR41S123456", dry_run=True)

    assert result["ok"] is False
    assert result["missing_env_names"] == ["PARTSAPI_KEY", "PARTSAPI_BASE_URL"]
    assert result["redacted_identifier"] == "MR4***456"
    assert result["request_plan"]["secret_exposed"] is False


def test_partsapi_lookup_can_use_method_specific_test_key(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
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
    _clear_partsapi_method_env(monkeypatch)
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
    _clear_partsapi_method_env(monkeypatch)
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
    _clear_partsapi_method_env(monkeypatch)
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


def test_partsapi_parts_by_vin_can_omit_type_for_non_oem(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.setenv("PARTSAPI_KEY", "secret-key")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    result = partsapi_catalog_lookup(
        operation="parts_by_vin",
        identifier="XW7BF4FK60S145161",
        part_type="non-oem",
        category="1191",
        dry_run=True,
    )

    assert result["ok"] is True
    assert "type" not in result["request_plan"]["params"]
    assert "type=" not in result["request_plan"]["redacted_url"]
    assert result["request_plan"]["params"]["cat"] == "1191"


def test_partsapi_engine_info_uses_tecdoc_type_params_and_method_key(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.delenv("PARTSAPI_KEY", raising=False)
    monkeypatch.setenv("PARTSAPI_GET_ENGINE_KEY", "method-secret")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    result = partsapi_catalog_lookup(
        operation="engine_info",
        type_id="1404",
        dry_run=True,
    )

    assert result["ok"] is True
    assert result["partsapi_method"] == "getEngine"
    assert result["request_plan"]["method_key_env_name"] == "PARTSAPI_GET_ENGINE_KEY"
    assert result["request_plan"]["params"] == {"TYPE": "PC", "TYPE_ID": "1404", "LANG": 16}
    assert "method=getEngine" in result["request_plan"]["redacted_url"]
    assert "TYPE=PC" in result["request_plan"]["redacted_url"]
    assert "TYPE_ID=1404" in result["request_plan"]["redacted_url"]
    assert "method-secret" not in result["request_plan"]["redacted_url"]


def test_partsapi_search_tree_and_article_operations_use_safe_params(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.delenv("PARTSAPI_KEY", raising=False)
    monkeypatch.setenv("PARTSAPI_SEARCH_TREE_KEY", "tree-secret")
    monkeypatch.setenv("PARTSAPI_ARTICLE_CRITERIA_KEY", "criteria-secret")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    tree = partsapi_catalog_lookup(operation="search_tree", type_id="1404", dry_run=True)
    criteria = partsapi_catalog_lookup(operation="article_criteria", article_id="1878343", dry_run=True)

    assert tree["ok"] is True
    assert tree["partsapi_method"] == "getSearchTree"
    assert tree["request_plan"]["params"] == {"TYPE": "PC", "TYPE_ID": "1404", "LANG": 16}
    assert "tree-secret" not in tree["request_plan"]["redacted_url"]
    assert criteria["ok"] is True
    assert criteria["partsapi_method"] == "getArticleCriteria"
    assert criteria["request_plan"]["params"] == {"ART_ID": "1878343", "LANG": 16}
    assert "criteria-secret" not in criteria["request_plan"]["redacted_url"]


def test_extract_partsapi_vehicle_profiles_handles_engine_info_payload():
    profiles = extract_partsapi_vehicle_profiles(
        operation="engine_info",
        payload={
            "data": {
                "array": {
                    "ENG_ID": "15",
                    "ENG_CODE": "CZDA",
                    "ENG_NAME": "1.4 TSI",
                    "fuelType": "Petrol",
                    "cylinderCapacityCcm": "1395",
                    "powerHpFrom": "150",
                }
            }
        },
    )

    assert profiles == [
        {
            "provider": "partsapi_ru",
            "source_operation": "engine_info",
            "raw_keys": ["ENG_CODE", "ENG_ID", "ENG_NAME", "cylinderCapacityCcm", "fuelType", "powerHpFrom"],
            "engine_id": "15",
            "engine_code": "CZDA",
            "engine_name": "1.4 TSI",
            "fuel_type": "Petrol",
            "displacement_cc": "1395",
            "power_hp_from": "150",
        }
    ]


def test_emex_lookup_reports_missing_credentials(monkeypatch):
    monkeypatch.delenv("EMEX_LOGIN", raising=False)
    monkeypatch.delenv("EMEX_PASSWORD", raising=False)

    result = emex_price_lookup(part_number="9091901164", dry_run=True)

    assert result["ok"] is False
    assert result["missing_env_names"] == ["EMEX_LOGIN", "EMEX_PASSWORD"]
    assert result["request_plan"]["secret_exposed"] is False


def test_emex_request_redacts_credentials(monkeypatch):
    monkeypatch.setenv("EMEX_LOGIN", "client-login")
    monkeypatch.setenv("EMEX_PASSWORD", "client-password")

    request = build_emex_find_detail_request(part_number="9091901164", brand="TY")

    assert request["ok"] is True
    assert request["secret_exposed"] is False
    assert request["params"]["login"] == "cl***in"
    assert request["params"]["password"] == "***"
    assert "client-password" in request["body"]
    assert "client-password" not in request["body_sha256"]


def test_emex_lookup_dry_run_with_configured_env(monkeypatch):
    monkeypatch.setenv("EMEX_LOGIN", "client-login")
    monkeypatch.setenv("EMEX_PASSWORD", "client-password")

    result = emex_price_lookup(part_number="9091901164", dry_run=True)

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["emex_method"] == "FindDetailAdv5"
    assert result["request_plan"]["params"]["password"] == "***"


def test_parse_emex_find_detail_response_extracts_detail_items():
    parsed = parse_emex_find_detail_response(
        """<?xml version="1.0" encoding="utf-8"?>
        <soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
          <soap:Body>
            <FindDetailAdv5Response xmlns="http://tempuri.org/">
              <FindDetailAdv5Result>
                <IsSuccess>true</IsSuccess>
                <ErrorMessage />
                <Details>
                  <DetailItem>
                    <PriceGroup>Original</PriceGroup>
                    <MakeLogo>TY</MakeLogo>
                    <MakeName>Toyota</MakeName>
                    <DetailNum>9091901164</DetailNum>
                    <DetailNameRus>Свеча зажигания</DetailNameRus>
                    <Quantity>5</Quantity>
                    <ADDays>2</ADDays>
                    <DDPercent>90.0</DDPercent>
                    <ResultPrice>437.2300</ResultPrice>
                    <DeliveryRegionType>PRI</DeliveryRegionType>
                  </DetailItem>
                </Details>
              </FindDetailAdv5Result>
            </FindDetailAdv5Response>
          </soap:Body>
        </soap:Envelope>"""
    )

    assert parsed["is_success"] is True
    assert parsed["details"][0]["brand"] == "Toyota"
    assert parsed["details"][0]["part_number"] == "9091901164"
    assert parsed["details"][0]["quantity"] == 5
    assert parsed["details"][0]["price_rub"] == 437.23


def test_emex_lookup_rejects_xml_entities_without_crashing(monkeypatch):
    monkeypatch.setenv("EMEX_LOGIN", "client-login")
    monkeypatch.setenv("EMEX_PASSWORD", "client-password")
    malicious_xml = """<?xml version="1.0"?>
    <!DOCTYPE response [<!ENTITY payload "unexpected">]>
    <FindDetailAdv5Result><ErrorMessage>&payload;</ErrorMessage></FindDetailAdv5Result>
    """
    monkeypatch.setattr(
        "autostop_manager.catalog_clients.urlopen",
        lambda request, timeout=20.0: _FakeRawResponse(malicious_xml),
    )

    result = emex_price_lookup(part_number="9091901164")

    assert result["ok"] is False
    assert "EntitiesForbidden" in result["error"]


def test_exist_request_builds_public_read_only_dry_run_plan():
    request = build_exist_price_lookup_request(part_number="9091901164", brand="Toyota", office_id=905)

    assert request["ok"] is True
    assert request["provider"] == "exist"
    assert request["access_mode"] == "public_site_read_only"
    assert request["office_cookie"] == "_go=905"
    assert "pcode=9091901164" in request["pcode_url"]
    assert request["secret_exposed"] is False


def test_parse_exist_catalog_candidates_extracts_brand_part_and_pid():
    parsed = parse_exist_catalog_candidates(EXIST_CATALOG_HTML, max_candidates=5)

    assert parsed["candidate_count"] == 3
    assert [candidate["brand"] for candidate in parsed["candidates"]] == ["Bosch", "Denso", "Toyota"]
    toyota = parsed["candidates"][2]
    assert toyota["part_number"] == "90919-01164"
    assert toyota["name"] == 'Свеча зажигания "K16R-U11"'
    assert toyota["pid"] == "02201730"
    assert toyota["url"] == "https://www.exist.ru/Price/?pid=02201730"


def test_parse_exist_price_page_normalizes_items_and_strips_basket_html():
    parsed = parse_exist_price_page(_exist_price_html(), max_offers=10)

    assert parsed["ok"] is True
    assert parsed["total_offers"] == 99
    assert parsed["hidden_fields"]["hfSrcId"] == "RawPartNumber"
    item = parsed["items"][0]
    assert item["brand"] == "Toyota"
    assert item["part_number"] == "90919-01164"
    assert item["price_count"] == 86
    assert item["min_price_rub"] == 309
    assert item["min_delivery_label"] == "Завтра"
    assert item["offers"][0]["price_rub"] == 310
    assert item["offers"][0]["lead_time_minutes"] == 6390
    assert item["offers"][0]["lead_time_label"] == "Сб 10:30"
    assert item["offers"][0]["availability_label"].startswith("Склад поставщика")
    assert item["offers"][0]["warehouse_hint"] == "central_exist_stock"
    assert item["offers"][1]["not_return"] is True
    serialized = str(item)
    assert "basketHTML" not in serialized
    assert "InlineProductId" not in serialized
    assert "Basket.aspx" not in serialized


def test_exist_lookup_returns_disambiguation_when_brand_missing(monkeypatch):
    calls: list[str] = []

    def fake_urlopen(request, timeout=20.0):
        url = request.full_url
        calls.append(url)
        if "/Api/Parts/Search" in url:
            return _FakeRawResponse(
                '[{"Name":"9091901164","InputText":"9091901164","NavigateUrl":"/Price/?pcode=9091901164","Relevance":0}]'
            )
        if "pcode=9091901164" in url:
            return _FakeRawResponse(EXIST_CATALOG_HTML)
        message = f"unexpected Exist URL: {url}"
        raise AssertionError(message)

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = exist_price_lookup(part_number="9091901164")

    assert result["ok"] is True
    assert result["needs_disambiguation"] is True
    assert result["selected_item"] is None
    assert {candidate["brand"] for candidate in result["candidates"]} == {"Bosch", "Denso", "Toyota"}
    assert not any("pid=02201730" in url for url in calls)


def test_exist_lookup_selects_requested_brand_and_returns_price(monkeypatch):
    def fake_urlopen(request, timeout=20.0):
        url = request.full_url
        if "/Api/Parts/Search" in url:
            return _FakeRawResponse(
                '[{"Name":"9091901164","InputText":"9091901164","NavigateUrl":"/Price/?pcode=9091901164","Relevance":0}]'
            )
        if "pcode=9091901164" in url:
            return _FakeRawResponse(EXIST_CATALOG_HTML)
        if "pid=02201730" in url:
            return _FakeRawResponse(_exist_price_html())
        message = f"unexpected Exist URL: {url}"
        raise AssertionError(message)

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = exist_price_lookup(part_number="9091901164", brand="Toyota", office_id=905)

    assert result["ok"] is True
    assert result["needs_disambiguation"] is False
    assert result["benchmark_kind"] == "public_retail_reference"
    assert result["office"]["id"] == 905
    assert result["selected_item"]["brand"] == "Toyota"
    assert result["selected_item"]["part_number"] == "90919-01164"
    assert result["selected_item"]["catalog_candidate"]["pid"] == "02201730"


def test_exist_lookup_dry_run_does_not_call_network(monkeypatch):
    def fail_urlopen(request, timeout=20.0):
        message = "dry-run must not call Exist"
        raise AssertionError(message)

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fail_urlopen)

    result = exist_price_lookup(part_number="9091901164", dry_run=True)

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["request_plan"]["office_cookie"] == "_go=905"
    assert result["request_plan"]["search_url"].endswith("searchString=9091901164")


def test_exist_lookup_network_error_returns_json_error(monkeypatch):
    def fake_urlopen(request, timeout=20.0):
        message = "network timeout"
        raise TimeoutError(message)

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = exist_price_lookup(part_number="9091901164")

    assert result["ok"] is False
    assert result["provider"] == "exist"
    assert "network timeout" in result["error"]


def test_resolve_partsapi_category_distinguishes_numeric_and_text_candidates():
    explicit = resolve_partsapi_category("стойка стабилизатора", explicit_category="1191")
    text = resolve_partsapi_category("стойка стабилизатора")
    unknown = resolve_partsapi_category("непонятная редкая деталь")

    assert explicit["category_kind"] == "numeric_id"
    assert explicit["category_unresolved"] is False
    assert text["category_kind"] == "numeric_id"
    assert text["category_unresolved"] is False
    assert text["source"] == "partsapi_category_index"
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
    _clear_partsapi_method_env(monkeypatch)
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


def test_partsapi_parts_by_vin_retry_records_attempts_without_secret(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.setenv("PARTSAPI_KEY", "secret-key")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    calls = []

    def fake_urlopen(request, timeout=20.0):
        calls.append(request.full_url)
        message = "network timeout"
        raise TimeoutError(message)

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = partsapi_catalog_lookup(
        operation="parts_by_vin",
        identifier="XW7BF4FK60S145161",
        part_type="oem",
        category="1191",
        max_attempts=2,
    )

    assert result["ok"] is False
    assert result["attempt_count"] == 2
    assert result["max_attempts"] == 2
    assert [attempt["ok"] for attempt in result["attempts"]] == [False, False]
    assert "network timeout" in result["error"]
    assert "secret-key" not in result["request_plan"]["redacted_url"]
    assert len(calls) == 2


def test_partsapi_oe_applicability_allows_empty_payload(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
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
    _clear_partsapi_method_env(monkeypatch)
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


def test_partsapi_crosses_title_uses_method_key_and_lang(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.delenv("PARTSAPI_KEY", raising=False)
    monkeypatch.setenv("PARTSAPI_CROSSES_TITLE_KEY", "method-secret")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    result = partsapi_catalog_lookup(
        operation="crosses_title",
        part_number="06D109244E",
        lang="en",
        dry_run=True,
    )

    assert result["ok"] is True
    assert result["partsapi_method"] == "getCrossesTitle"
    assert result["request_plan"]["method_key_env_name"] == "PARTSAPI_CROSSES_TITLE_KEY"
    assert result["request_plan"]["params"] == {"lang": "en", "number": "06D109244E"}
    assert "method=getCrossesTitle" in result["request_plan"]["redacted_url"]
    assert "method-secret" not in result["request_plan"]["redacted_url"]


def test_partsapi_crosses_title_normalizes_partname(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.setenv("PARTSAPI_KEY", "secret-key")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    def fake_urlopen(request, timeout=20.0):
        assert "method=getCrossesTitle" in request.full_url
        assert "lang=en" in request.full_url
        assert "number=06D109244E" in request.full_url
        return _FakeResponse(
            [
                {
                    "brand": "VAG",
                    "crossBrand": "INA",
                    "crossNumber": "420008610",
                    "partname": "Timing Chain",
                }
            ]
        )

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = partsapi_catalog_lookup(operation="crosses_title", part_number="06D109244E", lang="en")

    assert result["ok"] is True
    assert result["oem_candidates"] == []
    assert result["cross_candidates"][0]["source_brand"] == "VAG"
    assert result["cross_candidates"][0]["brand"] == "INA"
    assert result["cross_candidates"][0]["part_number"] == "420008610"
    assert result["cross_candidates"][0]["name"] == "Timing Chain"
    assert result["cross_candidates"][0]["fitment_evidence"]["partname"] == "Timing Chain"
    assert "secret-key" not in result["request_plan"]["redacted_url"]


def test_partsapi_crosses_uses_cross_candidates_not_oem(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
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
    _clear_partsapi_method_env(monkeypatch)
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


def test_partsapi_article_crosses_uses_article_id_and_method_key(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.delenv("PARTSAPI_KEY", raising=False)
    monkeypatch.setenv("PARTSAPI_ARTICLE_CROSSES_KEY", "method-secret")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    result = partsapi_catalog_lookup(
        operation="article_crosses",
        article_id="1878343",
        dry_run=True,
    )

    assert result["ok"] is True
    assert result["partsapi_method"] == "getArticleCrosses"
    assert result["request_plan"]["method_key_env_name"] == "PARTSAPI_ARTICLE_CROSSES_KEY"
    assert result["request_plan"]["params"] == {"ART_ID": "1878343", "LANG": 16}
    assert "method=getArticleCrosses" in result["request_plan"]["redacted_url"]
    assert "ART_ID=1878343" in result["request_plan"]["redacted_url"]
    assert "LANG=16" in result["request_plan"]["redacted_url"]
    assert "method-secret" not in result["request_plan"]["redacted_url"]


def test_partsapi_article_crosses_uses_article_candidates_not_oem(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.setenv("PARTSAPI_KEY", "secret-key")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    def fake_urlopen(request, timeout=20.0):
        assert "method=getArticleCrosses" in request.full_url
        assert "ART_ID=1878343" in request.full_url
        assert "LANG=16" in request.full_url
        return _FakeResponse(
            [
                {
                    "ART_ID": 3122568,
                    "ART_ARTICLE_NR": "40219",
                    "ART_SUP_BRAND": "3RG",
                    "ART_PRODUCT_NAME": "Подвеска, двигатель",
                }
            ]
        )

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = partsapi_catalog_lookup(operation="article_crosses", article_id="1878343")

    assert result["ok"] is True
    assert result["oem_candidates"] == []
    assert result["article_candidates"][0]["article_id"] == 3122568
    assert result["article_candidates"][0]["brand"] == "3RG"
    assert result["article_candidates"][0]["part_number"] == "40219"
    assert result["article_candidates"][0]["product_name"] == "Подвеска, двигатель"
    assert "secret-key" not in result["request_plan"]["redacted_url"]


def test_partsapi_article_crosses_normalizes_arl_payload(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.setenv("PARTSAPI_KEY", "secret-key")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    def fake_urlopen(request, timeout=20.0):
        assert "method=getArticleCrosses" in request.full_url
        return _FakeResponse(
            [
                {
                    "ARL_ART_ID": 2558558,
                    "ARL_BRA_BRAND": "LOBRO",
                    "ARL_DISPLAY_NR": "300641",
                    "ART_PRODUCT_NAME": "Приводной вал",
                }
            ]
        )

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = partsapi_catalog_lookup(operation="article_crosses", article_id="1878343")

    assert result["ok"] is True
    assert result["article_candidates"][0]["article_id"] == 2558558
    assert result["article_candidates"][0]["brand"] == "LOBRO"
    assert result["article_candidates"][0]["part_number"] == "300641"
    assert result["article_candidates"][0]["product_name"] == "Приводной вал"


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


def test_mann_filter_lookup_rejects_malformed_payload_without_crashing(monkeypatch):
    monkeypatch.setattr(
        "autostop_manager.catalog_clients.urlopen",
        lambda request, timeout=20.0: _FakeResponse({"data": ["unexpected"]}),
    )

    result = mann_filter_catalog_lookup(part_number="C 2029")

    assert result["ok"] is False
    assert result["error"] == "MANN-FILTER returned a malformed data payload."


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


def test_denso_lookup_rejects_malformed_payload_without_crashing(monkeypatch):
    monkeypatch.setattr(
        "autostop_manager.catalog_clients.urlopen",
        lambda request, timeout=20.0: _FakeResponse({"status": "success", "data": "unexpected"}),
    )

    result = denso_aftermarket_catalog_lookup(part_number="90919-01275", include_detail=False)

    assert result["ok"] is False
    assert result["error"] == "DENSO returned a malformed search data payload."


def test_public_aftermarket_catalog_lookup_rejects_unknown_provider():
    result = public_aftermarket_catalog_lookup(provider="unknown", part_number="123")

    assert result["ok"] is False
    assert "mann_filter_catalog" in result["available_providers"]
