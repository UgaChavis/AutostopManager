from __future__ import annotations

from io import BytesIO
import json
from zipfile import ZIP_DEFLATED, ZipFile

import autostop_manager.public_automotive_evidence as evidence


def _recall_payload() -> bytes:
    return json.dumps(
        {
            "Count": 1,
            "results": [
                {
                    "NHTSACampaignNumber": "24V000001",
                    "Manufacturer": "Example Motors",
                    "ReportReceivedDate": "01/02/2026",
                    "Component": "ENGINE AND ENGINE COOLING",
                    "Summary": "Example summary",
                    "Consequence": "Example consequence",
                    "Remedy": "Example remedy",
                }
            ],
        }
    ).encode("utf-8")


def _tsb_zip() -> bytes:
    csv_text = "\n".join(
        [
            "NHTSA ID Number,TSB/Document ID,Mfr Communication Date,Communication Type,Make,Model,Model Year,NHTSA Components,Summary",
            "123456789,SI 12 34 56,20260102,Service Bulletin / Repair Instructions,MERCEDES-BENZ,C-CLASS,2020,ENGINE,Relevant bulletin",
            "123456789,SI 12 34 56,20260102,Service Bulletin / Repair Instructions,MERCEDES-BENZ,C-CLASS,2020,ENGINE,Duplicate component row",
            "987654321,OTHER,20260103,Service Bulletin / Repair Instructions,BMW,3 SERIES,2020,ENGINE,Other make",
        ]
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("MfrComms.csv", csv_text)
    return buffer.getvalue()


def test_lookup_nhtsa_recalls_returns_compact_official_model_evidence(monkeypatch):
    monkeypatch.setattr(evidence, "_safe_get_bytes", lambda *_args, **_kwargs: _recall_payload())

    result = evidence.lookup_nhtsa_recalls(make="Mercedes-Benz", model="C-Class", model_year=2020)

    assert result["ok"] is True
    assert result["scope"] == "US model-level"
    assert result["records"] == [
        {
            "campaign_number": "24V000001",
            "manufacturer": "Example Motors",
            "report_received_date": "01/02/2026",
            "component": "ENGINE AND ENGINE COOLING",
            "summary": "Example summary",
            "consequence": "Example consequence",
            "remedy": "Example remedy",
        }
    ]
    assert "specific VIN" in result["warnings"][0]
    assert "make=Mercedes-Benz" in result["request_url"]


def test_lookup_nhtsa_recalls_requires_vehicle_context_without_calling_network(monkeypatch):
    monkeypatch.setattr(
        evidence, "_safe_get_bytes", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network"))
    )

    result = evidence.lookup_nhtsa_recalls(make="Mercedes-Benz", model=None, model_year=2020)

    assert result["ok"] is True
    assert result["records"] == []
    assert result["missing_context"] == ["make", "model", "model_year"]


def test_lookup_tsb_metadata_filters_and_deduplicates_official_zip(monkeypatch):
    monkeypatch.setattr(evidence, "_nhtsa_tsb_dataset_url", lambda: "https://static.nhtsa.gov/test.zip")
    monkeypatch.setattr(evidence, "_safe_get_bytes", lambda *_args, **_kwargs: _tsb_zip())

    result = evidence.lookup_nhtsa_tsb_metadata(make="Mercedes-Benz", model="C-Class", model_year=2020)

    assert result["ok"] is True
    assert result["source_id"] == "nhtsa_tsbs_recent_zips"
    assert result["records"] == [
        {
            "nhtsa_id": "123456789",
            "document_id": "SI 12 34 56",
            "communication_type": "Service Bulletin / Repair Instructions",
            "communication_date": "20260102",
            "component": "ENGINE",
            "summary": "Relevant bulletin",
        }
    ]


def test_public_evidence_redacts_vin_and_does_not_claim_vin_campaign_status(monkeypatch):
    monkeypatch.setattr(evidence, "_safe_get_bytes", lambda *_args, **_kwargs: _recall_payload())

    result = evidence.lookup_public_automotive_evidence(
        vin="WDD00000000000000",
        make="Mercedes-Benz",
        model="C-Class",
        model_year=2020,
        topics=["recalls", "fluids"],
        system="automatic transmission",
    )

    assert result["input_context"]["vin"] == "WDD***000"
    assert "WDD00000000000000" not in json.dumps(result)
    assert any(item["source_id"] == "nhtsa_datasets_apis" for item in result["evidence"])
    route_evidence = next(item for item in result["evidence"] if item["source_id"] == "official_fluid_reference_routes")
    assert {row["source_id"] for row in route_evidence["records"]} == {"mercedes_operating_fluids", "ZF_TE_ML_11"}
    assert any("VIN-specific" in rule for rule in result["rules"])


def test_safe_fetch_rejects_unapproved_hosts():
    try:
        evidence._safe_get_bytes("https://example.com/private", timeout=1)
    except ValueError as exc:
        assert "allowlist" in str(exc)
    else:  # pragma: no cover - documents the safety expectation
        raise AssertionError("unapproved host was accepted")
