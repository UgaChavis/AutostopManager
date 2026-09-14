from __future__ import annotations

from http.client import IncompleteRead
from unittest.mock import Mock
import pytest
from urllib.error import HTTPError

from autostop_manager import catalog_clients as catalog_clients_module
from autostop_manager import config as manager_config
from autostop_manager.catalog_clients import (
    build_17vin_signed_request,
    build_17vin_token,
    build_denso_aftermarket_search_request,
    build_exist_price_lookup_request,
    build_fapi_catalog_request,
    build_mann_filter_catalog_request,
    build_partsapi_request,
    denso_aftermarket_catalog_lookup,
    exist_price_lookup,
    extract_partsapi_article_candidates,
    extract_partsapi_autonorms_rows,
    extract_partsapi_cross_candidates,
    extract_partsapi_fill_volumes,
    extract_partsapi_search_tree_rows,
    extract_partsapi_parts_by_vin_candidates,
    extract_partsapi_vehicle_profiles,
    fapi_catalog_lookup,
    mann_filter_catalog_lookup,
    parse_exist_catalog_candidates,
    parse_exist_price_page,
    partsapi_catalog_lookup,
    partsapi_operation_status,
    public_aftermarket_catalog_lookup,
    resolve_partsapi_category,
    vin17_decode_vehicle,
)


PARTSAPI_METHOD_ENV_NAMES = [
    "PARTSAPI_VINDECODE_KEY",
    "PARTSAPI_VINDECODE_OE_KEY",
    "PARTSAPI_GOSNOMER2VIN_KEY",
    "PARTSAPI_PARTS_BY_VIN_KEY",
    "PARTSAPI_OE_APPLICABILITY_KEY",
    "PARTSAPI_CROSSES_KEY",
    "PARTSAPI_CROSSES_WITH_BRAND_KEY",
    "PARTSAPI_CROSSES_TITLE_KEY",
    "PARTSAPI_PARTNAME_BY_BRAND_NUMBER_KEY",
    "PARTSAPI_ARTICLE_CROSSES_KEY",
    "PARTSAPI_SEARCH_ARTICLES_KEY",
    "PARTSAPI_GET_ENGINE_KEY",
    "PARTSAPI_SEARCH_TREE_KEY",
    "PARTSAPI_ARTICLES_KEY",
    "PARTSAPI_ARTICLE_KEY",
    "PARTSAPI_ARTICLE_CRITERIA_KEY",
    "PARTSAPI_GET_NORMS_MAKES_KEY",
    "PARTSAPI_GET_NORMS_MODELS_KEY",
    "PARTSAPI_GET_NORMS_MOTORS_KEY",
    "PARTSAPI_GET_NORMS_TIMES_KEY",
    "PARTSAPI_GET_FILL_VOLUMES_KEY",
]


