from __future__ import annotations

import json

from autostop_manager.fluid_maintenance import build_fluid_maintenance_plan, normalize_unit


def test_normalize_unit_supports_russian_aliases():
    assert normalize_unit("двигатель") == "engine_oil"
    assert normalize_unit("раздатка") == "transfer_case"
    assert normalize_unit("задний редуктор") == "rear_differential"


def test_engine_oil_plan_includes_authority_and_selector_sources():
    result = build_fluid_maintenance_plan(
        brand="Toyota",
        unit="engine oil",
        year=2019,
        model="Camry",
        engine_code="A25A-FKS",
        market="Russia",
        limit=8,
    )

    assert result["ok"] is True
    assert result["unit"] == "engine_oil"
    assert "engine_code" in result["required_inputs"]
    assert result["authority_source_routes"]
    assert result["lubricant_product_selectors"]
    assert any(source["source_id"] == "liqui_moly_oil_guide" for source in result["lubricant_product_selectors"])
    assert any("without source-backed verification" in warning for warning in result["warnings"])
    assert "VIN/chassis or exact model" not in result["missing_context"]
    assert "oil/filter service type" in result["missing_context"]


def test_engine_oil_service_operation_satisfies_service_type_requirement():
    result = build_fluid_maintenance_plan(
        brand="Toyota",
        unit="engine oil",
        year=2019,
        model="Camry",
        engine_code="A25A-FKS",
        market="Russia",
        service_operation="oil and filter change",
        limit=8,
    )

    assert "VIN/chassis or exact model" not in result["missing_context"]
    assert "oil/filter service type" not in result["missing_context"]


def test_fluid_plan_redacts_raw_vin_and_chassis_from_public_output():
    raw_vin = "JTEBU3FJX05027767"
    result = build_fluid_maintenance_plan(
        brand="Toyota",
        unit="engine oil",
        vin=raw_vin,
        year=2019,
        model="Camry",
        engine_code="A25A-FKS",
        market="Russia",
        service_operation="oil and filter change",
    )

    rendered = json.dumps(result, ensure_ascii=False)
    assert raw_vin not in rendered
    assert result["privacy"]["raw_identifier_redacted_from_output"] is True
    assert result["vehicle_context"]["vin"] == "JTE***767"
    assert result["vehicle_context"]["identifier"]["redacted"]["display"] == "JTE***767"


def test_driveline_plan_marks_high_risk_context():
    result = build_fluid_maintenance_plan(
        brand="Toyota",
        unit="rear differential",
        year=2016,
        model="Land Cruiser Prado",
        drivetrain="4WD",
        market="Russia",
        limit=5,
    )

    assert result["unit"] == "rear_differential"
    assert any("High-risk driveline unit" in warning for warning in result["warnings"])
    assert any("transmission/axle/transfer-case code" in warning for warning in result["warnings"])
    assert "VIN/chassis" in result["missing_context"]
    assert "axle code" in result["missing_context"]
    assert "LSD/open differential" in result["missing_context"]


def test_driveline_unit_variant_satisfies_axle_and_lsd_requirements():
    result = build_fluid_maintenance_plan(
        brand="Toyota",
        unit="rear differential",
        chassis="GXE10-0088644",
        year=2016,
        model="Land Cruiser Prado",
        drivetrain="4WD",
        market="Russia",
        unit_variant="rear axle code A02A, open differential",
        limit=5,
    )

    rendered = json.dumps(result, ensure_ascii=False)
    assert "GXE10-0088644" not in rendered
    assert "GXE100088644" not in rendered
    assert result["vehicle_context"]["chassis"] == "GXE***644"
    assert "VIN/chassis" not in result["missing_context"]
    assert "axle code" not in result["missing_context"]
    assert "LSD/open differential" not in result["missing_context"]
