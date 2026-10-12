from __future__ import annotations

import pytest

from autostop_manager.catalog_clients import extract_partsapi_vehicle_profiles


def _engine_profile(item):
    profiles = extract_partsapi_vehicle_profiles(
        operation="engine_info", payload=[{"ENG_NAME": "Synthetic engine", **item}]
    )
    assert len(profiles) == 1
    return profiles[0]


def test_engine_info_normalizes_numbers_keeps_ranges_and_construction_fields():
    profile = _engine_profile(
        {
            "ENG_ID": "9007199254740993.000",
            "TYPE_ID": "321.000",
            "TecDocExternalId": "654",
            "TecRmiExternalId": 987,
            "ENG_CAPACITY_CCM": "1987.000",
            "ENG_CYLINDERS": "4.000",
            "ENG_VALVES": "16.000",
            "ENG_POWER_PS_START": "185.000",
            "ENG_POWER_PS_UPTO": "412.000",
            "ENG_POWER_KW_START": "136.5",
            "ENG_POWER_KW_UPTO": "303,2",
            "ENG_TORQUE_NM_START": "250.000",
            "ENG_TORQUE_NM_UPTO": "610.000",
            "COOLING_TYPE": "Liquid",
            "CYLINDER_CONSTRUCTION": "Inline",
            "ENG_BORE": "80000.000",
            "ENG_STROKE": "99000.000",
            "ENG_COMPRESSION": "11000.000",
        }
    )
    assert profile["engine_id"] == 9007199254740993
    assert profile["tecdoc_car_id"] == 321
    assert profile["tecdoc_external_id"] == 654
    assert profile["tecrmi_external_id"] == 987
    assert profile["displacement_cc"] == 1987
    assert profile["cylinders"] == 4
    assert profile["valves"] == 16
    assert (profile["power_hp_from"], profile["power_hp_to"]) == (185, 412)
    assert (profile["power_kw_from"], profile["power_kw_to"]) == (136.5, 303.2)
    assert (profile["torque_nm_from"], profile["torque_nm_to"]) == (250, 610)
    assert profile["cooling_type"] == "Liquid"
    assert profile["cylinder_construction"] == "Inline"
    assert "numeric_field_issues" not in profile
    assert not {"power_hp", "power_kw", "torque_nm", "bore", "stroke", "compression"} & profile.keys()
    assert {"ENG_BORE", "ENG_STROKE", "ENG_COMPRESSION"} <= set(profile["raw_keys"])


@pytest.mark.parametrize(
    "invalid", [True, False, float("nan"), float("inf"), "NaN", "-Infinity", "0", "-1", "90-110", [], "1_000"]
)
def test_engine_info_rejects_invalid_numeric_values(invalid):
    profile = _engine_profile(
        {
            "ENG_ID": invalid,
            "ENG_CAPACITY_CCM": invalid,
            "ENG_POWER_PS_START": invalid,
            "ENG_POWER_KW_UPTO": invalid,
            "ENG_TORQUE_NM_START": invalid,
            "ENG_CYLINDERS": invalid,
            "ENG_VALVES": invalid,
        }
    )
    fields = {"engine_id", "displacement_cc", "power_hp_from", "power_kw_to", "torque_nm_from", "cylinders", "valves"}
    assert not fields & profile.keys()
    assert profile["numeric_field_issues"] == dict.fromkeys(fields, "invalid_numeric_value")


def test_engine_info_integer_fields_reject_fractional_numbers():
    profile = _engine_profile(
        {
            "ENG_ID": "42.5",
            "TYPE_ID": "123.1",
            "ENG_CYLINDERS": 4.5,
            "ENG_VALVES": "16.5",
            "ENG_POWER_PS_START": "185.5",
        }
    )
    assert not {"engine_id", "tecdoc_car_id", "cylinders", "valves"} & profile.keys()
    assert profile["power_hp_from"] == 185.5


def test_engine_info_invalid_or_conflicting_aliases_are_not_replaced_by_fallback():
    profile = _engine_profile(
        {
            "ENG_ID": "invalid",
            "engineId": "42",
            "cylinderCapacityCcm": "1998",
            "ENG_CAPACITY_CCM": "2000.000",
            "powerHpFrom": 185,
            "ENG_POWER_PS_START": "185.000",
        }
    )
    assert "engine_id" not in profile
    assert "displacement_cc" not in profile
    assert profile["power_hp_from"] == 185
    assert profile["numeric_field_issues"] == {
        "engine_id": "invalid_numeric_value",
        "displacement_cc": "conflicting_numeric_aliases",
    }


def test_engine_info_reversed_ranges_are_not_reported_as_exact_values():
    profile = _engine_profile(
        {
            "ENG_POWER_PS_START": "200",
            "ENG_POWER_PS_UPTO": "100",
            "ENG_TORQUE_NM_START": "600",
            "ENG_TORQUE_NM_UPTO": "250",
            "ENG_POWER_KW_START": "100",
            "ENG_POWER_KW_UPTO": "200",
        }
    )
    assert not {"power_hp_from", "power_hp_to", "torque_nm_from", "torque_nm_to"} & profile.keys()
    assert (profile["power_kw_from"], profile["power_kw_to"]) == (100, 200)
    assert set(profile["numeric_field_issues"].values()) == {"invalid_numeric_range"}


def test_engine_info_unreadable_numeric_record_does_not_become_diagnostic_only_profile():
    assert extract_partsapi_vehicle_profiles(operation="engine_info", payload=[{"ENG_ID": "invalid"}]) == []


def test_engine_numeric_normalization_does_not_change_other_operations():
    profiles = extract_partsapi_vehicle_profiles(
        operation="vin_decode_oe",
        payload={"data": {"array": {"ENG_NAME": "Synthetic engine", "ENG_ID": "42", "cylinderCapacityCcm": "1987"}}},
    )
    assert profiles[0]["engine_id"] == "42"
    assert profiles[0]["displacement_cc"] == "1987"
