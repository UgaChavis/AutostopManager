from __future__ import annotations

import pytest

from autostop_manager.e4_identity import decode_wmi_local, make_observation
from autostop_manager.vin_lookup import build_lookup_plan, lookup_original_parts
from autostop_manager.vin_oem_resolver import resolve_vin_oem_parts

VIN = "WVWZZZ3CZEA000000"


def test_empty_observations_disable_all_hidden_identity_sources(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("E5 silently decoded identity")

    monkeypatch.setattr("autostop_manager.vin_lookup.decode_vin_vpic", fail)
    monkeypatch.setattr("autostop_manager.vin_oem_resolver.decode_vehicle_identity", fail)
    lookup = lookup_original_parts(VIN, identity_observations=[], part_name="front brake pads")
    assert lookup["identity"]["vehicle_profile"] == {}
    result = resolve_vin_oem_parts(
        identifier=VIN, requested_part="front brake pads", identity_observations=[], dry_run=True
    )
    assert result["identity"]["vehicle_profile"] == {}


def test_provided_observations_do_not_disable_separately_authorized_catalog_calls(monkeypatch):
    calls = []

    def catalog(**kwargs):
        calls.append(kwargs)
        return {"ok": False, "operation": kwargs["operation"], "outcome": "empty_result", "attempt_count": 1}

    monkeypatch.setattr("autostop_manager.vin_oem_resolver.partsapi_catalog_lookup", catalog)
    resolve_vin_oem_parts(
        identifier=VIN, requested_part="front brake pads", identity_observations=[], live_partsapi_identity=True
    )
    assert calls and calls[0]["operation"] == "vin_decode"
    assert calls[0]["dry_run"] is False


def test_foreign_observation_does_not_reach_catalog(monkeypatch):
    def fail(**kwargs):
        pytest.fail("Mismatched observation reached catalogue")

    foreign = make_observation("vin_brand_details", "WAUZZZ8KZEA000000", fields={"make": "Audi"})
    monkeypatch.setattr("autostop_manager.vin_oem_resolver.partsapi_catalog_lookup", fail)
    result = resolve_vin_oem_parts(
        identifier=VIN, requested_part="front brake pads", identity_observations=[foreign], live_partsapi_identity=True
    )
    assert result["identity"]["ok"] is False


def test_vag_missing_portals_does_not_prefer_man():
    plan = build_lookup_plan(VIN, make_hint="Volkswagen", live_vpic=False)
    assert any("catalog_mapping_gap" in warning for warning in plan["warnings"])
    assert not any("MAN" in route["source_name"] for route in plan["catalog_routes"])
    assert not any("Open MAN" in action for action in plan["next_actions"])


def test_lookup_passes_wmi_observation_to_pure_reconciler(monkeypatch):
    monkeypatch.setattr("autostop_manager.vin_lookup.decode_vin_vpic", lambda *a, **k: pytest.fail("Hidden decode"))
    result = lookup_original_parts(VIN, identity_observations=[decode_wmi_local("WVW")])
    assert result["identity"]["vehicle_profile"]["make"]
    assert not result["identity"]["parts_lookup_readiness"]["ready_for_oem_lookup"]


def test_mismatched_observation_cannot_confirm_captured_part():
    foreign = make_observation("vin_brand_details", VIN, fields={"make": "Volkswagen"})
    result = lookup_original_parts(
        "WBA00000000000000",
        make_hint="BMW",
        part_name="front brake pads",
        captured_oem_number="TEST-001",
        captured_source="BMW AIR/ETK via AOS",
        identity_observations=[foreign],
    )
    assert result["ok"] is False
    assert result["oem_candidates"][0]["confidence"] == "blocked"
    assert result["fitment_confidence"]["level"] == "blocked"


def test_unverified_wmi_keeps_information_without_vehicle_evidence():
    from autostop_manager.e4_identity import reconcile_vehicle_identity, vpic_observation
    from autostop_manager.vin_lookup import _parse_wmi_payload

    parsed = _parse_wmi_payload({"Results": [{"Name": "Synthetic Manufacturer", "Country": "Japan"}]}, "WVW")
    observation = vpic_observation("WVW", parsed, wmi=True)
    assert observation["outcome"] == "identity_unverified"
    assert observation["diagnostics"]["unverified_provider_fields"]["name"] == "Synthetic Manufacturer"
    assert observation["vehicle_profile"] == {}
    dossier = reconcile_vehicle_identity(VIN, [observation])
    assert dossier["vehicle_profile"] == {}
    assert not dossier["parts_lookup_readiness"]["ready_for_vehicle_lookup"]
