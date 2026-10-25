"""US VIN binding follows the selected vehicle and its actual ancestors."""

from io import BytesIO
import json
import socket

import pytest

from autostop_manager import catalog_clients as catalog

REQUESTED = "A" * 17
FOREIGN = "B" * 17


def vehicle(shape, identifier=None, **fields):
    row = {
        "Make": "SYNTHETIC",
        "Model": "Example",
        "ModelYear": "2020",
        "EngineModel": "SYNTHETIC-ENGINE",
        "Trim": "SYNTHETIC-TRIM",
        "ErrorCode": "0",
        "ErrorText": "0 - synthetic",
        **fields,
    }
    if identifier is not None:
        row["VIN"] = identifier
    if shape == "flat":
        return {**row, "source": "NHTSA vPIC"}
    if shape == "variables":
        return {"Results": [{"Variable": key, "Value": value} for key, value in row.items()]}
    return {"Results": [row]}


@pytest.fixture
def lookup(monkeypatch):
    monkeypatch.setattr(
        catalog,
        "_partsapi_credentials",
        lambda: {"base_url": "https://partsapi.example.test/api", "key_param": "key", "method_param": "method"},
    )
    monkeypatch.setattr(catalog, "_partsapi_method_key", lambda _method: ("synthetic-key", "SYNTHETIC_KEY"))

    def no_network(*_args, **_kwargs):
        pytest.fail("US record binding regression attempted network access")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(catalog, "bounded_urlopen", no_network)

    def run(payload):
        calls = []

        def respond(_request, **_kwargs):
            calls.append(True)
            return BytesIO(json.dumps(payload).encode("utf-8"))

        monkeypatch.setattr(catalog, "urlopen", respond)
        response = catalog.partsapi_catalog_lookup(operation="decodeVINus", identifier=REQUESTED, max_attempts=1)
        assert len(calls) == 1
        assert response.get("binding_kind") != "provider_group_reference"
        assert response["outcome"] != "group_match"
        assert all(profile["fitment_confirmed"] is False for profile in response["vehicle_profiles"])
        return response

    return run


@pytest.mark.parametrize("shape", ["flat", "record", "variables"])
@pytest.mark.parametrize("sibling", ["data", "result", "array", "items"])
def test_sibling_vin_cannot_confirm_selected_vehicle_without_its_own_vin(lookup, shape, sibling):
    response = lookup({**vehicle(shape), sibling: {"VIN": REQUESTED}})

    assert response["ok"] is True
    assert response["outcome"] == "partial_result"
    assert response["identifier_matches_request"] is None
    assert response["requires_exact_identifier_confirmation"] is True
    assert response["requires_fallback"] is True
    assert len(response["vehicle_profiles"]) == 1


@pytest.mark.parametrize("shape", ["flat", "record", "variables"])
@pytest.mark.parametrize("sibling", ["data", "result", "array", "items"])
def test_sibling_foreign_vin_does_not_override_selected_exact_vehicle(lookup, shape, sibling):
    response = lookup({**vehicle(shape, REQUESTED), sibling: {"VIN": FOREIGN}})

    assert response["outcome"] == "success"
    assert response["identifier_matches_request"] is True
    assert response["requires_fallback"] is False
    assert response["provenance"]["upstream"] == "nhtsa_vpic"


@pytest.mark.parametrize("echo", ["request", "params", "echo", "metadata", "requestParams", "requestParameters"])
@pytest.mark.parametrize("identifier", [None, REQUESTED], ids=["unbound", "exact"])
def test_request_or_metadata_echo_never_supplies_vehicle_binding(lookup, echo, identifier):
    echo_vin = REQUESTED if identifier is None else FOREIGN
    response = lookup({**vehicle("record", identifier), echo: vehicle("flat", echo_vin, Make="ECHO")})

    assert response["outcome"] == ("partial_result" if identifier is None else "success")
    assert response["identifier_matches_request"] is (None if identifier is None else True)
    assert response["vehicle_profiles"][0]["make"] == "SYNTHETIC"


@pytest.mark.parametrize("shape", ["flat", "record", "variables"])
@pytest.mark.parametrize("identifier", [None, REQUESTED, FOREIGN], ids=["unbound", "exact", "foreign"])
def test_selected_record_keeps_its_own_identifier_semantics(lookup, shape, identifier):
    response = lookup(vehicle(shape, identifier))
    expected = (
        "partial_result" if identifier is None else "success" if identifier == REQUESTED else "identifier_mismatch"
    )

    assert response["outcome"] == expected
    assert response["identifier_matches_request"] is (None if identifier is None else identifier == REQUESTED)
    assert response["requires_fallback"] is (identifier != REQUESTED)
    assert len(response["vehicle_profiles"]) == (0 if identifier == FOREIGN else 1)


