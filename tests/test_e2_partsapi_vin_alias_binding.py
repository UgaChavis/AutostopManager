"""Every supplied VINdecode alias must remain visible to identity binding."""

from io import BytesIO
import json
import socket

import pytest

from autostop_manager import catalog_clients as catalog

REQUESTED = "A" * 17
REPRESENTATIVE = "B" * 17


def vehicle(identifier=REQUESTED, **fields):
    return {
        "vin": identifier,
        "carId": "123",
        "carType": "PC",
        "manuName": "SYNTHETIC",
        "modelName": "Example",
        **fields,
    }


@pytest.fixture
def lookup(monkeypatch):
    monkeypatch.setattr(
        catalog,
        "_partsapi_credentials",
        lambda: {"base_url": "https://partsapi.example.test/api", "key_param": "key", "method_param": "method"},
    )
    monkeypatch.setattr(catalog, "_partsapi_method_key", lambda _method: ("synthetic-key", "SYNTHETIC_KEY"))

    def no_network(*_args, **_kwargs):
        pytest.fail("VIN alias regression attempted network access")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(catalog, "bounded_urlopen", no_network)

    def run(payload):
        calls = []

        def respond(_request, **_kwargs):
            calls.append(True)
            return BytesIO(json.dumps(payload).encode("utf-8"))

        monkeypatch.setattr(catalog, "urlopen", respond)
        result = catalog.partsapi_catalog_lookup(operation="vin_decode", identifier=REQUESTED, max_attempts=1)
        assert len(calls) == 1
        return result

    return run


@pytest.mark.parametrize(
    "malformed_alias",
    ["B" * 8, "AAA***********AAA", {"value": REPRESENTATIVE}, [REPRESENTATIVE], True, False, 0],
    ids=["partial", "masked", "object", "list", "true", "false", "zero"],
)
@pytest.mark.parametrize("placement", ["card", "ancestor", "descendant"])
@pytest.mark.parametrize("identifier", [REQUESTED, REPRESENTATIVE], ids=["exact", "group"])
def test_malformed_alias_cannot_establish_exact_or_group_binding(lookup, malformed_alias, placement, identifier):
    if placement == "card":
        payload = vehicle(identifier, VIN=malformed_alias)
    elif placement == "ancestor":
        payload = {"fRaMe": malformed_alias, "result": [vehicle(identifier)]}
    else:
        payload = vehicle(identifier, result={"ViN": malformed_alias})

    result = lookup(payload)

    assert result["outcome"] not in {"success", "group_match"}
    assert result.get("identifier_matches_request") is not True
    assert result.get("requires_exact_identifier_confirmation") is not False
    assert catalog.partsapi_identifier_allows_candidate_lookup(result) is False
    assert catalog.partsapi_is_group_reference(result) is False


@pytest.mark.parametrize("wrapper", ["data", "result", "array", "items"])
@pytest.mark.parametrize("identifier", [REQUESTED, REPRESENTATIVE], ids=["exact", "group"])
def test_identifier_only_descendant_cannot_hide_invalid_alias(lookup, wrapper, identifier):
    result = lookup(vehicle(identifier, **{wrapper: {"FrAmE": {"value": REPRESENTATIVE}}}))

    assert result["outcome"] not in {"success", "group_match"}
    assert catalog.partsapi_identifier_allows_candidate_lookup(result) is False


@pytest.mark.parametrize("placement", ["card", "ancestor", "descendant"])
@pytest.mark.parametrize("identifier", [REQUESTED, REPRESENTATIVE], ids=["exact", "group"])
def test_consistent_complete_aliases_keep_exact_and_group_semantics(lookup, placement, identifier):
    alias = " " + identifier.lower()[:8] + " " + identifier.lower()[8:] + " "
    if placement == "card":
        payload = vehicle(identifier, VIN=alias, fRaMe=identifier)
    elif placement == "ancestor":
        payload = {"Vin": alias, "result": [vehicle(identifier)]}
    else:
        payload = vehicle(identifier, result={"FrAmE": alias})

    result = lookup(payload)
    group = identifier == REPRESENTATIVE

    assert result["outcome"] == ("group_match" if group else "success")
    assert result["identifier_matches_request"] is (not group)
    assert result["requires_exact_identifier_confirmation"] is group
    assert catalog.partsapi_is_group_reference(result) is group
    assert catalog.partsapi_identifier_allows_candidate_lookup(result) is True
    assert result["catalog_binding"]["fitment_confirmed"] is False


@pytest.mark.parametrize("empty_alias", [None, ""])
@pytest.mark.parametrize("identifier", [REQUESTED, REPRESENTATIVE], ids=["exact", "group"])
def test_empty_alias_does_not_override_a_complete_identifier(lookup, empty_alias, identifier):
    result = lookup(vehicle(identifier, VIN=empty_alias))

    assert result["outcome"] == ("group_match" if identifier == REPRESENTATIVE else "success")
    assert catalog.partsapi_identifier_allows_candidate_lookup(result) is True