def _clear_partsapi_method_env(monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/tmp/autostop-manager-test-empty.env")
    monkeypatch.setattr(manager_config, "_ENV_LOADED", False)
    for name in PARTSAPI_METHOD_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _clear_fapi_env(monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/tmp/autostop-manager-test-empty.env")
    monkeypatch.setattr(manager_config, "_ENV_LOADED", False)
    monkeypatch.delenv("FAPI_API_KEY", raising=False)
    catalog_clients_module._FAPI_MANUFACTURER_CACHE.clear()


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

    def read(self, limit: int = -1):
        return self.payload if limit < 0 else self.payload[:limit]


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


def test_partsapi_operation_status_is_specific_to_each_method_key(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.delenv("PARTSAPI_KEY", raising=False)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    monkeypatch.setenv("PARTSAPI_PARTS_BY_VIN_KEY", "parts-secret")

    by_vin = partsapi_operation_status("parts_by_vin")
    vin_decode = partsapi_operation_status("vin_decode")

    assert by_vin["live_callable_now"] is True
    assert vin_decode["live_callable_now"] is False
    assert "PARTSAPI_VINDECODE_KEY" in vin_decode["missing_key_env_names"]
    assert "PARTSAPI_KEY" in vin_decode["accepted_key_env_names"]


def test_partsapi_plate_and_part_name_operations_use_documented_params(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.delenv("PARTSAPI_KEY", raising=False)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    monkeypatch.setenv("PARTSAPI_GOSNOMER2VIN_KEY", "plate-secret")
    monkeypatch.setenv("PARTSAPI_PARTNAME_BY_BRAND_NUMBER_KEY", "name-secret")

    plate = partsapi_catalog_lookup(operation="plate_to_vin", registration_number="A564AA99", dry_run=True)
    part_name = partsapi_catalog_lookup(
        operation="part_name_by_brand_number",
        brand="MAHLE",
        part_number="OC1038",
        dry_run=True,
    )

    assert plate["partsapi_method"] == "gosnomer2vin"
    assert plate["request_plan"]["params"] == {"gosnomer": "A56***A99"}
    assert plate["request_plan"]["method_key_env_name"] == "PARTSAPI_GOSNOMER2VIN_KEY"
    assert plate["redacted_registration_number"] == "A56***A99"
    assert "A564AA99" not in plate["request_plan"]["redacted_url"]
    assert part_name["partsapi_method"] == "getPartnameByBrandNumber"
    assert part_name["request_plan"]["params"] == {"brand": "MAHLE", "number": "OC1038", "lang": "ru"}
    assert part_name["request_plan"]["method_key_env_name"] == "PARTSAPI_PARTNAME_BY_BRAND_NUMBER_KEY"
    assert "name-secret" not in part_name["request_plan"]["redacted_url"]


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


def test_partsapi_autonorms_operations_use_method_keys_and_documented_params(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.delenv("PARTSAPI_KEY", raising=False)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    monkeypatch.setenv("PARTSAPI_GET_NORMS_MAKES_KEY", "makes-secret")
    monkeypatch.setenv("PARTSAPI_GET_NORMS_MODELS_KEY", "models-secret")
    monkeypatch.setenv("PARTSAPI_GET_NORMS_MOTORS_KEY", "motors-secret")
    monkeypatch.setenv("PARTSAPI_GET_NORMS_TIMES_KEY", "times-secret")
    monkeypatch.setenv("PARTSAPI_GET_FILL_VOLUMES_KEY", "volumes-secret")

    makes = partsapi_catalog_lookup(operation="norms_makes", dry_run=True)
    models = partsapi_catalog_lookup(operation="norms_models", make_name_seo="toyota", dry_run=True)
    motors = partsapi_catalog_lookup(operation="norms_motors", model_id="123", dry_run=True)
    times = partsapi_catalog_lookup(
        operation="norms_times",
        motor_id="456",
        top_category_id="10",
        sub_category_id="20",
        dry_run=True,
    )
    volumes = partsapi_catalog_lookup(operation="fill_volumes", car_id="24458", dry_run=True)

    assert makes["partsapi_method"] == "GetNormsMakes"
    assert makes["request_plan"]["params"] == {}
    assert models["request_plan"]["params"] == {"makeNameSEO": "toyota"}
    assert motors["request_plan"]["params"] == {"modelId": "123"}
    assert times["request_plan"]["params"] == {"motorId": "456", "TopCatId": "10", "SubCatId": "20"}
    assert volumes["partsapi_method"] == "GetFillVolumes"
    assert volumes["request_plan"]["params"] == {"carId": "24458"}
    for result, expected_env_name, secret in (
        (makes, "PARTSAPI_GET_NORMS_MAKES_KEY", "makes-secret"),
        (models, "PARTSAPI_GET_NORMS_MODELS_KEY", "models-secret"),
        (motors, "PARTSAPI_GET_NORMS_MOTORS_KEY", "motors-secret"),
        (times, "PARTSAPI_GET_NORMS_TIMES_KEY", "times-secret"),
        (volumes, "PARTSAPI_GET_FILL_VOLUMES_KEY", "volumes-secret"),
    ):
        assert result["ok"] is True
        assert result["request_plan"]["method_key_env_name"] == expected_env_name
        assert secret not in result["request_plan"]["redacted_url"]


def test_extract_partsapi_autonorms_times_keeps_norm_hours_separate_from_price():
    rows = extract_partsapi_autonorms_rows(
        operation="norms_times",
        payload={
            "data": {
                "array": [
                    {
                        "workId": "42",
                        "workName": "Замена передних колодок",
                        "workTime": "0.8",
                        "workPrice": "0",
                        "TopCatId": "10",
                        "SubCatId": "20",
                    }
                ]
            }
        },
    )

    assert rows[0]["workName"] == "Замена передних колодок"
    assert rows[0]["workTime"] == "0.8"
    assert rows[0]["workPrice"] == "0"


def test_extract_partsapi_fill_volumes_keeps_fluid_evidence_separate():
    rows = extract_partsapi_fill_volumes(
        payload={
            "data": {
                "array": [
                    {
                        "fillVolume": "4.8",
                        "fillUnit": "л",
                        "fillType": "Моторное масло",
                        "fillTitle": "Двигатель",
                        "fillInfo": "с фильтром",
                    }
                ]
            }
        }
    )

    assert rows[0]["fillVolume"] == "4.8"
    assert rows[0]["fillType"] == "Моторное масло"
    assert rows[0]["source_operation"] == "fill_volumes"


def test_extract_partsapi_plate_and_part_name_payloads():
    profiles = extract_partsapi_vehicle_profiles(
        operation="plate_to_vin",
        payload={"data": {"array": {"VIN": "XW7BF4FK60S145161"}}},
    )
    candidates = extract_partsapi_article_candidates(
        operation="part_name_by_brand_number",
        payload={"data": [{"brand": "MAHLE", "number": "OC1038", "partname": "Масляный фильтр"}]},
    )

    assert profiles[0]["redacted_identifier"] == "XW7***161"
    assert "XW7BF4FK60S145161" not in profiles[0].values()
    assert candidates[0]["brand"] == "MAHLE"
    assert candidates[0]["part_number"] == "OC1038"
    assert candidates[0]["product_name"] == "Масляный фильтр"


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


def test_extract_partsapi_search_tree_rows_keeps_only_documented_nodes():
    rows = extract_partsapi_search_tree_rows(
        payload={
            "data": [
                {
                    "STR_LEVEL": 2,
                    "ROOT_NODE_TEXT": "Двигатель",
                    "ROOT_NODE_STR_ID": 100,
                    "NODE_1_TEXT": "Фильтры",
                    "NODE_1_STR_ID": 110,
                }
            ]
        }
    )

    assert rows[0]["ROOT_NODE_STR_ID"] == 100
    assert rows[0]["NODE_1_STR_ID"] == 110


@pytest.mark.parametrize("operation", ["engine_info", "search_tree"])
def test_partsapi_structured_operation_falls_back_on_unrecognized_payload(monkeypatch, operation):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.setenv("PARTSAPI_KEY", "secret-key")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    payload = {"data": {}} if operation == "engine_info" else {"response": "unrecognised"}
    monkeypatch.setattr(
        "autostop_manager.catalog_clients.urlopen",
        lambda request, timeout=20.0: _FakeResponse(payload),
    )

    result = partsapi_catalog_lookup(operation=operation, type_id="1404")

    assert result["ok"] is True
    assert result["outcome"] == "empty_result"
    assert result["requires_fallback"] is True


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


def test_resolve_partsapi_category_does_not_map_brake_disc_to_brake_pad_numeric_category():
    result = resolve_partsapi_category("тормозные диски")

    assert result["category"] != "1191"
    assert result["category_unresolved"] is True
    assert result["category_kind"] == "text_candidate"


def test_compound_part_request_does_not_automatically_select_one_index_category():
    phrase = "передние колодки и задние амортизаторы"
    result = resolve_partsapi_category(phrase)
    assert result["part_intent"]["intent_id"] == "multiple_parts"
    assert result["index_matches"]  # Retain useful hints without selecting one for the whole request.
    assert result["category"] is None
    assert result["category_unresolved"] is True
    assert resolve_partsapi_category(phrase, explicit_category="1191")["category"] == "1191"


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
    assert candidates[0]["fitment_evidence"]["vin_query_scoped"] is True
    assert candidates[0]["fitment_evidence"]["fitment_status"] == "unconfirmed"
    assert "is_fit_for_this_vin" not in candidates[0]["fitment_evidence"]
    assert candidates[0]["confidence"] == 0.72


def test_partsapi_parts_by_vin_preserves_explicit_negative_fitment():
    candidates = extract_partsapi_parts_by_vin_candidates(
        payload=[
            {
                "group": "Brake",
                "name": "Rear brake pads",
                "parts": "TEST|P-REAR",
                "is_fit_for_this_vin": "false",
            }
        ]
    )

    assert candidates[0]["fitment_evidence"]["is_fit_for_this_vin"] is False
    assert candidates[0]["confidence"] == 0.72


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
    assert result["oem_candidates"][0]["fitment_evidence"]["fitment_status"] == "unconfirmed"
    assert "is_fit_for_this_vin" not in result["oem_candidates"][0]["fitment_evidence"]
    assert "secret-key" not in result["request_plan"]["redacted_url"]


@pytest.mark.parametrize("failure", [TimeoutError("network timeout"), IncompleteRead(b"secret-key", 1)])
def test_partsapi_parts_by_vin_retry_records_attempts_without_secret(monkeypatch, failure):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.setenv("PARTSAPI_KEY", "secret-key")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    calls = []

    def fake_urlopen(request, timeout=20.0):
        calls.append(request.full_url)
        raise failure

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
    assert result["outcome"] == ("timeout" if isinstance(failure, TimeoutError) else "network_error")
    assert "secret-key" not in result["error"]
    assert "secret-key" not in result["request_plan"]["redacted_url"]
    assert len(calls) == 2


def test_partsapi_retry_is_bounded_to_three_attempts(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.setenv("PARTSAPI_KEY", "secret-key")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    calls = []

    def fake_urlopen(request, timeout=20.0):
        calls.append(request.full_url)
        raise TimeoutError("network timeout")

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)
    result = partsapi_catalog_lookup(
        operation="parts_by_vin",
        identifier="XW7BF4FK60S145161",
        part_type="oem",
        category="1191",
        max_attempts=10,
    )

    assert result["outcome"] == "timeout"
    assert result["retryable"] is True
    assert result["attempt_count"] == 3
    assert result["max_attempts"] == 3
    assert len(calls) == 3


def test_partsapi_5xx_is_not_reported_as_empty_result(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.setenv("PARTSAPI_KEY", "secret-key")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    def fail_urlopen(request, timeout=20.0):
        raise HTTPError(request.full_url, 500, "server error", hdrs=None, fp=None)

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fail_urlopen)
    result = partsapi_catalog_lookup(
        operation="parts_by_vin",
        identifier="XW7BF4FK60S145161",
        part_type="oem",
        category="1191",
    )

    assert result["ok"] is False
    assert result["outcome"] == "provider_http_5xx"
    assert result["failure_class"] == "provider_http_5xx"
    assert result["retryable"] is True
    assert result["empty_payload"] is False


def test_partsapi_declared_error_is_not_reported_as_empty_success(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.setenv("PARTSAPI_KEY", "secret-key")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    monkeypatch.setattr(
        "autostop_manager.catalog_clients.urlopen",
        lambda request, timeout=20.0: _FakeResponse({"status": "error", "error": "unsupported identifier"}),
    )

    result = partsapi_catalog_lookup(operation="vin_decode_oe", identifier="MR41S123456")

    assert result["ok"] is False
    assert result["outcome"] == "provider_rejected"
    assert result["requires_fallback"] is True
    assert result["empty_payload"] is False
    assert "secret-key" not in result["request_plan"]["redacted_url"]


def test_partsapi_unknown_nonempty_vin_shape_is_an_empty_result_for_fallback(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.setenv("PARTSAPI_KEY", "secret-key")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    monkeypatch.setattr(
        "autostop_manager.catalog_clients.urlopen",
        lambda request, timeout=20.0: _FakeResponse({"response": "unrecognised"}),
    )

    result = partsapi_catalog_lookup(operation="vin_decode_oe", identifier="MR41S123456")

    assert result["ok"] is True
    assert result["outcome"] == "empty_result"
    assert result["requires_fallback"] is True
    assert result["response_shape"] == "object"


def test_vin17_non_object_payload_is_structured_not_an_exception(monkeypatch):
    monkeypatch.setenv("VIN17_ACCOUNT", "test-user")
    monkeypatch.setenv("VIN17_SECRET", "test-secret")
    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", lambda request, timeout=20.0: _FakeResponse([]))

    result = vin17_decode_vehicle("LFMGJE720DS070251")

    assert result["ok"] is False
    assert result["outcome"] == "adapter_malformed_payload"
    assert result["requires_fallback"] is True


@pytest.mark.parametrize("failure", [ConnectionResetError("connection reset"), IncompleteRead(b"", 1)])
def test_vin17_connection_reset_is_structured_for_fallback(monkeypatch, failure):
    monkeypatch.setenv("VIN17_ACCOUNT", "test-user")
    monkeypatch.setenv("VIN17_SECRET", "test-secret")

    def fail_urlopen(request, timeout=20.0):
        raise failure

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fail_urlopen)

    result = vin17_decode_vehicle("LFMGJE720DS070251")

    assert result["ok"] is False
    assert result["outcome"] == "network_error"
    assert result["retryable"] is True
    assert result["requires_fallback"] is True


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


def test_fapi_request_redacts_api_key_and_keeps_brand_article_context():
    request = build_fapi_catalog_request(
        brand="MANN-FILTER",
        part_number="W 75/3",
        api_key="fapi-test-secret",
        credential_mode="runtime_env",
        manufacturer_id=11617,
    )

    assert request["ok"] is True
    assert request["provider"] == "fapi_catalog"
    assert request["params"] == {
        "brand": "MANN-FILTER",
        "part_number": "W 75/3",
        "manufacturer_id": 11617,
        "minimum_cross_rating": 1,
    }
    assert "fapi-test-secret" not in request["redacted_url"]
    assert "ui=***" in request["redacted_url"]
    assert request["secret_exposed"] is False


def test_fapi_lookup_normalizes_source_part_and_cross_candidates(monkeypatch):
    _clear_fapi_env(monkeypatch)
    monkeypatch.setenv("FAPI_API_KEY", "fapi-test-secret")

    def fake_urlopen(request, timeout=20.0):
        url = request.full_url
        if "/manufacturerList?" in url:
            return _FakeResponse(
                {
                    "mf": [
                        {"i": 0, "ds": "MANN-FILTER", "da": "mann-filter", "dbi": 11617},
                    ]
                }
            )
        assert "/analogList?" in url
        assert "mfi=11617" in url
        assert "r=1" in url
        return _FakeResponse(
            {
                "manufacturerList": {
                    "mf": [
                        {"i": 0, "ds": "MANN-FILTER", "da": "mann-filter", "dbi": 11617},
                        {"i": 1, "ds": "BOSCH", "da": "bosch", "dbi": 30},
                    ]
                },
                "productList": {
                    "p": [
                        {
                            "i": 10,
                            "mfi": 0,
                            "n": "W 75/3",
                            "ns": "w753",
                            "d": "Oil filter",
                            "sr": 4,
                        },
                        {
                            "i": 11,
                            "mfi": 1,
                            "n": "F 026 407 336",
                            "ns": "f026407336",
                            "d": "Oil filter",
                            "sr": 3,
                        },
                    ]
                },
                "analogList": {
                    "a": [
                        {"pi": 10, "pai": 11, "mfi": 0, "mfai": 1, "rp": 4, "rm": 0},
                    ]
                },
            }
        )

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = fapi_catalog_lookup(brand="mann filter", part_number="W75/3", max_analogs=5)

    assert result["ok"] is True
    assert result["outcome"] == "found"
    assert result["matched_brand"] == "MANN-FILTER"
    assert result["source_parts"] == [
        {
            "brand": "MANN-FILTER",
            "part_number": "W 75/3",
            "description": "Oil filter",
            "aggregate_rating": 4,
            "fitment_confirmed": False,
        }
    ]
    assert result["oem_references"] == []
    assert result["oem_references_available"] is False
    assert result["analog_candidates"][0]["brand"] == "BOSCH"
    assert result["analog_candidates"][0]["part_number"] == "F 026 407 336"
    assert result["analog_candidates"][0]["fitment_confirmed"] is False
    assert result["analog_candidates"][0]["cross_evidence"]["positive_ratings"] == 4
    assert "fapi-test-secret" not in result["request_plan"]["redacted_url"]


def test_fapi_empty_response_is_nonfatal_and_requires_fallback(monkeypatch):
    _clear_fapi_env(monkeypatch)
    monkeypatch.setenv("FAPI_API_KEY", "fapi-test-secret")

    def fake_urlopen(request, timeout=20.0):
        if "/manufacturerList?" in request.full_url:
            return _FakeResponse({"mf": [{"i": 0, "ds": "KILEN", "da": "kilen", "dbi": 777}]})
        return _FakeResponse(
            {
                "manufacturerList": {"mf": []},
                "productList": {"p": []},
                "analogList": {"a": []},
            }
        )

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = fapi_catalog_lookup(brand="KILEN", part_number="NOT-A-REAL-ARTICLE")

    assert result["ok"] is True
    assert result["outcome"] == "empty_result"
    assert result["requires_fallback"] is True
    assert result["source_parts"] == []
    assert result["analog_candidates"] == []


@pytest.mark.parametrize("failure_kind", ["http_503", "reset", "incomplete"])
def test_fapi_provider_error_is_safe_and_requires_fallback(monkeypatch, failure_kind):
    import json

    _clear_fapi_env(monkeypatch)
    monkeypatch.setenv("FAPI_API_KEY", "fapi-test-secret")

    def fake_urlopen(request, timeout=20.0):
        if failure_kind == "incomplete":
            response = _FakeResponse({})
            monkeypatch.setattr(response, "read", Mock(side_effect=IncompleteRead(b"fapi-test-secret", 1)))
            return response
        if failure_kind == "reset":
            raise ConnectionResetError("synthetic failure containing fapi-test-secret")
        raise HTTPError(request.full_url, 503, "Service unavailable", {}, None)

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = fapi_catalog_lookup(brand="MANN-FILTER", part_number="W 75/3")

    assert result["ok"] is False
    assert result["failure_class"] == ("provider_http_5xx" if failure_kind == "http_503" else "network_error")
    assert result["retryable"] is True
    assert result["requires_fallback"] is True
    assert "fapi-test-secret" not in json.dumps(result)


def test_fapi_http_200_application_error_is_not_reported_as_empty(monkeypatch):
    _clear_fapi_env(monkeypatch)
    monkeypatch.setenv("FAPI_API_KEY", "fapi-test-secret")

    def fake_urlopen(request, timeout=20.0):
        if "/manufacturerList?" in request.full_url:
            return _FakeResponse({"mf": [{"i": 7, "ds": "KILEN", "da": "KILEN", "dbi": 777}], "al": []})
        return _FakeResponse(
            {
                "manufacturerList": {"mf": []},
                "productList": {"p": []},
                "analogList": {"a": []},
                "messageList": {"m": [{"i": 0, "l": 1, "d": "parameter rejected"}]},
            }
        )

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = fapi_catalog_lookup(brand="KILEN", part_number="10100")

    assert result["ok"] is False
    assert result["outcome"] == "provider_error"
    assert result["failure_class"] == "provider_application_error"
    assert result["provider_message_count"] == 1
    assert result["requires_fallback"] is True


def test_fapi_alias_resolves_canonical_brand_and_manufacturer_list_is_cached(monkeypatch):
    _clear_fapi_env(monkeypatch)
    monkeypatch.setenv("FAPI_API_KEY", "fapi-test-secret")
    manufacturer_calls = 0

    def fake_urlopen(request, timeout=20.0):
        nonlocal manufacturer_calls
        if "/manufacturerList?" in request.full_url:
            manufacturer_calls += 1
            return _FakeResponse(
                {
                    "mf": [{"i": 12, "ds": "MERCEDES-BENZ", "da": "MERCEDESBENZ", "dbi": 111}],
                    "al": [{"mfi": 12, "da": "DAIMLERBENZ"}],
                }
            )
        assert "mfi=111" in request.full_url
        return _FakeResponse(
            {
                "manufacturerList": {"mf": [{"i": 0, "ds": "MERCEDES-BENZ", "da": "MERCEDESBENZ", "dbi": 111}]},
                "productList": {"p": [{"i": 1, "mfi": 0, "n": "A 000 180 26 09", "d": "Oil filter"}]},
                "analogList": {"a": []},
                "messageList": {"m": []},
            }
        )

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    first = fapi_catalog_lookup(brand="Daimler-Benz", part_number="A0001802609")
    second = fapi_catalog_lookup(brand="Daimler-Benz", part_number="A0001802609")

    assert first["ok"] is True
    assert first["outcome"] == "found"
    assert first["matched_brand"] == "MERCEDES-BENZ"
    assert first["source_parts"][0]["brand"] == "MERCEDES-BENZ"
    assert second["ok"] is True
    assert manufacturer_calls == 1


def test_fapi_http_429_is_retryable_with_bounded_retry_after(monkeypatch):
    _clear_fapi_env(monkeypatch)
    monkeypatch.setenv("FAPI_API_KEY", "fapi-test-secret")

    def fake_urlopen(request, timeout=20.0):
        raise HTTPError(request.full_url, 429, "Too Many Requests", {"Retry-After": "999999"}, None)

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = fapi_catalog_lookup(brand="MANN-FILTER", part_number="W 75/3")

    assert result["ok"] is False
    assert result["failure_class"] == "rate_limited"
    assert result["retryable"] is True
    assert result["retry_after_seconds"] == 3600
    assert result["requires_fallback"] is True


def test_fapi_demo_access_is_explicit_and_never_returns_demo_key(monkeypatch):
    import json

    _clear_fapi_env(monkeypatch)

    def fake_urlopen(request, timeout=20.0):
        if request.full_url.startswith("https://gist.githubusercontent.com/"):
            return _FakeRawResponse("fapi-demo-test-secret")
        if "/manufacturerList?" in request.full_url:
            return _FakeResponse({"mf": [{"i": 0, "ds": "KILEN", "da": "kilen", "dbi": 777}]})
        return _FakeResponse(
            {
                "manufacturerList": {"mf": []},
                "productList": {"p": []},
                "analogList": {"a": []},
            }
        )

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = fapi_catalog_lookup(brand="KILEN", part_number="10100", demo_access=True)

    assert result["ok"] is True
    assert result["request_plan"]["credential_mode"] == "demo_ephemeral"
    assert "fapi-demo-test-secret" not in json.dumps(result)


def test_public_aftermarket_catalog_lookup_rejects_unknown_provider():
    result = public_aftermarket_catalog_lookup(provider="unknown", part_number="123")

    assert result["ok"] is False
    assert "mann_filter_catalog" in result["available_providers"]


def test_public_aftermarket_all_reports_aggregate_provider_failures(monkeypatch):
    monkeypatch.setattr(
        "autostop_manager.catalog_clients.mann_filter_catalog_lookup",
        lambda **_kwargs: {"ok": False, "provider": "mann_filter_catalog"},
    )
    monkeypatch.setattr(
        "autostop_manager.catalog_clients.denso_aftermarket_catalog_lookup",
        lambda **_kwargs: {"ok": False, "provider": "denso_aftermarket_catalog"},
    )

    failed = public_aftermarket_catalog_lookup(provider="all", part_number="123")

    assert failed["ok"] is False
    assert failed["success_count"] == 0
    assert failed["failure_count"] == 2

    monkeypatch.setattr(
        "autostop_manager.catalog_clients.mann_filter_catalog_lookup",
        lambda **_kwargs: {"ok": True, "provider": "mann_filter_catalog"},
    )
    partial = public_aftermarket_catalog_lookup(provider="all", part_number="123")

    assert partial["ok"] is True
    assert partial["success_count"] == 1
    assert partial["failure_count"] == 1


def test_public_aftermarket_all_continues_when_fapi_fails(monkeypatch):
    monkeypatch.setattr(
        "autostop_manager.catalog_clients.mann_filter_catalog_lookup",
        lambda **_kwargs: {"ok": True, "provider": "mann_filter_catalog"},
    )
    monkeypatch.setattr(
        "autostop_manager.catalog_clients.denso_aftermarket_catalog_lookup",
        lambda **_kwargs: {"ok": True, "provider": "denso_aftermarket_catalog"},
    )
    monkeypatch.setattr(
        "autostop_manager.catalog_clients.fapi_catalog_lookup",
        lambda **_kwargs: {"ok": False, "provider": "fapi_catalog", "requires_fallback": True},
    )

    result = public_aftermarket_catalog_lookup(
        provider="all", brand="MANN-FILTER", part_number="W 75/3", demo_access=True
    )

    assert result["ok"] is True
    assert result["success_count"] == 2
    assert result["failure_count"] == 1
    assert result["requires_fallback"] is True
    assert [item["provider"] for item in result["results"]] == [
        "mann_filter_catalog",
        "denso_aftermarket_catalog",
        "fapi_catalog",
    ]


def test_public_aftermarket_all_uses_one_deadline_and_returns_safe_partial(monkeypatch):
    calls = []
    ticks = iter([100.0, 100.0, 104.0, 110.0, 111.0])
    monkeypatch.setattr(catalog_clients_module.time, "monotonic", lambda: next(ticks))

    def fake_mann(**kwargs):
        calls.append(("mann", kwargs["timeout"]))
        return {"ok": True, "provider": "mann_filter_catalog"}

    def fake_denso(**kwargs):
        calls.append(("denso", kwargs["timeout"], kwargs["_deadline"]))
        return {"ok": True, "provider": "denso_aftermarket_catalog"}

    monkeypatch.setattr("autostop_manager.catalog_clients.mann_filter_catalog_lookup", fake_mann)
    monkeypatch.setattr("autostop_manager.catalog_clients.denso_aftermarket_catalog_lookup", fake_denso)
    monkeypatch.setattr(
        "autostop_manager.catalog_clients.fapi_catalog_lookup",
        lambda **_kwargs: pytest.fail("FAPI must not start after the shared deadline"),
    )

    result = public_aftermarket_catalog_lookup(
        provider="all",
        brand="MANN-FILTER",
        part_number="W 75/3",
        timeout=10.0,
    )

    assert calls == [("mann", 10.0), ("denso", 6.0, 110.0)]
    assert result["ok"] is True
    assert result["success_count"] == 2
    assert result["failure_count"] == 1
    assert result["partial"] is True
    assert result["requires_fallback"] is True
    assert result["results"][2]["outcome"] == "deadline_exceeded"
