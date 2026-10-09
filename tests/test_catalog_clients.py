from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.client import IncompleteRead
from io import BytesIO
import json
import pytest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from autostop_manager import catalog_clients as catalog_clients_module
from autostop_manager import config as manager_config
from autostop_manager import vin_oem_resolver
from autostop_manager.partsapi_methods import PARTSAPI_SHOP_METHODS
from autostop_manager.catalog_clients import (
    PARTSAPI_METHOD_KEY_ENV_NAMES,
    PARTSAPI_OPERATIONS,
    build_denso_aftermarket_search_request,
    build_exist_price_lookup_request,
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
    mann_filter_catalog_lookup,
    parse_exist_catalog_candidates,
    parse_exist_price_page,
    partsapi_catalog_lookup,
    partsapi_operation_status,
    public_aftermarket_catalog_lookup,
    resolve_partsapi_category,
)


PARTSAPI_METHOD_ENV_NAMES = sorted(set(PARTSAPI_METHOD_KEY_ENV_NAMES.values()))


def _clear_partsapi_method_env(monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setattr(manager_config, "_ENV_LOADED", False)
    monkeypatch.delenv("PARTSAPI_KEY", raising=False)
    for name in PARTSAPI_METHOD_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _configure_partsapi_test_keys(monkeypatch):
    for name in PARTSAPI_METHOD_ENV_NAMES:
        monkeypatch.setenv(name, "secret-key")


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


@pytest.fixture
def partsapi_vin_env(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    _configure_partsapi_test_keys(monkeypatch)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    monkeypatch.setattr(catalog_clients_module, "urlopen", lambda *_a, **_k: pytest.fail("Unexpected network call"))


def _vin_row(**fields):
    return {"carId": 12345, "carType": "PC", "manuName": "TEST", "modelName": "MODEL", **fields}


def _partsapi_vin_readback(monkeypatch, payload, *, transmission=None):
    methods = []

    def respond(request, **_kwargs):
        method = parse_qs(urlsplit(request.full_url).query)["method"][0]
        methods.append(method)
        return _FakeResponse(
            {
                "VINdecode": payload,
                "getMakes": [],
                "getSearchTree": [{"NODE_3_TEXT": "Колодки тормозные", "NODE_3_STR_ID": 100470}],
                "getArticles": [{"ART_ID": 1, "ART_NUM": "TEST-1", "SUP_BRAND": "TEST"}],
            }[method]
        )

    monkeypatch.setattr(catalog_clients_module, "urlopen", respond)
    monkeypatch.setattr(
        vin_oem_resolver,
        "decode_vehicle_identity",
        lambda *_a, **_k: {
            "confidence_label": "medium",
            "vehicle_profile": {
                "make": "TEST",
                "model": "MODEL",
                **({"transmission": transmission} if transmission else {}),
            },
            "conflicts": [],
        },
    )
    decoded = partsapi_catalog_lookup(operation="vin_decode", identifier="A" * 17, max_attempts=3)
    methods.clear()
    resolved = vin_oem_resolver.resolve_vin_oem_parts(
        identifier="A" * 17,
        requested_part="передние колодки",
        live_partsapi_oem=True,
        live_vpic=False,
        make="TEST",
        model="MODEL",
        vehicle_type="PC",
    )
    assert resolved["readiness"]["ready_for_crm_writeback"] is False
    for candidate in resolved["article_candidates"]:
        assert candidate["vin_fitment_confirmed"] is False
        assert candidate["manual_review_required"] is True
    return decoded, resolved, methods


@pytest.mark.parametrize(
    ("parent_fields", "children", "expected_transmissions", "agreement"),
    [
        pytest.param({"kp": "6MT"}, [_vin_row()], ["6MT"], "conflict", id="inherit-transmission"),
        pytest.param({"kp": "6MT"}, [_vin_row(kp="")], ["6MT"], "conflict", id="empty-child-field"),
        pytest.param(
            {"kp": "6MT"},
            [_vin_row(carId=67890, kp="6AT")],
            ["6MT", "6AT"],
            "ambiguous_vehicle_modification",
            id="distinct-modifications",
        ),
        pytest.param(
            {"kp": "6MT"},
            [_vin_row(kpp="6AT")],
            ["6MT", "6AT"],
            "ambiguous_vehicle_modification",
            id="conflicting-alias",
        ),
        pytest.param(
            {"kp": "6AT"},
            [_vin_row(manuName="OTHER", kp="6AT")],
            ["6AT", "6AT"],
            "ambiguous_vehicle_modification",
            id="conflicting-make",
        ),
        pytest.param(
            {"kp": "6AT"},
            [_vin_row(modelName="OTHER", kp="6AT")],
            ["6AT", "6AT"],
            "ambiguous_vehicle_modification",
            id="conflicting-model",
        ),
        pytest.param(
            {"kp": "6AT"},
            [_vin_row(carType="LCV", kp="6AT")],
            ["6AT", "6AT"],
            "ambiguous_vehicle_modification",
            id="conflicting-type",
        ),
        pytest.param(
            {"kp": "6AT", "carType": "pc"},
            [_vin_row(carId="12345")],
            ["6AT"],
            "matched",
            id="same-id-and-type-normalized",
        ),
        pytest.param({"carId": None, "kp": "6AT"}, [_vin_row()], ["6AT"], "matched", id="shared-wrapper"),
        pytest.param({"carId": None, "kp": "6MT"}, [_vin_row()], ["6MT"], "conflict", id="shared-wrapper-conflict"),
        pytest.param(
            {"carId": 0, "kp": "6AT"},
            [_vin_row(kp="6AT")],
            ["6AT", "6AT"],
            "ambiguous_vehicle_modification",
            id="nonpositive-parent-id",
        ),
        pytest.param(
            {"kp": "6AT"},
            [_vin_row(carId=True, kp="6AT")],
            ["6AT", "6AT"],
            "ambiguous_vehicle_modification",
            id="boolean-child-id",
        ),
        pytest.param(
            {"kp": "6AT"},
            [_vin_row(carId=None, kp="6AT")],
            ["6AT", "6AT"],
            "ambiguous_vehicle_modification",
            id="missing-child-id",
        ),
        pytest.param(
            {"kp": "6MT"},
            [_vin_row(), _vin_row(carId=67890, kp="6AT")],
            ["6MT", None, "6AT"],
            "ambiguous_vehicle_modification",
            id="multiple-children",
        ),
    ],
)
def test_partsapi_nested_profiles_preserve_parent_context(
    partsapi_vin_env, monkeypatch, parent_fields, children, expected_transmissions, agreement
):
    payload = _vin_row(vin="A" * 17, result=children, **parent_fields)
    before = json.dumps(payload, sort_keys=True)
    decoded, resolved, methods = _partsapi_vin_readback(monkeypatch, payload, transmission="6AT")
    assert [profile.get("transmission") for profile in decoded["vehicle_profiles"]] == expected_transmissions
    assert decoded["identifier_matches_request"] is True
    assert resolved["tecdoc_vehicle"]["identity_agreement"] == agreement
    assert methods == (
        ["VINdecode", "getSearchTree", "getArticles"]
        if agreement == "matched"
        else ["VINdecode"]
        if agreement == "conflict"
        else ["VINdecode", "getMakes"]
    )
    assert json.dumps(payload, sort_keys=True) == before


@pytest.mark.parametrize(
    "alias_fields",
    [
        {"TYPE_ID": 67890, "CAR_TYPE": "PC", "brend": "TEST", "modely": "MODEL"},
        {"typeNumber": 67890, "carType": "PC", "manuShortName": "TEST", "modelName": "MODEL"},
        {"TecDocExternalId": 67890, "CAR_TYPE": "PC"},
        {"CAR_TYPE": "PC", "manuShortName": "TEST", "modely": "MODEL"},
        {"carId": 0, "carType": "PC"},
        {"TecDocExternalId": 0, "CAR_TYPE": "PC"},
    ],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_partsapi_supported_alias_card_cannot_hide_foreign_vin(partsapi_vin_env, monkeypatch, alias_fields, reverse):
    rows = [_vin_row(vin="A" * 17), {"vin": "B" * 17, **alias_fields}]
    if reverse:
        rows.reverse()
    decoded, resolved, methods = _partsapi_vin_readback(monkeypatch, {"result": rows})
    assert len(decoded["vehicle_profiles"]) == 2
    assert [profile["identifier_matches_request"] for profile in decoded["vehicle_profiles"]] == (
        [False, True] if reverse else [True, False]
    )
    assert decoded["ok"] is False
    assert decoded["outcome"] == "identifier_mismatch"
    assert resolved["tecdoc_vehicle"]["identity_agreement"] == "identifier_mismatch"
    assert methods == ["VINdecode", "getMakes"]


@pytest.mark.parametrize(
    "ignored_key", ["metadata", "meta", "request", "params", "echo", "requestParams", "requestParameters"]
)
def test_partsapi_metadata_card_cannot_confirm_response_vin(partsapi_vin_env, monkeypatch, ignored_key):
    decoded, resolved, methods = _partsapi_vin_readback(
        monkeypatch, {ignored_key: _vin_row(vin="A" * 17), "result": {"vin": "B" * 17}}
    )
    assert decoded["vehicle_profiles"] == []
    assert decoded["outcome"] == "unparsed_response"
    assert decoded.get("identifier_matches_request") is None
    assert resolved["tecdoc_vehicle"]["identity_agreement"] == "provider_failed"
    assert methods == ["VINdecode", "getMakes"]


@pytest.mark.parametrize(
    ("context", "child", "agreement", "count"),
    [
        ({"kp": "6MT"}, _vin_row(), "conflict", 1),
        ({"kp": "6AT"}, _vin_row(), "matched", 1),
        ({"manuName": "OTHER"}, _vin_row(), "ambiguous_vehicle_modification", 2),
        ({"modelName": "OTHER"}, _vin_row(), "ambiguous_vehicle_modification", 2),
        ({"carType": "CV"}, _vin_row(), "ambiguous_vehicle_modification", 2),
        ({"engine": "OTHER"}, _vin_row(engine="CURRENT"), "ambiguous_vehicle_modification", 2),
    ],
)
def test_partsapi_sparse_ancestor_context_survives(partsapi_vin_env, monkeypatch, context, child, agreement, count):
    decoded, resolved, methods = _partsapi_vin_readback(
        monkeypatch, {"vin": "A" * 17, **context, "result": [child]}, transmission="6AT"
    )
    assert len(decoded["vehicle_profiles"]) == count
    assert resolved["tecdoc_vehicle"]["identity_agreement"] == agreement
    assert methods == (
        ["VINdecode", "getSearchTree", "getArticles"]
        if agreement == "matched"
        else ["VINdecode"]
        if agreement == "conflict"
        else ["VINdecode", "getMakes"]
    )
    if "kp" in context:
        assert decoded["vehicle_profiles"][0]["transmission"] == context["kp"]


@pytest.mark.parametrize("fields", [{"manuName": "OTHER"}, {"carType": "PC"}, {"kp": "6MT"}])
@pytest.mark.parametrize("reverse", [False, True])
def test_partsapi_foreign_sparse_row_blocks_complete_row(partsapi_vin_env, monkeypatch, fields, reverse):
    rows = [_vin_row(vin="A" * 17), {"vin": "B" * 17, **fields}]
    decoded, resolved, methods = _partsapi_vin_readback(monkeypatch, {"result": rows[::-1] if reverse else rows})
    assert len(decoded["vehicle_profiles"]) == 2
    assert decoded["outcome"] == "identifier_mismatch"
    assert decoded["identifier_matches_request"] is False
    assert resolved["tecdoc_vehicle"]["identity_agreement"] == "identifier_mismatch"
    assert methods == ["VINdecode", "getMakes"]


@pytest.mark.parametrize("envelope", ["data", "result", "array", "items"])
@pytest.mark.parametrize("error_fields", [{"ok": False}, {"error": "Synthetic rejection"}, {"status": "failed"}])
def test_partsapi_nested_provider_rejection_blocks_candidate_lookup(
    partsapi_vin_env, monkeypatch, envelope, error_fields
):
    decoded, resolved, methods = _partsapi_vin_readback(
        monkeypatch, {envelope: {**error_fields, "array": [_vin_row(vin="A" * 17)]}}
    )
    assert decoded["ok"] is False
    assert decoded["outcome"] == "provider_rejected"
    assert decoded["attempt_count"] == 1
    assert decoded["retryable"] is False
    assert resolved["tecdoc_vehicle"]["identity_agreement"] == "provider_failed"
    assert methods == ["VINdecode", "getMakes"]


@pytest.mark.parametrize(
    ("payload", "transmission", "outcome", "matches", "allowed"),
    [
        (_vin_row(vin="A" * 17, TYPE_ID=67890), "6AT", "unparsed_response", None, False),
        (_vin_row(vin="A" * 17, CAR_TYPE="CV"), "6AT", "unparsed_response", None, False),
        (_vin_row(vin="A" * 17, CAR_TYPE="pc"), "6AT", "success", True, True),
        (_vin_row(vin="A" * 17, kp="6AT", kpp="6MT"), "6AT", "unparsed_response", None, False),
        (_vin_row(vin="A" * 17, carId="00123", TYPE_ID=123), "6AT", "success", True, True),
        (_vin_row(vin="A" * 17, kp="6AT", kpp="6 speed automatic"), "6AT", "success", True, True),
        (_vin_row(vin="A" * 17, kp="Automatic", kpp="6AT"), "6AT", "success", True, True),
        (_vin_row(vin="A" * 17, kp="Automatic", kpp="6AT"), "8AT", "success", True, False),
        ("depth_overflow", "6AT", "unparsed_response", None, False),
    ],
)
def test_partsapi_incomplete_or_conflicting_profile_cannot_hide_evidence(
    partsapi_vin_env, monkeypatch, payload, transmission, outcome, matches, allowed
):
    if payload == "depth_overflow":
        foreign = _vin_row(vin="B" * 17)
        for _ in range(4):
            foreign = {"data": foreign}
        payload = {"result": [_vin_row(vin="A" * 17), foreign]}
    decoded, resolved, methods = _partsapi_vin_readback(monkeypatch, payload, transmission=transmission)
    assert decoded["outcome"] == outcome
    assert decoded.get("identifier_matches_request") is matches
    assert decoded["attempt_count"] == 1
    assert decoded["retryable"] is False
    assert methods == (
        ["VINdecode", "getSearchTree", "getArticles"]
        if allowed
        else ["VINdecode", "getMakes"]
        if outcome == "unparsed_response"
        else ["VINdecode"]
    )
    assert resolved["tecdoc_vehicle"]["requires_exact_identifier_confirmation"] is (matches is not True)
    if outcome == "success" and payload.get("kp"):
        assert decoded["vehicle_profiles"][0]["transmission"] == f"{payload['kp']} / {payload['kpp']}"


@pytest.mark.parametrize(
    ("extractor", "row", "empty_row"),
    [
        (extract_partsapi_cross_candidates, {"crossBrand": "TEST", "crossNumber": "TEST-1"}, {"crossNumber": ""}),
        (extract_partsapi_parts_by_vin_candidates, {"parts": "TEST|TEST-1"}, {"parts": ""}),
        (extract_partsapi_article_candidates, {"ART_ID": 1, "ART_ARTICLE_NR": "TEST-1"}, {"ART_ID": None}),
    ],
)
@pytest.mark.parametrize("envelope", ["row", "list", "data", "items", "empty", "scalar"])
def test_partsapi_candidate_parsers_filter_noise_and_duplicates(extractor, row, empty_row, envelope):
    rows = [row, {"unrecognized": "ignore"}, None, row]
    payload = {
        "row": row,
        "list": rows,
        "data": {"data": {"result": rows}},
        "items": {"items": {"data": row}},
        "empty": empty_row,
        "scalar": "unrecognized",
    }[envelope]
    candidates = extractor(payload=payload)
    if envelope in {"empty", "scalar"}:
        assert candidates == []
        return
    assert len(candidates) == 1
    assert candidates[0]["part_number"] == "TEST-1"
    assert candidates[0]["fitment_evidence"].get("fitment_confirmed") is not True
    assert candidates[0]["fitment_evidence"].get("is_fit_for_this_vin") is not True


@pytest.mark.parametrize(
    ("parts", "expected"),
    [("TEST-1", [(None, "TEST-1")]), ("TEST|TEST-1|TEST-2", [("TEST", "TEST-1"), (None, "TEST-2")]), ("|||", [])],
)
def test_partsapi_vin_scoped_tokens_do_not_confirm_fitment(parts, expected):
    candidates = extract_partsapi_parts_by_vin_candidates(payload={"parts": parts, "group": "brakes"})
    assert [(item["brand"], item["part_number"]) for item in candidates] == expected
    assert all(item["fitment_evidence"]["fitment_status"] == "unconfirmed" for item in candidates)
    assert all(item["confidence"] == 0.72 for item in candidates)


@pytest.mark.parametrize(
    ("call_fields", "profile_fields", "allowed"),
    [
        ({}, {"identifier_matches_request": True}, True),
        ({"ok": False}, {"identifier_matches_request": True}, False),
        ({"dry_run": True}, {"identifier_matches_request": True}, False),
        ({"outcome": "identifier_mismatch"}, {"identifier_matches_request": True}, False),
        ({}, {"identifier_matches_request": False}, False),
        ({}, {"identifier_matches_request": "true"}, False),
        ({}, {"requires_exact_identifier_confirmation": True}, False),
        ({"outcome": "identifier_unverified"}, {"requires_exact_identifier_confirmation": True}, False),
        ({"outcome": "identifier_unverified"}, {}, False),
    ],
)
def test_partsapi_candidate_gate_requires_explicit_identifier_evidence(call_fields, profile_fields, allowed):
    call = {"ok": True, "outcome": "success", "vehicle_profiles": [profile_fields], **call_fields}
    assert catalog_clients_module.partsapi_identifier_allows_candidate_lookup(call) is allowed


@pytest.mark.parametrize("operation", ["vin_decode", "decodeVINus"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_partsapi_rejects_conflicting_vin_before_http(partsapi_vin_env, operation, dry_run):
    result = partsapi_catalog_lookup(
        operation=operation, identifier="A" * 17, provider_parameters={"vin": "B" * 17}, dry_run=dry_run
    )
    assert result["ok"] is False
    assert result["outcome"] == "invalid_input"
    assert result["attempt_count"] == 0
    assert result["retryable"] is False


@pytest.mark.parametrize("identifier", [None, " aaaaa aaaaa aaaaaaa "])
def test_partsapi_uses_one_normalized_vin(partsapi_vin_env, monkeypatch, identifier):
    def respond(request, **_kwargs):
        assert parse_qs(urlsplit(request.full_url).query)["vin"] == ["A" * 17]
        return _FakeResponse({"result": [_vin_row(vin="a" * 17)]})

    monkeypatch.setattr(catalog_clients_module, "urlopen", respond)
    result = partsapi_catalog_lookup(
        operation="vin_decode", identifier=identifier, provider_parameters={"vin": "A" * 17}
    )
    assert result["outcome"] == "success"
    assert result["identifier_matches_request"] is True
    assert result["redacted_identifier"] == "AAA***AAA"
    assert result["privacy"]["raw_identifier_is_sensitive"] is True
    assert "A" * 17 not in str(result["request_plan"])
    assert "secret-key" not in str(result["request_plan"])


@pytest.mark.parametrize(
    ("payload", "matches"),
    [
        (_vin_row(vin="A" * 17), True),
        ({"Vin": "A" * 17, "result": [_vin_row()]}, True),
        ({"data": {"VIN": "A" * 17, "array": [_vin_row()]}}, True),
        ({"vin": "B" * 17, "result": [_vin_row()]}, False),
        ({"data": {"Vin": "B" * 17, "result": [_vin_row()]}}, False),
        ({"vin": "B" * 17, "result": [_vin_row(vin="A" * 17)]}, False),
        ({"vin": "A" * 17, "result": [_vin_row(vin="B" * 17)]}, False),
        (_vin_row(vin="A" * 17, result=[_vin_row(vin="B" * 17)]), False),
        (_vin_row(vin="B" * 17, result=[_vin_row(vin="A" * 17)]), False),
        (_vin_row(vin="A" * 17, result={"Vin": "B" * 17}), False),
        (_vin_row(vin="A" * 17, data={"Vin": "B" * 17}), False),
        (_vin_row(vin="A" * 17, metadata={"Vin": "B" * 17}), True),
        (_vin_row(vin="A" * 17, VIN="B" * 17), False),
        (_vin_row(Vin="B" * 17), False),
        ({"vin": "B" * 17, "result": [_vin_row(vin="")]}, False),
        (_vin_row(vin="AAA***********AAA"), None),
        (_vin_row(vin="A" * 8), None),
        (_vin_row(), None),
        ({"request": _vin_row(vin="A" * 17), "result": [_vin_row()]}, None),
        ({"metadata": {"vin": "B" * 17}, "result": [_vin_row(vin="A" * 17)]}, True),
        ({"requestParams": _vin_row(vin="B" * 17), "result": [_vin_row(vin="A" * 17)]}, True),
        ({"requestParameters": _vin_row(vin="B" * 17), "result": [_vin_row(vin="A" * 17)]}, True),
        (_vin_row(vin="A" * 17, metadata={"error": "Unrelated metadata"}), True),
        (_vin_row(result={"Vin": "A" * 17}), None),
        (_vin_row(result={"Vin": "B" * 17}), False),
        ({"result": [_vin_row(vin="A" * 17), _vin_row(vin="B" * 17)]}, False),
        ({"result": [_vin_row(vin="B" * 17), _vin_row(vin="A" * 17)]}, False),
        ({"result": [_vin_row(vin="A" * 17), _vin_row(vin="A" * 17, carId=67890)]}, True),
    ],
)
def test_partsapi_vin_correlation_across_response_branches(partsapi_vin_env, monkeypatch, payload, matches):
    before = json.dumps(payload, sort_keys=True)
    result, resolved, methods = _partsapi_vin_readback(monkeypatch, payload)
    assert result["identifier_matches_request"] is matches
    assert result["requires_exact_identifier_confirmation"] is (matches is not True)
    group_reference = result.get("binding_kind") == "provider_group_reference"
    assert result["outcome"] == (
        "group_match"
        if group_reference
        else "identifier_mismatch"
        if matches is False
        else "identifier_unverified"
        if matches is None
        else "success"
    )
    assert result["ok"] is (matches is not False or group_reference)
    assert result["failure_class"] == (
        "provider_identifier_mismatch" if matches is False and not group_reference else None
    )
    assert result["requires_fallback"] is (matches is not True and not group_reference)
    assert result["attempt_count"] == 1
    assert result["retryable"] is False
    assert result["empty_payload"] is False
    assert "B" * 17 not in str(result["vehicle_profiles"])
    assert json.dumps(payload, sort_keys=True) == before

    allowed = (matches is True or group_reference) and len(result["vehicle_profiles"]) == 1
    assert methods == (["VINdecode", "getSearchTree", "getArticles"] if allowed else ["VINdecode", "getMakes"])
    assert resolved["tecdoc_vehicle"]["identifier_matches_request"] is matches


@pytest.mark.parametrize("parallel", [False, True])
def test_partsapi_vin_requests_remain_isolated(partsapi_vin_env, monkeypatch, parallel):
    def respond(request, **_kwargs):
        vin = parse_qs(urlsplit(request.full_url).query)["vin"][0]
        return _FakeResponse({"result": [_vin_row(vin=vin, carId=ord(vin[0]))]})

    monkeypatch.setattr(catalog_clients_module, "urlopen", respond)
    vins = ["A" * 17, "B" * 17, "A" * 17] * (12 if parallel else 1)

    def lookup(vin):
        return partsapi_catalog_lookup(operation="vin_decode", identifier=vin)

    if parallel:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lookup, vins))
    else:
        results = list(map(lookup, vins))
    for vin, result in zip(vins, results, strict=True):
        assert result["identifier_matches_request"] is True
        assert result["vehicle_profiles"][0]["tecdoc_car_id"] == ord(vin[0])
        assert result["outcome"] == "success"


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


def test_partsapi_request_redacts_key(monkeypatch):
    request = build_partsapi_request(
        method="VINdecode",
        params={"vin": "MR41S123456"},
        key="secret-key",
        base_url="https://partsapi.example.test/api",
    )

    assert request["ok"] is True
    assert "secret-key" not in request["redacted_url"]
    assert "MR41S123456" not in request["redacted_url"]
    assert "vin=MR4***456" in request["redacted_url"]
    assert "key=***" in request["redacted_url"]
    assert "method=VINdecode" in request["redacted_url"]
    assert request["secret_exposed"] is False


def test_partsapi_lookup_reports_missing_env(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.delenv("PARTSAPI_KEY", raising=False)
    monkeypatch.delenv("PARTSAPI_BASE_URL", raising=False)

    result = partsapi_catalog_lookup(operation="vin_decode", identifier="MR41S123456", dry_run=True)

    assert result["ok"] is False
    assert result["authorization_status"] == "not_configured"
    assert result["readiness_basis"] == "configuration_only"
    assert result["live_callable_now"] is False
    assert result["missing_env_names"] == ["PARTSAPI_VINDECODE_KEY", "PARTSAPI_BASE_URL"]
    assert result["redacted_identifier"] == "MR4***456"
    assert result["request_plan"]["secret_exposed"] is False


def test_partsapi_generic_key_does_not_authorize_a_shop_method(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.setenv("PARTSAPI_KEY", "legacy-generic-key")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    status = partsapi_operation_status("vin_decode")
    result = partsapi_catalog_lookup(operation="vin_decode", identifier="SYNTHETICVIN00001", dry_run=True)

    assert status["configured"] is False
    assert status["accepted_key_env_names"] == ["PARTSAPI_VINDECODE_KEY"]
    assert result["outcome"] == "credentials_missing"
    assert result["missing_env_names"] == ["PARTSAPI_VINDECODE_KEY"]
    assert "legacy-generic-key" not in str(result)


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
    monkeypatch.setenv("PARTSAPI_GET_PRODUCT_GROUPS_BY_BRAND_NUMBER_KEY", "groups-secret")

    groups = partsapi_operation_status("getProductGroupsByBrandNumber")
    vin_decode = partsapi_operation_status("vin_decode")

    assert groups["configured"] is True
    assert groups["authorization_status"] == "unverified"
    assert groups["readiness_basis"] == "configuration_only"
    assert groups["outcome"] == "configured_unverified"
    assert groups["live_callable_now"] is False
    assert vin_decode["configured"] is False
    assert vin_decode["authorization_status"] == "not_configured"
    assert vin_decode["readiness_basis"] == "configuration_only"
    assert vin_decode["live_callable_now"] is False
    assert "PARTSAPI_VINDECODE_KEY" in vin_decode["missing_key_env_names"]
    assert vin_decode["accepted_key_env_names"] == ["PARTSAPI_VINDECODE_KEY"]


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
    _configure_partsapi_test_keys(monkeypatch)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    result = partsapi_catalog_lookup(
        operation="crosses_with_brand",
        part_number="04465-60280",
        brand="Toyota",
        dry_run=True,
    )

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["authorization_status"] == "unverified"
    assert result["readiness_basis"] == "configuration_only"
    assert result["live_callable_now"] is False
    assert result["outcome"] == "configured_unverified"
    assert result["partsapi_method"] == "getCrossesWithBrand"
    assert "secret-key" not in result["request_plan"]["redacted_url"]


def test_partsapi_vin_decode_defaults_to_russian_lang(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    _configure_partsapi_test_keys(monkeypatch)
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


def test_partsapi_current_shop_methods_are_the_only_available_operations():
    assert len(PARTSAPI_SHOP_METHODS) == len(PARTSAPI_OPERATIONS) == 43
    assert {spec["method"] for spec in PARTSAPI_OPERATIONS.values()} == set(PARTSAPI_SHOP_METHODS)
    assert {"VINdecodeOE", "getPartsbyVIN", "getOEApplicability"}.isdisjoint(PARTSAPI_SHOP_METHODS)
    assert {"vin_decode_oe", "parts_by_vin", "oe_applicability"}.isdisjoint(PARTSAPI_OPERATIONS)


def test_partsapi_product_groups_requires_all_documented_parameters(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.setenv("PARTSAPI_GET_PRODUCT_GROUPS_BY_BRAND_NUMBER_KEY", "groups-secret")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    missing = partsapi_catalog_lookup(operation="getProductGroupsByBrandNumber", dry_run=True)
    result = partsapi_catalog_lookup(
        operation="getProductGroupsByBrandNumber",
        provider_parameters={"brand": "BOSCH", "sku": "0 092 A68 008", "lang": 16},
        dry_run=True,
    )

    assert missing["outcome"] == "invalid_input"
    assert missing["missing_params"] == ["brand", "sku", "lang"]
    assert result["ok"] is True
    assert result["partsapi_method"] == "getProductGroupsByBrandNumber"
    assert result["request_plan"]["params"] == {"brand": "BOSCH", "sku": "0 092 A68 008", "lang": 16}
    assert result["request_plan"]["method_key_env_name"] == "PARTSAPI_GET_PRODUCT_GROUPS_BY_BRAND_NUMBER_KEY"
    assert "groups-secret" not in str(result)


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
    assert models["request_plan"]["params"] == {"makeNameSEO": "TOYOTA"}
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
    assert tree["request_plan"]["params"] == {"carType": "PC", "carId": "1404", "lang": 16}
    assert "tree-secret" not in tree["request_plan"]["redacted_url"]
    assert criteria["ok"] is True
    assert criteria["partsapi_method"] == "getArticleCriteria"
    assert criteria["request_plan"]["params"] == {"ART_ID": "1878343", "LANG": 16}
    assert "criteria-secret" not in criteria["request_plan"]["redacted_url"]


@pytest.mark.parametrize("envelope", ["list", "row", "data_array"])
def test_partsapi_article_criteria_rows_are_success_without_article_candidates(monkeypatch, envelope):
    _clear_partsapi_method_env(monkeypatch)
    _configure_partsapi_test_keys(monkeypatch)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    row = {"CRITERIA_NAME": "Высота [мм]", "CRITERIA_VALUE": "100"}
    payload = row if envelope == "row" else {"data": {"array": [row]}} if envelope == "data_array" else [row]
    monkeypatch.setattr(
        "autostop_manager.catalog_clients.urlopen",
        lambda request, timeout=20.0: _FakeResponse(payload),
    )

    result = partsapi_catalog_lookup(operation="article_criteria", article_id="12345")

    assert result["ok"] is True
    assert result["outcome"] == "success"
    assert result["empty_payload"] is False
    assert result["requires_fallback"] is False
    assert result["article_candidates"] == []
    assert result["oem_candidates"] == []
    assert result["record_counts"]["article_criteria_rows"] == 1
    assert result["article_criteria_rows"] == [
        {
            "provider": "partsapi_ru",
            "source_operation": "article_criteria",
            "CRITERIA_NAME": "Высота [мм]",
            "CRITERIA_VALUE": "100",
            "raw_keys": ["CRITERIA_NAME", "CRITERIA_VALUE"],
        }
    ]
    assert result["payload"] == payload


@pytest.mark.parametrize(
    "payload",
    [None, [], {"data": []}, [{"response": "unrecognised"}], [{"CRITERIA_NAME": "Высота [мм]", "CRITERIA_VALUE": ""}]],
)
def test_partsapi_article_criteria_requires_recognized_nonempty_rows(monkeypatch, payload):
    _clear_partsapi_method_env(monkeypatch)
    _configure_partsapi_test_keys(monkeypatch)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    monkeypatch.setattr(
        "autostop_manager.catalog_clients.urlopen",
        lambda request, timeout=20.0: _FakeResponse(payload),
    )

    result = partsapi_catalog_lookup(operation="article_criteria", article_id="12345")

    empty_payload = payload in (None, [], {"data": []})
    assert result["ok"] is empty_payload
    assert result["outcome"] == ("empty_result" if empty_payload else "unparsed_response")
    assert result["requires_fallback"] is True
    assert result["article_criteria_rows"] == []


def test_partsapi_article_criteria_preserves_legacy_article_candidates(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    _configure_partsapi_test_keys(monkeypatch)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    monkeypatch.setattr(
        "autostop_manager.catalog_clients.urlopen",
        lambda request, timeout=20.0: _FakeResponse([{"ART_ID": 12345, "ART_ARTICLE_NR": "TEST-100"}]),
    )

    result = partsapi_catalog_lookup(operation="article_criteria", article_id="12345")

    assert result["outcome"] == "success"
    assert result["article_candidates"][0]["article_id"] == 12345
    assert result["article_criteria_rows"] == []


def test_partsapi_criteria_rows_do_not_satisfy_article_search(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    _configure_partsapi_test_keys(monkeypatch)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    monkeypatch.setattr(
        "autostop_manager.catalog_clients.urlopen",
        lambda request, timeout=20.0: _FakeResponse([{"CRITERIA_NAME": "Высота [мм]", "CRITERIA_VALUE": "100"}]),
    )

    result = partsapi_catalog_lookup(operation="search_articles", part_number="TEST-100")

    assert result["ok"] is False
    assert result["outcome"] == "unparsed_response"
    assert result["requires_fallback"] is True
    assert result["article_criteria_rows"] == []


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
            "engine_id": 15,
            "engine_code": "CZDA",
            "engine_name": "1.4 TSI",
            "fuel_type": "Petrol",
            "displacement_cc": 1395,
            "power_hp_from": 150,
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


def test_partsapi_current_search_tree_retains_ids_and_names_for_article_lookup(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    _configure_partsapi_test_keys(monkeypatch)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    payload = [
        {"STR_ID": 100, "STR_ID_PARENT": None, "STR_LEVEL": 1, "STR_NODE_NAME": "Двигатель", "STR_PATH": "Двигатель"},
        {
            "STR_ID": 110,
            "STR_ID_PARENT": 100,
            "STR_LEVEL": 2,
            "STR_NODE_NAME": "Фильтры",
            "STR_PATH": "Двигатель/Фильтры",
        },
    ]
    monkeypatch.setattr(
        "autostop_manager.catalog_clients.urlopen",
        lambda request, timeout=20.0: _FakeResponse(payload),
    )

    result = partsapi_catalog_lookup(operation="search_tree", type_id="12345")

    assert result["outcome"] == "success"
    assert result["record_counts"]["search_tree_rows"] == 2
    for original, normalized in zip(payload, result["search_tree_rows"], strict=True):
        assert all(normalized[key] == value for key, value in original.items())
    articles = partsapi_catalog_lookup(
        operation="articles", type_id="12345", category=str(result["search_tree_rows"][1]["STR_ID"]), dry_run=True
    )
    assert articles["ok"] is True
    assert articles["request_plan"]["params"]["strId"] == "110"


@pytest.mark.parametrize("operation", ["engine_info", "search_tree", "articles"])
def test_partsapi_structured_operation_distinguishes_empty_from_unparsed_payload(monkeypatch, operation):
    _clear_partsapi_method_env(monkeypatch)
    _configure_partsapi_test_keys(monkeypatch)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    payload = {"data": {}} if operation == "engine_info" else {"response": "unrecognised"}
    monkeypatch.setattr(
        "autostop_manager.catalog_clients.urlopen",
        lambda request, timeout=20.0: _FakeResponse(payload),
    )

    result = partsapi_catalog_lookup(operation=operation, type_id="1404", category="1191")

    assert result["ok"] is (operation == "engine_info")
    assert result["outcome"] == ("empty_result" if operation == "engine_info" else "unparsed_response")
    assert result["empty_payload"] is (operation == "engine_info")
    assert result["failure_class"] == (None if operation == "engine_info" else "adapter_unparsed_response")
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


@pytest.mark.parametrize("brand_markup", ["", "<b>---</b>"])
def test_exist_lookup_does_not_select_unknown_brand_as_requested_brand(monkeypatch, brand_markup):
    calls: list[str] = []
    catalog = f'<a href="/Price/?pid=SYNTHETIC">{brand_markup} SYNTHETIC123 <dd>Деталь</dd></a>'

    def fake_urlopen(request, timeout=20.0):
        url = request.full_url
        calls.append(url)
        if "/Api/Parts/Search" in url:
            return _FakeRawResponse("[]")
        if "pcode=SYNTHETIC123" in url:
            return _FakeRawResponse(catalog)
        raise AssertionError("An unknown brand must not be selected for a price request")

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)
    result = exist_price_lookup(part_number="SYNTHETIC123", brand="BOSCH")
    assert result["ok"] is True
    assert result["needs_disambiguation"] is True
    assert result["selected_item"] is None
    assert not any("pid=SYNTHETIC" in url for url in calls)


@pytest.mark.parametrize(("search_payload", "status"), [(b"", 204), (b" \n", 200)])
def test_exist_lookup_continues_to_pcode_when_search_response_is_empty(monkeypatch, search_payload, status):
    calls: list[str] = []

    def fake_urlopen(request, timeout=20.0):
        url = request.full_url
        calls.append(url)
        if "/Api/Parts/Search" in url:
            response = _FakeRawResponse(search_payload)
            response.status = status
            return response
        if "pcode=9091901164" in url:
            return _FakeRawResponse(EXIST_CATALOG_HTML)
        if "pid=02201730" in url:
            return _FakeRawResponse(_exist_price_html())
        message = f"unexpected Exist URL: {url}"
        raise AssertionError(message)

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = exist_price_lookup(part_number="9091901164", brand="Toyota", office_id=905)

    assert result["ok"] is True
    assert result["search_suggestions"] == []
    assert result["selected_item"]["catalog_candidate"]["pid"] == "02201730"
    assert any("pcode=9091901164" in url for url in calls)


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


def test_resolve_partsapi_category_keeps_only_inactive_legacy_hints():
    explicit = resolve_partsapi_category("стойка стабилизатора", explicit_category="1191")
    text = resolve_partsapi_category("стойка стабилизатора")
    unknown = resolve_partsapi_category("непонятная редкая деталь")

    assert explicit["category"] is None
    assert explicit["legacy_category_hint"] == "1191"
    assert explicit["legacy_category_kind"] == "numeric_id"
    assert explicit["category_mode"] == "legacy_inactive"
    assert explicit["category_queryable"] is False
    assert text["category"] is None
    assert text["legacy_category_kind"] == "numeric_id"
    assert text["category_unresolved"] is True
    assert text["source"] == "partsapi_category_index"
    assert "stabilizer link" in text["text_candidates"]
    assert unknown["category_kind"] == "unresolved"


def test_resolve_partsapi_category_curated_text_is_not_a_tecdoc_tree_node():
    result = resolve_partsapi_category("тормозные диски")

    assert result["category"] is None
    assert result["legacy_category_hint"] == "brake disc"
    assert result["legacy_category_kind"] == "text_candidate"
    assert result["category_mode"] == "legacy_inactive"
    assert result["category_queryable"] is False


def test_resolve_partsapi_category_rejects_untrusted_explicit_text():
    result = resolve_partsapi_category("амортизатор", explicit_category="untrusted category")

    assert result["category"] is None
    assert result["legacy_category_hint"] == "untrusted category"
    assert result["category_queryable"] is False
    assert result["category_unresolved"] is True


@pytest.mark.parametrize(
    "phrase",
    [
        "передние колодки и задние амортизаторы",
        "передние колодки 1 комплект и прокладка клапанной крышки 1 шт",
        "передние колодки 1 комплект и передние колодки 2 комплекта",
    ],
)
def test_compound_part_request_does_not_automatically_select_one_index_category(phrase):
    result = resolve_partsapi_category(phrase)
    assert result["part_intent"]["intent_id"] == "multiple_parts"
    assert result["index_matches"]  # Retain useful hints without selecting one for the whole request.
    assert result["category"] is None
    assert result["category_unresolved"] is True
    assert result["legacy_category_hint"] is None
    assert resolve_partsapi_category(phrase, explicit_category="1191")["legacy_category_hint"] == "1191"


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
    assert profiles[0]["production_date_from"] == "2000-01-01"
    assert profiles[0]["production_date_to"] == "2005-12-31"
    assert "model_year_from" not in profiles[0]


@pytest.mark.parametrize(
    "payload",
    [
        {"carId": 12345, "manuName": "TEST", "modelName": "MODEL"},
        [{"carId": 12345, "manuName": "TEST", "modelName": "MODEL"}],
        {"result": {"vehicle": {"carId": 12345, "manuName": "TEST", "modelName": "MODEL"}}},
    ],
)
def test_extract_partsapi_vin_decode_keeps_tecdoc_id_across_documented_response_shapes(payload):
    profiles = extract_partsapi_vehicle_profiles(operation="vin_decode", payload=payload)
    assert len(profiles) == 1
    assert profiles[0]["tecdoc_car_id"] == 12345
    assert profiles[0]["make"] == "TEST"
    assert profiles[0]["model"] == "MODEL"


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


def test_extract_partsapi_vehicle_profiles_handles_top_level_vin_decode_oe_payload():
    profiles = extract_partsapi_vehicle_profiles(
        operation="vin_decode_oe",
        payload={
            "brand": "HONDA",
            "name": "ACCORD",
            "commonAttributes": [
                {"key": "country", "value": "USA"},
                {"key": "manufactured", "value": "2003"},
                {"key": "transmission", "value": "5AT"},
            ],
            "modifications": [
                {"vehicleId": 989},
                {"vehicleId": 990},
            ],
        },
    )

    assert len(profiles) == 1
    assert profiles[0]["make"] == "HONDA"
    assert profiles[0]["model"] == "ACCORD"
    assert profiles[0]["market"] == "USA"
    assert profiles[0]["catalog_year"] == "2003"
    assert "production_date" not in profiles[0]
    assert profiles[0]["transmission"] == "5AT"
    assert "modification" not in profiles[0]


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


def test_partsapi_tecdoc_vin_tree_articles_live_payloads_are_normalized(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    _configure_partsapi_test_keys(monkeypatch)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    def fake_urlopen(request, timeout=20.0):
        assert "key=secret-key" in request.full_url
        if "method=VINdecode" in request.full_url:
            return _FakeResponse(
                {"result": {"carId": 12345, "carType": "PC", "manuName": "TEST", "modelName": "MODEL"}}
            )
        if "method=getSearchTree" in request.full_url:
            return _FakeResponse([{"NODE_2_TEXT": "Brake pad", "NODE_2_STR_ID": 1191}])
        if "method=getArticles" in request.full_url:
            return _FakeResponse(
                [
                    {
                        "ART_ID": 42,
                        "ART_ARTICLE_NR": "TEST-123",
                        "SUP_BRAND": "TEST",
                        "SUP_ID": 7,
                        "PRODUCT_GROUP": "Brake pad",
                        "PT_ID": 88,
                    }
                ]
            )
        pytest.fail("unexpected method")

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    decoded = partsapi_catalog_lookup(operation="vin_decode", identifier="SYNTHETICVIN00001")
    tree = partsapi_catalog_lookup(operation="search_tree", type_id="12345")
    articles = partsapi_catalog_lookup(operation="articles", type_id="12345", category="1191")

    assert decoded["outcome"] == "identifier_unverified"
    assert tree["outcome"] == articles["outcome"] == "success"
    assert decoded["vehicle_profiles"][0]["tecdoc_car_id"] == 12345
    assert decoded["vehicle_profiles"][0]["vehicle_type"] == "PC"
    assert decoded["oem_candidates"] == []
    assert tree["search_tree_rows"][0]["NODE_2_STR_ID"] == 1191
    article = articles["article_candidates"][0]
    assert article["part_number"] == "TEST-123"
    assert article["brand"] == "TEST"
    assert article["supplier_id"] == 7
    assert article["product_group_id"] == 88
    assert article["fitment_evidence"]["fitment_confirmed"] is False
    assert articles["oem_candidates"] == []
    assert all("secret-key" not in str(row["request_plan"]) for row in (decoded, tree, articles))


@pytest.mark.parametrize("failure", [TimeoutError("network timeout"), IncompleteRead(b"secret-key", 1)])
def test_partsapi_vin_decode_retry_records_attempts_without_secret(monkeypatch, failure):
    _clear_partsapi_method_env(monkeypatch)
    _configure_partsapi_test_keys(monkeypatch)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    calls = []

    def fake_urlopen(request, timeout=20.0):
        calls.append(request.full_url)
        raise failure

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = partsapi_catalog_lookup(
        operation="vin_decode",
        identifier="SYNTHETICVIN00001",
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
    _configure_partsapi_test_keys(monkeypatch)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    calls = []

    def fake_urlopen(request, timeout=20.0):
        calls.append(request.full_url)
        raise TimeoutError("network timeout")

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)
    result = partsapi_catalog_lookup(
        operation="vin_decode",
        identifier="SYNTHETICVIN00001",
        max_attempts=10,
    )

    assert result["outcome"] == "timeout"
    assert result["retryable"] is True
    assert result["attempt_count"] == 3
    assert result["max_attempts"] == 3
    assert len(calls) == 3


def test_partsapi_5xx_is_not_reported_as_empty_result(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    _configure_partsapi_test_keys(monkeypatch)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    def fail_urlopen(request, timeout=20.0):
        raise HTTPError(request.full_url, 500, "server error", hdrs=None, fp=None)

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fail_urlopen)
    result = partsapi_catalog_lookup(
        operation="vin_decode",
        identifier="SYNTHETICVIN00001",
    )

    assert result["ok"] is False
    assert result["outcome"] == "provider_http_5xx"
    assert result["failure_class"] == "provider_http_5xx"
    assert result["retryable"] is True
    assert result["empty_payload"] is False


@pytest.mark.parametrize(
    "provider_payload",
    [
        {"error_code": 5000, "message": "provider detail must not be returned", "status": 401},
        {"message": "Exceeded the number of requests from the current IP address.", "status": 401},
    ],
)
def test_partsapi_ip_quota_401_requires_provider_action_without_retry_or_raw_body(monkeypatch, provider_payload):
    _clear_partsapi_method_env(monkeypatch)
    _configure_partsapi_test_keys(monkeypatch)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    calls = []

    def fail_urlopen(request, timeout=20.0):
        calls.append(request.full_url)
        body = json.dumps(provider_payload).encode("utf-8")
        raise HTTPError(request.full_url, 401, "unauthorized", hdrs=None, fp=BytesIO(body))

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fail_urlopen)

    result = partsapi_catalog_lookup(
        operation="vin_decode",
        identifier="SYNTHETICVIN00001",
        max_attempts=3,
    )

    assert result["ok"] is False
    assert result["outcome"] == "provider_ip_quota_exceeded"
    assert result["failure_class"] == "provider_ip_quota_exceeded"
    assert result["retryable"] is False
    assert result["requires_provider_action"] is True
    assert result["requires_fallback"] is True
    assert result["attempt_count"] == 1
    assert len(calls) == 1
    serialized = json.dumps(result, ensure_ascii=False)
    assert "provider detail must not be returned" not in serialized
    assert "Exceeded the number of requests from the current IP address." not in serialized
    assert "secret-key" not in serialized


@pytest.mark.parametrize(
    "provider_body",
    [b"x" * 4097, b"{not-json private-provider-detail"],
)
def test_partsapi_untrusted_http_error_body_is_bounded_closed_and_not_returned(monkeypatch, provider_body):
    _clear_partsapi_method_env(monkeypatch)
    _configure_partsapi_test_keys(monkeypatch)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    streams = []

    def fail_urlopen(request, timeout=20.0):
        stream = BytesIO(provider_body)
        streams.append(stream)
        raise HTTPError(request.full_url, 401, "unauthorized", hdrs=None, fp=stream)

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fail_urlopen)

    result = partsapi_catalog_lookup(
        operation="vin_decode",
        identifier="SYNTHETICVIN00001",
        max_attempts=3,
    )

    assert result["failure_class"] == "provider_auth_error"
    assert result["attempt_count"] == 1
    assert streams and streams[0].closed is True
    serialized = json.dumps(result, ensure_ascii=False)
    assert "private-provider-detail" not in serialized
    assert "secret-key" not in serialized


def test_partsapi_declared_error_is_not_reported_as_empty_success(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    _configure_partsapi_test_keys(monkeypatch)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    monkeypatch.setattr(
        "autostop_manager.catalog_clients.urlopen",
        lambda request, timeout=20.0: _FakeResponse({"status": "error", "error": "unsupported identifier"}),
    )

    result = partsapi_catalog_lookup(operation="vin_decode", identifier="SYNTHETICVIN00001")

    assert result["ok"] is False
    assert result["outcome"] == "provider_rejected"
    assert result["requires_fallback"] is True
    assert result["empty_payload"] is False
    assert "secret-key" not in result["request_plan"]["redacted_url"]


def test_partsapi_unknown_nonempty_vin_shape_is_a_parser_gap(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    _configure_partsapi_test_keys(monkeypatch)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    monkeypatch.setattr(
        "autostop_manager.catalog_clients.urlopen",
        lambda request, timeout=20.0: _FakeResponse({"response": "unrecognised"}),
    )

    result = partsapi_catalog_lookup(operation="vin_decode", identifier="SYNTHETICVIN00001")

    assert result["ok"] is False
    assert result["outcome"] == "unparsed_response"
    assert result["failure_class"] == "adapter_unparsed_response"
    assert result["empty_payload"] is False
    assert result["requires_fallback"] is True
    assert result["response_shape"] == "object"


def test_partsapi_search_tree_allows_empty_payload(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    _configure_partsapi_test_keys(monkeypatch)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    def fake_urlopen(request, timeout=20.0):
        assert "method=getSearchTree" in request.full_url
        assert "carId=12345" in request.full_url
        return _FakeResponse(None)

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = partsapi_catalog_lookup(operation="search_tree", type_id="12345")

    assert result["ok"] is True
    assert result["payload"] is None
    assert result["empty_payload"] is True
    assert result["search_tree_rows"] == []
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
    _configure_partsapi_test_keys(monkeypatch)
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
    _configure_partsapi_test_keys(monkeypatch)
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
    _configure_partsapi_test_keys(monkeypatch)
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
    _configure_partsapi_test_keys(monkeypatch)
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
    _configure_partsapi_test_keys(monkeypatch)
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
    _configure_partsapi_test_keys(monkeypatch)
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

    result = public_aftermarket_catalog_lookup(
        provider="all",
        part_number="W 75/3",
        timeout=10.0,
    )

    assert calls == [("mann", 10.0), ("denso", 6.0, 110.0)]
    assert result["ok"] is True
    assert result["success_count"] == 2
    assert result["failure_count"] == 0
    assert result["partial"] is True
    assert result["requires_fallback"] is True
    assert len(result["results"]) == 2


def test_partsapi_oe_shared_details_survive_without_selecting_modification():
    profile = extract_partsapi_vehicle_profiles(
        operation="vin_decode_oe",
        payload={
            "brand": "TOYOTA",
            "name": "TEST SERIES",
            "commonAttributes": [
                {"key": "date", "value": "02.2016"},
                {"key": "model", "value": "TEST-MODEL-CODE"},
                {"key": "prodPeriod", "value": "2014 - 2018"},
                {"key": "framecolor", "value": "TEST-PAINT"},
                {"key": "trimcolor", "value": "TEST-TRIM"},
                {
                    "key": "options",
                    "value": "АКПП/МКПП: AUTOMATIC; Тип трансмиссии: 6AT; Двигатель: TEST ENGINE; Расположение руля: LEFT; Тип кузова: WAGON; Комплектация: STANDARD; Рынок сбыта: EUROPE",
                },
            ],
            "modifications": [{"engine": "WRONG ENGINE"}],
        },
    )[0]
    assert profile["model"] == "TEST SERIES"
    assert profile["model_code"] == "TEST-MODEL-CODE"
    assert profile["production_date"] == "02.2016"
    assert profile["production_period"] == "2014 - 2018"
    assert profile["frame_color"] == "TEST-PAINT"
    assert profile["trim_color"] == "TEST-TRIM"
    assert profile["engine"] == "TEST ENGINE"
    assert profile["transmission"] == "6AT"
    assert profile["steering"] == "LEFT"
    assert profile["market"] == "EUROPE"
    assert "modification" not in profile


@pytest.mark.parametrize(
    "parameters",
    [
        {"key": "injected"},
        {"method": "other"},
        {"url": "https://invalid.test"},
        {"carType": True},
        {"carType": float("nan")},
    ],
)
def test_partsapi_provider_overrides_reject_unknown_or_unsafe_input(monkeypatch, parameters):
    monkeypatch.setattr(catalog_clients_module, "urlopen", lambda *a, **kw: pytest.fail("network must not run"))
    result = partsapi_catalog_lookup(operation="getMakes", provider_parameters=parameters)
    assert result["outcome"] == "invalid_input"
    assert result["attempt_count"] == 0


def test_partsapi_new_shop_methods_use_method_key_and_require_parameters(monkeypatch):
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    monkeypatch.setenv("PARTSAPI_GET_MAKES_KEY", "private-method-key")
    monkeypatch.setenv("PARTSAPI_KEY", "wrong-generic-key")
    monkeypatch.setattr(catalog_clients_module, "urlopen", lambda *a, **kw: pytest.fail("dry run must not spend quota"))
    missing = partsapi_catalog_lookup(operation="getMakes", dry_run=True)
    assert missing["missing_params"] == ["carType"]
    result = partsapi_catalog_lookup(operation="getMakes", provider_parameters={"carType": "PC"}, dry_run=True)
    assert result["ok"]
    assert result["request_plan"]["params"] == {"carType": "PC"}
    assert result["request_plan"]["method_key_env_name"] == "PARTSAPI_GET_MAKES_KEY"
    assert "private-method-key" not in str(result)
    assert "wrong-generic-key" not in str(result)


def test_partsapi_article_requires_number_and_supplier_not_old_id(monkeypatch):
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    monkeypatch.setenv("PARTSAPI_ARTICLE_KEY", "private-key")
    old = partsapi_catalog_lookup(operation="article", article_id="123", dry_run=True)
    assert old["outcome"] == "invalid_input"
    result = partsapi_catalog_lookup(operation="article", part_number="TEST-PART", supplier_id=42, dry_run=True)
    assert result["ok"]
    assert result["request_plan"]["params"] == {"ART_NUM": "TEST-PART", "SUP_ID": 42, "LANG": 16}


def test_partsapi_returned_different_vin_is_not_exact_confirmation():
    profile = extract_partsapi_vehicle_profiles(
        operation="vin_decode",
        requested_identifier="A" * 17,
        payload={"result": [{"brand": "TEST", "vin": "B" * 17, "model": "TEST MODEL"}]},
    )[0]
    assert profile["identifier_matches_request"] is False
    assert profile["requires_exact_identifier_confirmation"] is True
    assert "B" * 17 not in str(profile)


def test_partsapi_same_prefix_for_different_model_years_keeps_requested_vin(partsapi_vin_env, monkeypatch):
    original_vins = ["WVGZZZ7P0GD000001", "WVGZZZ7P0HD000002"]
    catalog_vin = "WVGZZZ7P0FD000003"
    seen = []

    def respond(request, **_kwargs):
        seen.append(parse_qs(urlsplit(request.full_url).query)["vin"][0])
        return _FakeResponse(
            {"result": [_vin_row(vin=catalog_vin, vin8=catalog_vin[:8], modelName="Touareg", modelyearfrom=2015)]}
        )

    monkeypatch.setattr(catalog_clients_module, "urlopen", respond)
    responses = [partsapi_catalog_lookup(operation="vin_decode", identifier=vin) for vin in original_vins]
    assert seen == original_vins
    for vin, result in zip(original_vins, responses, strict=True):
        assert result["outcome"] == "group_match"
        assert result["ok"] is True
        assert result["failure_class"] is None
        assert result["redacted_requested_identifier"] == vin[:3] + "***" + vin[-3:]
        profile = result["vehicle_profiles"][0]
        assert profile["redacted_requested_identifier"] == result["redacted_requested_identifier"]
        assert profile["redacted_identifier"] != result["redacted_requested_identifier"]
        assert profile["provider_identifier_is_vehicle_confirmation"] is False
        assert profile["vin_prefix_is_vehicle_confirmation"] is False
        assert "vin8" not in profile
        assert catalog_vin not in str(profile)


def test_partsapi_vin8_only_does_not_confirm_full_vin(partsapi_vin_env, monkeypatch):
    monkeypatch.setattr(catalog_clients_module, "urlopen", lambda *_a, **_k: _FakeResponse(_vin_row(vin8="WVGZZZ7P")))
    result = partsapi_catalog_lookup(operation="vin_decode", identifier="WVGZZZ7P0GD000001")
    assert result["outcome"] == "identifier_unverified"
    assert result["identifier_matches_request"] is None
    assert result["vehicle_profiles"][0]["vin_prefix_is_vehicle_confirmation"] is False


def test_partsapi_vin_decode_production_interval_and_model_year_remain_separate():
    profile = extract_partsapi_vehicle_profiles(
        operation="vin_decode",
        requested_identifier="A" * 17,
        payload=_vin_row(
            vin="A" * 17,
            yearOfConstrFrom="Tue, 01 May 2018 00:00:00 GMT",
            yearOfConstrTo="",
            modelyearfrom=2019,
            modelyearto=2023,
        ),
    )[0]
    assert profile["production_date_from"] == "2018-05-01"
    assert profile["production_year_from"] == 2018
    assert profile["model_year_from"] == 2019
    assert profile["model_year_to"] == 2023
    assert "production_date_to" not in profile
    assert profile["identifier_matches_request"] is True
    assert profile["provider_identifier_is_vehicle_confirmation"] is False


@pytest.mark.parametrize(
    ("start", "finish", "normalized_start", "normalized_finish", "precision"),
    [
        ("Tue, 01 May 2018 00:00:00 GMT", "Sat, 31 Dec 2022 00:00:00 GMT", "2018-05-01", "2022-12-31", "day"),
        ("2018-05-01", "2022-12-31", "2018-05-01", "2022-12-31", "day"),
        ("2018", "2022", "2018-01-01", "2022-12-31", "year"),
        ("201805", "202212", "2018-05-01", "2022-12-31", "month"),
        ("2018-05", "2022-12", "2018-05-01", "2022-12-31", "month"),
        ("2018/05", "2022/12", "2018-05-01", "2022-12-31", "month"),
    ],
)
def test_partsapi_get_cars_normalizes_production_dates(start, finish, normalized_start, normalized_finish, precision):
    profile = extract_partsapi_vehicle_profiles(
        operation="getCars", payload=[{"carId": 1, "yearOfConstrFrom": start, "yearOfConstrTo": finish}]
    )[0]
    assert profile["production_date_from"] == normalized_start
    assert profile["production_date_to"] == normalized_finish
    assert profile["production_year_from"] == 2018
    assert profile["production_year_to"] == 2022
    assert profile["production_boundaries"]["production_date_from"]["precision"] == precision
    assert "model_year_from" not in profile
    assert "model_year_to" not in profile


@pytest.mark.parametrize("boundary", [None, "", "  "])
def test_partsapi_get_cars_empty_production_boundary_is_open(boundary):
    profile = extract_partsapi_vehicle_profiles(
        operation="getCars", payload=[{"carId": 1, "yearOfConstrFrom": boundary, "yearOfConstrTo": boundary}]
    )[0]
    assert "production_boundaries" not in profile
    assert "production_date_from" not in profile
    assert "production_date_to" not in profile


@pytest.mark.parametrize("boundary", ["not a date", "201813", "2018-02-31", True])
def test_partsapi_get_cars_invalid_production_boundary_retains_diagnostic(boundary):
    profile = extract_partsapi_vehicle_profiles(
        operation="getCars", payload=[{"carId": 1, "yearOfConstrFrom": boundary}]
    )[0]
    assert "production_date_from" not in profile
    assert profile["production_boundaries"]["production_date_from"]["date"] is None
    assert profile["production_boundaries"]["production_date_from"]["raw_value"] == boundary


@pytest.mark.parametrize(
    "wrap",
    [lambda rows: rows, lambda rows: {"data": {"array": rows}}, lambda rows: {"result": {"0": rows[0], "1": rows[1]}}],
)
def test_partsapi_get_cars_keeps_all_normalized_modifications(partsapi_vin_env, monkeypatch, wrap):
    rows = [
        {
            "carId": "123",
            "makeId": "121",
            "modelId": "456",
            "manuName": "Volkswagen",
            "modelName": "Touareg",
            "typeName": "3.0 V6 TDI",
            "cylinderCapacityCcm": "2967.000",
            "powerHpFrom": "286",
            "powerHpTo": "286",
            "powerKwFrom": "210.0",
            "powerKwTo": 210,
            "engineType": "Diesel",
            "fuelType": "Diesel",
            "motorCodes": "DENA",
            "driveType": "AWD",
            "kp": "8AT",
            "yearOfConstrFrom": "Tue, 01 May 2018 00:00:00 GMT",
            "yearOfConstrTo": "",
        },
        {"id": "124", "name": "3.0 V6 TDI", "capacity": "2967", "kw": "170,5", "hp": "231"},
    ]
    monkeypatch.setattr(catalog_clients_module, "urlopen", lambda *_a, **_k: _FakeResponse(wrap(rows)))
    result = partsapi_catalog_lookup(
        operation="getCars", provider_parameters={"carType": "PC", "makeId": 121, "modelId": 456}
    )
    assert result["outcome"] == "success"
    assert result["record_counts"]["vehicle_profiles"] == 2
    first, second = result["vehicle_profiles"]
    assert first["tecdoc_car_id"] == 123
    assert first["make_id"] == 121
    assert first["model_id"] == 456
    assert first["make"] == "Volkswagen"
    assert first["model"] == "Touareg"
    assert first["modification"] == "3.0 V6 TDI"
    assert first["displacement_cc"] == 2967
    assert first["power_hp_from"] == first["power_hp_to"] == 286
    assert first["power_kw_from"] == first["power_kw_to"] == 210
    assert first["engine_type"] == "Diesel"
    assert first["engine_code"] == first["engine"] == "DENA"
    assert first["transmission"] == "8AT"
    assert first["drive_type"] == "AWD"
    assert first["drivetrain"] == "AWD"
    assert second["power_kw_from"] == 170.5
    for profile in result["vehicle_profiles"]:
        assert profile["vehicle_type"] == "PC"
        assert profile["catalog_candidate_only"] is True
        assert profile["independent_vehicle_confirmation"] is False
        assert profile["fitment_confirmed"] is False
        assert profile["requires_exact_identifier_confirmation"] is True


@pytest.mark.parametrize("car_id", [None, "", False, -1, "NaN", "1.5", "unknown", {}, []])
def test_partsapi_get_cars_invalid_identifier_does_not_create_vehicle(car_id):
    assert extract_partsapi_vehicle_profiles(operation="getCars", payload=[{"carId": car_id, "typeName": "Test"}]) == []


def test_partsapi_get_cars_conflicting_identifiers_are_not_selected():
    assert (
        extract_partsapi_vehicle_profiles(operation="getCars", payload=[{"carId": 1, "TYPE_ID": 2, "typeName": "Test"}])
        == []
    )
    profile = extract_partsapi_vehicle_profiles(
        operation="getCars", payload=[{"carId": "001", "TYPE_ID": 1, "typeName": "Test"}]
    )[0]
    assert profile["tecdoc_car_id"] == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"kp": "8AT", "kpp": "6MT"},
        {"transmission": "8AT", "kp": "6AT"},
        {"transmission": "Automatic", "kp": "6AT", "kpp": "8AT"},
        {"engineCode": "DENA", "motorCodes": "CRCA"},
        {"driveType": "AWD", "drive_type": "FWD"},
        {"cylinderCapacityCcm": "2967", "ccmTech": "1984"},
        {"powerHpFrom": "286", "powerHp": "245"},
        {"powerKwFrom": "210", "powerKw": "170"},
        {"makeId": 121, "manuId": 999},
        {"modelId": 456, "MOD_ID": 999},
        {"manuName": "Volkswagen", "makeName": "Porsche"},
        {"modelName": "Touareg", "model": "Golf"},
        {"carType": "PC", "CAR_TYPE": "CV"},
    ],
)
def test_partsapi_get_cars_contradictory_aliases_cannot_hide_material_facts(partsapi_vin_env, monkeypatch, fields):
    payload = [{"carId": 1, "motorCodes": "DENA", **fields}]
    assert extract_partsapi_vehicle_profiles(operation="getCars", payload=payload) == []
    monkeypatch.setattr(catalog_clients_module, "urlopen", lambda *_a, **_k: _FakeResponse(payload))
    result = partsapi_catalog_lookup(
        operation="getCars", provider_parameters={"makeId": 121, "modelId": 456, "carType": "PC"}
    )
    assert result["outcome"] == "unparsed_response"
    assert result["ok"] is False
    assert result["vehicle_profiles"] == []
    assert result["requires_fallback"] is True


def test_partsapi_get_cars_compatible_aliases_keep_transmission_speed():
    profile = extract_partsapi_vehicle_profiles(
        operation="getCars",
        payload=[
            {
                "carId": 1,
                "transmission": "Automatic",
                "kp": "8AT",
                "kpp": "8 speed automatic",
                "engineCode": "DENA",
                "motorCodes": "DENA",
                "cylinderCapacityCcm": "2967",
                "ccmTech": 2967.0,
                "powerHpFrom": "286",
                "powerHp": 286,
                "driveType": "AWD",
                "drive_type": "4WD",
            }
        ],
    )[0]
    assert profile["transmission"] == "Automatic / 8AT / 8 speed automatic"
    assert profile["engine_code"] == "DENA"
    assert profile["displacement_cc"] == 2967
    assert profile["power_hp_from"] == 286


@pytest.mark.parametrize(
    "invalid_row",
    [
        {"carId": 101, "TYPE_ID": 102, "engineCode": "CASA"},
        {"carId": 101, "engineCode": "CASA", "motorCodes": "DENA"},
        {"carId": 101, "kp": "6MT", "kpp": "8AT"},
        {"carId": 0, "engineCode": "CASA"},
        {"carId": None, "engineCode": "CASA"},
        {"carId": "", "engineCode": "CASA"},
        {"carId": "unknown", "engineCode": "CASA"},
    ],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_partsapi_get_cars_invalid_variant_cannot_create_partial_success(
    partsapi_vin_env, monkeypatch, invalid_row, reverse
):
    payload = [invalid_row, {"carId": 103, "engineCode": "CASA"}]
    if reverse:
        payload.reverse()
    assert extract_partsapi_vehicle_profiles(operation="getCars", payload=payload) == []
    monkeypatch.setattr(catalog_clients_module, "urlopen", lambda *_a, **_k: _FakeResponse(payload))
    result = partsapi_catalog_lookup(
        operation="getCars", provider_parameters={"makeId": 121, "modelId": 456, "carType": "PC"}
    )
    assert result["outcome"] == "unparsed_response"
    assert result["ok"] is False
    assert result["vehicle_profiles"] == []
    assert result["empty_payload"] is False


def test_partsapi_get_cars_parser_depth_does_not_publish_partial_result():
    nested = {"carId": 1}
    for _ in range(6):
        nested = {"data": nested}
    assert extract_partsapi_vehicle_profiles(operation="getCars", payload=[{"carId": 2}, nested]) == []


@pytest.mark.parametrize("value", [True, "invalid", -1, "NaN", "", None, {}])
def test_partsapi_get_cars_invalid_numeric_characteristics_are_missing(value):
    profile = extract_partsapi_vehicle_profiles(
        operation="getCars", payload=[{"carId": 1, "cylinderCapacityCcm": value, "powerHpFrom": value}]
    )[0]
    assert "displacement_cc" not in profile
    assert "power_hp_from" not in profile


@pytest.mark.parametrize(
    ("payload", "outcome"),
    [([], "empty_result"), ({"items": []}, "empty_result"), ({"unknown": "shape"}, "unparsed_response")],
)
def test_partsapi_get_cars_empty_and_unparsed_are_distinct(partsapi_vin_env, monkeypatch, payload, outcome):
    monkeypatch.setattr(catalog_clients_module, "urlopen", lambda *_a, **_k: _FakeResponse(payload))
    result = partsapi_catalog_lookup(
        operation="getCars", provider_parameters={"carType": "PC", "makeId": 1, "modelId": 2}
    )
    assert result["outcome"] == outcome
    assert result["vehicle_profiles"] == []
    assert result["requires_fallback"] is True


def test_partsapi_get_cars_provider_failure_is_not_empty(partsapi_vin_env, monkeypatch):
    def fail(*_args, **_kwargs):
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr(catalog_clients_module, "urlopen", fail)
    result = partsapi_catalog_lookup(
        operation="getCars", provider_parameters={"carType": "PC", "makeId": 1, "modelId": 2}
    )
    assert result["ok"] is False
    assert result["failure_class"] == "timeout"
    assert result["empty_payload"] is False
    assert result["requires_fallback"] is True


@pytest.mark.parametrize("year_first", [False, True])
def test_partsapi_oe_engine_market_and_year_survive_normalization(year_first):
    attributes = [
        {"key": "date", "value": "01.02.2020"},
        {"key": "manufactured", "value": "2021"},
        {"key": "engine", "value": "TEST-ENGINE"},
        {"key": "engine_info", "value": "2000CC / 200hp"},
        {"key": "market", "value": "TEST-MARKET"},
        {"key": "prodrange", "value": "2019 - 2022"},
    ]
    if year_first:
        attributes.reverse()
    result = extract_partsapi_vehicle_profiles(
        operation="vin_decode_oe",
        payload={
            "brand": "TEST",
            "name": "TEST MODEL",
            "commonAttributes": attributes,
            "modifications": [{"engine": "OTHER", "market": "OTHER"}],
        },
    )[0]
    assert result["engine"] == "TEST-ENGINE"
    assert result["market"] == "TEST-MARKET"
    assert result["production_date"] == "01.02.2020"
    assert result["catalog_year"] == "2021"
    assert result["production_period"] == "2019 - 2022"
    assert result["engine_description"] == "2000CC / 200hp"


def test_partsapi_engine_list_is_a_successful_profile(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    _configure_partsapi_test_keys(monkeypatch)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    monkeypatch.setattr(
        catalog_clients_module,
        "urlopen",
        lambda *a, **kw: _FakeResponse(
            [
                {
                    "ENGINE_CODE": "TEST-ENGINE",
                    "MANUFACTURER": "TEST",
                    "TYPE_ID": 123,
                    "ENG_CAPACITY_CCM": "2000.000",
                    "ENG_NUMBER_OF_CYLINDERS": 4,
                    "ENG_NUMBER_OF_VALVES": 16,
                    "ENG_POWER_KW_START": "150.000",
                    "ENG_TORQUE_NM_START": "300.000",
                    "ENGINE_MANAGEMENT": "Test timing drive",
                },
            ]
        ),
    )
    result = partsapi_catalog_lookup(operation="engine_info", type_id="123")
    assert result["outcome"] == "success"
    profile = result["vehicle_profiles"][0]
    assert profile["engine"] == profile["engine_code"] == "TEST-ENGINE"
    assert profile["tecdoc_car_id"] == 123
    assert profile["displacement_cc"] == 2000
    assert profile["cylinders"] == 4
    assert profile["valves"] == 16
    assert profile["power_kw_from"] == 150
    assert profile["torque_nm_from"] == 300
    assert profile["timing_drive"] == "Test timing drive"


@pytest.mark.parametrize("code", [401, 403])
def test_partsapi_auth_failure_does_not_retry_and_names_oe_fallback(monkeypatch, code):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.setenv("PARTSAPI_GET_ENGINE_KEY", "private-method-key")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    calls = []

    def fail(request, **kwargs):
        calls.append(request)
        raise HTTPError(request.full_url, code, "private-method-key", hdrs=None, fp=None)

    monkeypatch.setattr(catalog_clients_module, "urlopen", fail)
    monkeypatch.setattr(catalog_clients_module.time, "sleep", lambda _: pytest.fail("must not retry auth"))
    result = partsapi_catalog_lookup(operation="engine_info", type_id="123", max_attempts=3)
    assert len(calls) == 1
    assert result["failure_class"] == "provider_auth_error"
    assert result["retryable"] is False
    assert result["empty_payload"] is False
    assert result["fallback_operation"] == "vin_decode"
    assert result["fallback_requires_identifier"] is True
    assert "private-method-key" not in json.dumps(result)


@pytest.mark.parametrize("via_override", [False, True])
def test_partsapi_norms_models_canonicalizes_make_code(monkeypatch, via_override):
    _clear_partsapi_method_env(monkeypatch)
    _configure_partsapi_test_keys(monkeypatch)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    kwargs = {"provider_parameters": {"makeNameSEO": " audi "}} if via_override else {"make_name_seo": " audi "}
    result = partsapi_catalog_lookup(operation="norms_models", dry_run=True, **kwargs)
    assert result["request_plan"]["params"] == {"makeNameSEO": "AUDI"}


@pytest.mark.parametrize("recovers", [False, True])
def test_partsapi_norms_retry_once_with_delay_and_honest_failure(monkeypatch, recovers):
    _clear_partsapi_method_env(monkeypatch)
    _configure_partsapi_test_keys(monkeypatch)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")
    calls, delays = [], []

    def respond(request, **kwargs):
        calls.append(request)
        if recovers and len(calls) == 2:
            return _FakeResponse([{"makeName": "TEST", "model": "TEST MODEL", "modelId": 1}])
        raise HTTPError(request.full_url, 503, "unavailable", hdrs=None, fp=None)

    monkeypatch.setattr(catalog_clients_module, "urlopen", respond)
    monkeypatch.setattr(catalog_clients_module.time, "sleep", delays.append)
    result = partsapi_catalog_lookup(operation="norms_models", make_name_seo="TEST", max_attempts=10)
    assert len(calls) == result["attempt_count"] == result["max_attempts"] == 2
    assert delays == [0.25]
    assert result["outcome"] == ("success" if recovers else "provider_http_5xx")
    if not recovers:
        assert result["empty_payload"] is False
        assert result["requires_fallback"] is True
        assert "временно недоступен" in result["status_message"]


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), "invalid", None, 0, 1000])
def test_restored_provider_timeout_limits_are_finite(timeout):
    from autostop_manager.catalog_clients import _clamp_timeout

    bounded = _clamp_timeout(timeout)
    assert 1.0 <= bounded <= 30.0


def test_restored_provider_reader_rejects_oversized_responses():
    from autostop_manager.catalog_clients import MAX_PROVIDER_RESPONSE_BYTES, _read_response_bytes

    with pytest.raises(ValueError, match="safety limit"):
        _read_response_bytes(BytesIO(b"x" * (MAX_PROVIDER_RESPONSE_BYTES + 1)))


def test_restored_provider_reader_rejects_non_bytes():
    from autostop_manager.catalog_clients import _read_response_bytes

    class Response:
        def read(self, _size):
            return "text"

    with pytest.raises(ValueError, match="not bytes"):
        _read_response_bytes(Response())


def test_restored_provider_opener_denies_redirects_without_network():
    from autostop_manager.catalog_clients import _NoRedirectHandler

    assert _NoRedirectHandler().redirect_request(None, None, 302, "redirect", {}, "https://other.test/") is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://provider.test/api",
        "https://user:password@provider.test/api",
        "https://provider.test/api#fragment",
        "not-a-url",
    ],
)
def test_restored_partsapi_endpoint_rejects_insecure_credentials_destinations(monkeypatch, endpoint):
    _clear_partsapi_method_env(monkeypatch)
    monkeypatch.setenv("PARTSAPI_KEY", "fixture-secret")
    monkeypatch.setenv("PARTSAPI_BASE_URL", endpoint)
    plan = build_partsapi_request(method="VINdecode", params={"vin": "SYNTHETICVIN00001"})
    assert plan["ok"] is False
    assert plan["configured"] is False
    assert plan["url"] is None
    assert "PARTSAPI_BASE_URL" in plan["missing_env_names"]


def test_restored_partsapi_total_attempt_budget_is_bounded(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    _configure_partsapi_test_keys(monkeypatch)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://provider.example.test/api")
    timeouts = []

    def fail(request, timeout):
        timeouts.append(timeout)
        raise TimeoutError("fixture timeout")

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fail)
    monkeypatch.setattr("autostop_manager.catalog_clients.time.sleep", lambda delay: None)
    result = partsapi_catalog_lookup(
        operation="vin_decode", identifier="SYNTHETICVIN00001", timeout=1000, max_attempts=3
    )
    assert result["attempt_count"] == 3
    assert sum(timeouts) <= 60.0


@pytest.fixture
def supplier_lookup(monkeypatch):
    _clear_partsapi_method_env(monkeypatch)
    _configure_partsapi_test_keys(monkeypatch)
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    def run(operation, rows, **kwargs):
        monkeypatch.setattr(catalog_clients_module, "urlopen", lambda *_args, **_kwargs: _FakeResponse(rows))
        return partsapi_catalog_lookup(operation=operation, **kwargs)

    return run


def _supplier_article_row(**fields):
    return {
        "ART_ID": 123,
        "ART_ARTICLE_NR": "TEST-100",
        "ART_SUP_BRAND": "TEST BRAND",
        "ART_PRODUCT_NAME": "Brake disc",
        "FOUND_VIA": "IAMNumber",
        **fields,
    }


def test_partsapi_supplier_search_contract_exposes_supported_id_followup(supplier_lookup):
    result = supplier_lookup("search_articles", [_supplier_article_row()], part_number="TEST-100")
    candidate = result["article_candidates"][0]
    assert candidate["supplier_id"] is None and candidate["supplier_id_source"] is None
    assert candidate["get_article_ready"] is False
    assert candidate["missing_for_get_article"] == ["supplier_id"]
    assert candidate["supplier_id_resolution"] == "linked_getArticles"
    assert candidate["article_id_operations"] == ["article_criteria", "getArticleMedia", "article_crosses"]
    assert result["completeness"] == "complete" and result["completeness_scope"] == "catalog_response"
    assert result["missing_fields"] == []
    followup = partsapi_catalog_lookup(
        operation="article_criteria", article_id=str(candidate["article_id"]), dry_run=True
    )
    assert followup["ok"] is True and followup["attempt_count"] == 0
    assert followup["request_plan"]["params"] == {"ART_ID": "123", "LANG": 16}
    assert candidate["fitment_evidence"]["fitment_confirmed"] is False


def test_partsapi_supplier_get_articles_keeps_documented_id(supplier_lookup):
    result = supplier_lookup(
        "articles",
        [_supplier_article_row(SUP_ID="7", PRODUCT_GROUP="Brake disc", PT_ID=8)],
        type_id="42",
        category="18",
    )
    candidate = result["article_candidates"][0]
    assert candidate["supplier_id"] == 7 and candidate["supplier_id_source"] == "provider_response"
    assert candidate["get_article_ready"] is True and candidate["missing_for_get_article"] == []
    assert result["completeness"] == "complete"
    assert result["catalog_binding"]["fitment_confirmed"] is False


def test_partsapi_supplier_article_inherits_only_matching_request_parameters(supplier_lookup):
    result = supplier_lookup(
        "article",
        [_supplier_article_row(ARTICLE_CRITERIA="Diameter [mm]: 300", OEM_NUMBERS="TEST OEM: A123")],
        provider_parameters={"ART_NUM": "test 100", "SUP_ID": 42, "LANG": 16},
        brand="TEST BRAND",
    )
    candidate = result["article_candidates"][0]
    assert candidate["supplier_id"] == 42 and candidate["supplier_id_source"] == "request_parameters"
    assert candidate["get_article_ready"] is True
    assert result["completeness"] == "complete" and result["missing_fields"] == []
    assert "SUP_ID" not in candidate["raw_keys"]
    assert result["oem_candidates"] == []
    assert candidate["oe_references"][0]["fitment_confirmed"] is False


@pytest.mark.parametrize("fields", [{"ART_ARTICLE_NR": "FOREIGN-200"}, {"ART_SUP_BRAND": "FOREIGN BRAND"}])
def test_partsapi_supplier_article_mismatched_return_never_inherits_id(supplier_lookup, fields):
    result = supplier_lookup(
        "article", [_supplier_article_row(**fields)], part_number="TEST-100", supplier_id=42, brand="TEST BRAND"
    )
    candidate = result["article_candidates"][0]
    assert candidate["supplier_id"] is None and candidate["supplier_id_source"] is None
    assert candidate["get_article_ready"] is False
    assert result["completeness"] == "partial"
    assert candidate["missing_for_get_article"] == ["supplier_id"]


def test_partsapi_supplier_article_ambiguous_brand_return_does_not_inherit_id(supplier_lookup):
    result = supplier_lookup(
        "article",
        [_supplier_article_row(), _supplier_article_row(ART_ID=124, ART_SUP_BRAND="OTHER BRAND")],
        part_number="TEST-100",
        supplier_id=42,
    )
    assert result["record_counts"]["article_candidates"] == 2
    assert all(candidate["supplier_id"] is None for candidate in result["article_candidates"])


@pytest.mark.parametrize("supplier", [True, 0, -1, "unknown", "1.5"])
def test_partsapi_supplier_invalid_response_id_does_not_enable_get_article(supplier):
    candidate = extract_partsapi_article_candidates(payload=[_supplier_article_row(SUP_ID=supplier)])[0]
    assert candidate["supplier_id"] is None and candidate["get_article_ready"] is False


def test_partsapi_supplier_article_missing_details_are_partial(supplier_lookup):
    result = supplier_lookup("article", [_supplier_article_row()], part_number="TEST-100", supplier_id=42)
    assert result["completeness"] == "partial"
    assert result["missing_fields"] == ["criteria", "oe_references"]
    assert result["outcome"] == "success" and result["record_counts"]["article_candidates"] == 1


def test_partsapi_supplier_article_explicit_empty_details_are_known_response(supplier_lookup):
    row = _supplier_article_row(ARTICLE_CRITERIA=None, OEM_NUMBERS=None)
    row.pop("ART_PRODUCT_NAME")  # Fresh getArticle omits this optional field.
    result = supplier_lookup("article", [row], part_number="TEST-100", supplier_id=42)
    assert result["completeness"] == "complete" and result["missing_fields"] == []
    assert result["article_candidates"][0]["oe_references"] == []
    assert result["article_candidates"][0]["fitment_evidence"]["fitment_confirmed"] is False


@pytest.mark.parametrize("partial", [False, True])
def test_partsapi_supplier_get_cars_completeness_is_catalog_only(supplier_lookup, partial):
    row = {
        "CAR_ID": 123,
        "MAKE_NAME": "TEST",
        "MODEL_NAME": "TEST SUV",
        "CAR_NAME": "TEST 2.0",
        "CAR_TYPES": "PC",
        "CAPACITY": "1998/2.0 l",
        "POWER_KW": "150.0000",
        "POWER_PS": "204.0000",
        "ENGINE_TYPE": "Petrol Engine",
        "YEAR_START": "Sat, 01 Dec 2007 00:00:00 GMT",
        "YEAR_END": "",
    }
    if partial:
        row["CAPACITY"] = "unknown"
    result = supplier_lookup("getCars", [row], provider_parameters={"carType": "PC", "makeId": 42, "modelId": 43})
    assert result["completeness"] == ("partial" if partial else "complete")
    assert result["completeness_scope"] == "catalog_response"
    assert result["missing_fields"] == (["displacement_cc"] if partial else [])
    assert result["vehicle_profiles"][0]["fitment_confirmed"] is False
    assert result["vehicle_profiles"][0]["independent_vehicle_confirmation"] is False


def test_partsapi_supplier_partial_search_retains_counts_and_guards(supplier_lookup):
    result = supplier_lookup("search_articles", [{"ART_ID": 123, "ART_ARTICLE_NR": "TEST-100"}], part_number="TEST-100")
    assert result["completeness"] == "partial"
    assert result["missing_fields"] == ["brand", "found_via", "product_name"]
    assert result["record_counts"]["article_candidates"] == 1
    assert result["article_candidates"][0]["fitment_evidence"]["fitment_confirmed"] is False


@pytest.mark.parametrize("wrapper", ["flat", "results"])
@pytest.mark.parametrize("marked", [False, True])
def test_partsapi_us_spaced_diagnostics_preserve_partial_binding_and_origin(supplier_lookup, wrapper, marked):
    row = {
        "VIN": "A" * 17,
        "Make": "TEST",
        "Model": "TEST MODEL",
        "Error Code": "1,7;400",
        "ErrorCode": "1,7;400",
        "Error Text": "Incomplete synthetic decode",
        "ErrorText": "Incomplete synthetic decode",
    }
    if marked:
        row["source"] = "NHTSA vPIC"
    payload = {"Results": [row]} if wrapper == "results" else row
    before = json.dumps(payload, sort_keys=True)
    result = supplier_lookup("decodeVINus", payload, identifier="A" * 17)
    assert result["semantic_status"] == "partial"
    assert result["provider_diagnostics"]["error_codes"] == ["1", "7", "400"]
    assert result["provider_diagnostics"]["has_error_text"] is True
    assert result["identifier_matches_request"] is True
    assert result["provenance"]["upstream"] == ("nhtsa_vpic" if marked or wrapper == "results" else "unknown")
    assert result["vehicle_profiles"][0]["fitment_confirmed"] is False
    assert result["requires_fallback"] is True
    assert json.dumps(payload, sort_keys=True) == before


@pytest.mark.parametrize("wrapper", ["flat", "results"])
@pytest.mark.parametrize("echo", [None, "A" * 17])
def test_partsapi_us_diagnostics_only_do_not_create_vehicle_identity(supplier_lookup, wrapper, echo):
    row = {"Error Code": "1,7,400", "Error Text": "No vehicle facts", "source": "NHTSA vPIC"}
    if echo:
        row["VIN"] = echo
    payload = {"Results": [row]} if wrapper == "results" else row
    result = supplier_lookup("decodeVINus", payload, identifier="A" * 17)
    assert result["semantic_status"] == "missing"
    assert result["outcome"] == "empty_result"
    assert result["vehicle_profiles"] == []
    assert result["provider_diagnostics"]["error_codes"] == ["1", "7", "400"]
    assert result["identifier_matches_request"] is (True if echo else None)
    assert result["requires_fallback"] is True


@pytest.mark.parametrize("wrapper", ["flat", "results"])
@pytest.mark.parametrize(
    "fields",
    [
        {"Error Code": "0", "ErrorCode": "1"},
        {"Error Text": "first", "ErrorText": "second"},
        {"Error Code": True},
        {"Error Code": ["1", "7"]},
        {"Error Text": {"text": "SENSITIVE_SENTINEL"}},
    ],
)
def test_partsapi_us_conflicting_or_malformed_diagnostics_fail_closed(supplier_lookup, wrapper, fields):
    row = {"VIN": "A" * 17, "Make": "TEST", **fields}
    payload = {"Results": [row]} if wrapper == "results" else row
    result = supplier_lookup("decodeVINus", payload, identifier="A" * 17)
    assert result["outcome"] == "unparsed_response"
    assert result["vehicle_profiles"] == []
    assert result["provider_diagnostics"]["error_codes"] == []
    assert result["provenance"]["upstream"] == "unknown"


@pytest.mark.parametrize("code", ["0", "1,7,400,SENSITIVE_SENTINEL", "9" * 4097])
def test_partsapi_us_diagnostic_codes_are_bounded_and_zero_text_is_not_an_error(supplier_lookup, code):
    row = {
        "VIN": "A" * 17,
        "Make": "TEST",
        "Model": "TEST MODEL",
        "ModelYear": "2020",
        "EngineModel": "TEST ENGINE",
        "Trim": "TEST TRIM",
        "Error Code": code,
        "Error Text": "0 - explanatory text",
        "source": "NHTSA vPIC",
    }
    result = supplier_lookup("decodeVINus", row, identifier="A" * 17)
    diagnostics = result["provider_diagnostics"]
    assert diagnostics["error_codes"] == (["1", "7", "400"] if code.startswith("1,") else [])
    assert "SENSITIVE_SENTINEL" not in json.dumps(diagnostics)
    assert diagnostics["has_errors"] is (code != "0")
    assert diagnostics["has_error_text"] is True
    assert result["semantic_status"] == ("complete" if code == "0" else "partial")


@pytest.mark.parametrize("outer_foreign", [False, True])
def test_partsapi_us_spaced_diagnostics_cannot_hide_foreign_vin(supplier_lookup, outer_foreign):
    row = {"VIN": "A" * 17 if outer_foreign else "B" * 17, "Make": "TEST", "Error Code": "0", "Error Text": "OK"}
    payload = {"Results": [row], **({"VIN": "B" * 17} if outer_foreign else {})}
    result = supplier_lookup("decodeVINus", payload, identifier="A" * 17)
    assert result["outcome"] == "identifier_mismatch"
    assert result["identifier_matches_request"] is False
    assert result["vehicle_profiles"] == []