@pytest.mark.parametrize("shape", ["flat", "record", "variables"])
@pytest.mark.parametrize("wrapper", ["data", "result", "singleton_list"])
def test_real_ancestor_vin_can_bind_a_selected_wrapped_vehicle(lookup, shape, wrapper):
    nested = vehicle(shape)
    if wrapper == "singleton_list":
        payload = {"Vin": REQUESTED, "result": [nested]}
    else:
        payload = {"Vin": REQUESTED, wrapper: nested}
    response = lookup(payload)

    assert response["outcome"] == "success"
    assert response["identifier_matches_request"] is True
    assert response["requires_fallback"] is False


@pytest.mark.parametrize("shape", ["flat", "record", "variables"])
@pytest.mark.parametrize("ancestor", ["VIN", "vIn"])
def test_conflicting_real_ancestor_vin_rejects_selected_vehicle(lookup, shape, ancestor):
    response = lookup({ancestor: FOREIGN, "data": {"VIN": REQUESTED, "result": [vehicle(shape, REQUESTED)]}})

    assert response["ok"] is False
    assert response["outcome"] == "identifier_mismatch"
    assert response["identifier_matches_request"] is False
    assert response["vehicle_profiles"] == []


@pytest.mark.parametrize("shape", ["flat", "record", "variables"])
@pytest.mark.parametrize("sibling", ["data", "result", "array", "items"])
def test_recognized_foreign_vehicle_card_still_rejects_an_exact_selected_vehicle(lookup, shape, sibling):
    response = lookup({**vehicle(shape, REQUESTED), sibling: vehicle("flat", FOREIGN, Make="SECOND")})

    assert response["ok"] is False
    assert response["outcome"] == "identifier_mismatch"
    assert response["identifier_matches_request"] is False
    assert response["vehicle_profiles"] == []


@pytest.mark.parametrize("shape", ["flat", "record", "variables"])
@pytest.mark.parametrize("sibling", ["data", "result", "array", "items"])
def test_recognized_sibling_card_cannot_supply_positive_binding(lookup, shape, sibling):
    response = lookup({**vehicle(shape), sibling: vehicle("flat", REQUESTED, Make="SECOND")})

    assert response["outcome"] == "partial_result"
    assert response["identifier_matches_request"] is None
    assert response["vehicle_profiles"][0]["make"] == "SYNTHETIC"


@pytest.mark.parametrize("shape", ["flat", "record", "variables"])
@pytest.mark.parametrize("reverse", [False, True])
def test_existing_first_wrapper_keeps_foreign_full_card_veto(lookup, shape, reverse):
    branches = [("data", vehicle(shape, REQUESTED)), ("result", vehicle(shape, FOREIGN, Make="SECOND"))]
    if reverse:
        branches.reverse()
    response = lookup(dict(branches))

    assert response["outcome"] == "identifier_mismatch"
    assert response["identifier_matches_request"] is False
    assert response["vehicle_profiles"] == []


@pytest.mark.parametrize("shape", ["flat", "record", "variables"])
def test_consistent_complete_card_duplicates_remain_accepted(lookup, shape):
    response = lookup({"data": vehicle(shape, REQUESTED), "result": vehicle(shape, REQUESTED)})

    assert response["outcome"] == "success"
    assert response["identifier_matches_request"] is True
    assert response["vehicle_profiles"][0]["make"] == "SYNTHETIC"


@pytest.mark.parametrize(
    ("shape", "levels", "singleton_list", "expected"),
    [
        ("flat", 5, False, "success"),
        ("flat", 6, False, "unparsed_response"),
        ("record", 3, False, "success"),
        ("record", 4, False, "unparsed_response"),
        ("variables", 3, False, "success"),
        ("variables", 4, False, "unparsed_response"),
        ("record", 1, True, "success"),
        ("record", 2, True, "unparsed_response"),
    ],
)
def test_selected_path_keeps_existing_depth_limit(lookup, shape, levels, singleton_list, expected):
    payload = vehicle(shape, REQUESTED)
    for _ in range(levels):
        payload = {"data": [payload] if singleton_list else payload}
    response = lookup(payload)

    assert response["outcome"] == expected
