from __future__ import annotations

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
