from __future__ import annotations

from copy import deepcopy
import json

import pytest

from autostop_manager import automotive_identity, vin_lookup


OFFICIAL_SHAPE = {
    "Count": 1,
    "Message": "Results returned successfully",
    "SearchCriteria": "WMI:1FD",
    "Results": [{"ManufacturerName": "FORD MOTOR COMPANY", "Make": "FORD", "VehicleType": "Incomplete Vehicle"}],
}


def native_fixture(monkeypatch, payload, wmi="1FD"):
    calls = []

    def read(url, *, timeout):
        calls.append((url, timeout))
        return deepcopy(payload)

    monkeypatch.setattr(vin_lookup, "_vpic_request_json", read)
    row = automotive_identity.decode_wmi_vpic(wmi)
    assert len(calls) == row["execution"]["network_calls"] == len(row["execution"]["attempts"]) == 1
    assert "/DecodeWMI/" in calls[0][0]
    assert row["data"]["input_binding"] is None
    assert set(row["data"]["vehicle_profile"]) == {"manufacturer", "make", "country", "vehicle_type"}
    assert row["evidence"][0]["identifier_binding"] == row["data"]["identifier_binding"]
    return row


def test_official_decode_wmi_shape_binds_exact_search_criteria_without_row_echo(monkeypatch):
    row = native_fixture(monkeypatch, OFFICIAL_SHAPE)
    assert row["outcome"] == "success"
    assert row["data"]["identifier_binding"] == {"status": "exact", "verified": True}
    assert row["data"]["vehicle_profile"]["manufacturer"] == "FORD MOTOR COMPANY"
    assert row["data"]["vehicle_profile"]["make"] == "FORD"
    assert row["data"]["vehicle_profile"]["country"] is None


@pytest.mark.parametrize("wmi", ["AAA", "1G9123"])
def test_single_search_criteria_accepts_exact_three_or_six_character_wmi(monkeypatch, wmi):
    payload = deepcopy(OFFICIAL_SHAPE)
    payload["SearchCriteria"] = "WMI:" + wmi
    row = native_fixture(monkeypatch, payload, wmi)
    assert row["outcome"] == "success"
    assert row["data"]["identifier_binding"]["verified"] is True


@pytest.mark.parametrize(
    "criteria",
    [
        None,
        False,
        123,
        [],
        {},
        "",
        "1FD",
        "VIN:1FD",
        "WMI:1FD;WMI:1FD",
        "WMI:1FD,1FD",
        "WMI:1FD\nWMI:1FD",
        "WMI:1FD more",
    ],
)
@pytest.mark.parametrize("with_row_echo", [False, True])
def test_malformed_or_multiple_search_criteria_cannot_promote_manufacturer(monkeypatch, criteria, with_row_echo):
    payload = deepcopy(OFFICIAL_SHAPE)
    payload["SearchCriteria"] = criteria
    if with_row_echo:
        payload["Results"][0]["WMI"] = "1FD"
    row = native_fixture(monkeypatch, payload)
    assert row["outcome"] == "partial"
    assert row["data"]["identifier_binding"]["verified"] is False
    assert all(value is None for value in row["data"]["vehicle_profile"].values())


@pytest.mark.parametrize("echo,criteria", [("1FD", "WMI:1HG"), ("1HG", "WMI:1FD"), ("1HG", "WMI:1HG")])
def test_any_row_or_criteria_mismatch_blocks_other_matching_echo(monkeypatch, echo, criteria):
    payload = deepcopy(OFFICIAL_SHAPE)
    payload["Results"][0]["WMI"] = echo
    payload["SearchCriteria"] = criteria
    row = native_fixture(monkeypatch, payload)
    assert row["outcome"] == "partial"
    assert row["data"]["identifier_binding"] == {"status": "mismatch", "verified": False}
    assert row["data"]["vehicle_profile"]["manufacturer"] is None


def test_wrong_criteria_without_row_echo_does_not_confirm_identity(monkeypatch):
    payload = deepcopy(OFFICIAL_SHAPE)
    payload["SearchCriteria"] = "WMI:1HG"
    row = native_fixture(monkeypatch, payload)
    assert row["data"]["identifier_binding"] == {"status": "mismatch", "verified": False}
    assert row["data"]["vehicle_profile"]["manufacturer"] is None


@pytest.mark.parametrize("echo", [None, "", False, 123, [], {}])
def test_present_invalid_row_echo_is_not_overridden_by_matching_criteria(monkeypatch, echo):
    payload = deepcopy(OFFICIAL_SHAPE)
    payload["Results"][0]["WMI"] = echo
    row = native_fixture(monkeypatch, payload)
    assert row["outcome"] == "partial"
    assert row["data"]["identifier_binding"]["verified"] is False
    assert row["data"]["vehicle_profile"]["manufacturer"] is None


def test_both_echoes_missing_remains_fail_closed(monkeypatch):
    payload = deepcopy(OFFICIAL_SHAPE)
    payload.pop("SearchCriteria")
    row = native_fixture(monkeypatch, payload)
    assert row["outcome"] == "partial"
    assert row["data"]["identifier_binding"] == {"status": "missing", "verified": False}
    assert row["data"]["vehicle_profile"]["manufacturer"] is None


def test_exact_row_echo_path_and_two_consistent_echoes_remain_compatible(monkeypatch):
    for criteria in (None, "WMI:1FD"):
        payload = deepcopy(OFFICIAL_SHAPE)
        payload["Results"][0]["WMI"] = "1FD"
        if criteria is None:
            payload.pop("SearchCriteria")
        row = native_fixture(monkeypatch, payload)
        assert row["outcome"] == "success"
        assert row["data"]["identifier_binding"] == {"status": "exact", "verified": True}


def test_search_criteria_never_binds_multiple_provider_rows(monkeypatch):
    payload = deepcopy(OFFICIAL_SHAPE)
    payload["Results"] *= 2
    row = native_fixture(monkeypatch, payload)
    assert row["outcome"] == "parse_error"
    assert row["data"]["vehicle_profile"]["manufacturer"] is None


def test_bound_wmi_model_engine_and_transmission_are_never_vehicle_facts(monkeypatch):
    payload = deepcopy(OFFICIAL_SHAPE)
    payload["Results"][0].update({"Model": "unconfirmed", "EngineModel": "unconfirmed", "Transmission": "unconfirmed"})
    row = native_fixture(monkeypatch, payload)
    assert "unconfirmed" not in json.dumps(row)
