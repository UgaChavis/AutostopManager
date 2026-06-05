from __future__ import annotations

import json

from autostop_manager.partsapi_smoke import build_partsapi_vin_smoke_report, select_crm_partsapi_smoke_case


def test_select_crm_partsapi_smoke_case_uses_recognized_part_intent():
    orders = [
        {
            "number": "1",
            "vehicle": "Mystery",
            "vin": "ABC123",
            "reason": "просто диагностика",
        },
        {
            "number": "2",
            "vehicle": "Skoda Rapid",
            "vin": "XW8AC2NH9JK106477",
            "reason": "замена стойки стабилизатора",
        },
    ]

    selected = select_crm_partsapi_smoke_case(orders)

    assert selected["ok"] is True
    assert selected["selected"]["number"] == "2"
    assert selected["selected"]["requested_part"] == "замена стойки стабилизатора"


def test_partsapi_vin_smoke_pipeline_with_numeric_category(monkeypatch):
    raw_identifier = "XW7BF4FK60S145161"
    calls: list[str] = []

    monkeypatch.setattr(
        "autostop_manager.partsapi_smoke.decode_vehicle_identity",
        lambda **kwargs: {
            "confidence": 0.9,
            "confidence_label": "high",
            "parts_lookup_readiness": {"ready_for_oem_lookup": True},
            "vehicle_profile": {"make": "Citroen", "model": "C4"},
            "warnings": [],
            "conflicts": [],
        },
    )

    def fake_partsapi_catalog_lookup(**kwargs):
        operation = kwargs["operation"]
        calls.append(operation)
        base = {
            "ok": True,
            "provider": "partsapi_ru",
            "operation": operation,
            "partsapi_method": operation,
            "request_plan": {"configured": True, "params": {}, "redacted_url": "https://api.partsapi.ru?key=***"},
            "empty_payload": False,
        }
        if operation in {"vin_decode", "vin_decode_oe"}:
            return {**base, "vehicle_profiles": [{"make": "Citroen"}]}
        if operation == "parts_by_vin":
            return {
                **base,
                "oem_candidates": [
                    {
                        "provider": "partsapi_ru",
                        "brand": "CITROEN",
                        "part_number": "5610106660",
                        "name": "Ветровое стекло",
                        "confidence": 0.95,
                        "fitment_evidence": {"is_fit_for_this_vin": True},
                    }
                ],
            }
        if operation == "search_articles":
            return {**base, "article_candidates": [{"article_id": 1, "brand": "CITROEN", "part_number": "5610106660"}]}
        if operation == "article_crosses":
            return {**base, "article_candidates": [{"article_id": 2, "brand": "AGC", "part_number": "AGC-2"}]}
        if operation == "oe_applicability":
            return {**base, "empty_payload": True}
        if operation == "crosses_title":
            return {**base, "cross_candidates": [{"brand": "AGC", "part_number": "AGC-0", "name": "Windshield"}]}
        if operation == "crosses_with_brand":
            return {**base, "cross_candidates": [{"brand": "AGC", "part_number": "AGC-1"}]}
        raise AssertionError(operation)

    monkeypatch.setattr("autostop_manager.partsapi_smoke.partsapi_catalog_lookup", fake_partsapi_catalog_lookup)

    report = build_partsapi_vin_smoke_report(
        {"vin": raw_identifier, "vehicle": "Citroen C4", "requested_part": "ветровое стекло"},
        partsapi_category="1191",
    )

    serialized = json.dumps(report, ensure_ascii=False)
    assert report["ok"] is True
    assert report["partsapi_category_resolution"]["category_kind"] == "numeric_id"
    assert report["candidate_count"] == 1
    assert report["enrichment"][0]["article_candidates"][0]["part_number"] == "5610106660"
    assert calls == [
        "vin_decode",
        "vin_decode_oe",
        "parts_by_vin",
        "search_articles",
        "article_crosses",
        "oe_applicability",
        "crosses_title",
        "crosses_with_brand",
    ]
    assert raw_identifier not in serialized
    assert "XW7***161" in serialized


def test_partsapi_vin_smoke_treats_generic_vin_decode_as_optional(monkeypatch):
    monkeypatch.setattr(
        "autostop_manager.partsapi_smoke.decode_vehicle_identity",
        lambda **kwargs: {
            "confidence": 0.8,
            "confidence_label": "medium",
            "parts_lookup_readiness": {"ready_for_oem_lookup": True},
            "vehicle_profile": {"make": "Mitsubishi", "model": "Outlander"},
            "warnings": [],
            "conflicts": [],
        },
    )

    def fake_partsapi_catalog_lookup(**kwargs):
        operation = kwargs["operation"]
        base = {
            "provider": "partsapi_ru",
            "operation": operation,
            "partsapi_method": operation,
            "request_plan": {"configured": True, "params": {}, "redacted_url": "https://api.partsapi.ru?key=***"},
        }
        if operation == "vin_decode":
            return {**base, "ok": False, "missing_env_names": ["PARTSAPI_KEY"]}
        if operation == "vin_decode_oe":
            return {**base, "ok": True, "vehicle_profiles": [{"make": "Mitsubishi"}]}
        if operation == "parts_by_vin":
            return {**base, "ok": False, "error": "The read operation timed out"}
        raise AssertionError(operation)

    monkeypatch.setattr("autostop_manager.partsapi_smoke.partsapi_catalog_lookup", fake_partsapi_catalog_lookup)

    report = build_partsapi_vin_smoke_report(
        {"vin": "Z8TXLCW6WCM902224", "vehicle": "Mitsubishi Outlander", "requested_part": "передние колодки"},
        partsapi_category="1191",
        timeout=10,
    )

    assert report["ok"] is False
    assert report["missing_env_names"] == []
    assert report["optional_missing_env_names"] == ["PARTSAPI_KEY"]
    assert report["failed_required_calls"][0]["operation"] == "parts_by_vin"


def test_partsapi_vin_smoke_reports_text_category_without_live_parts_call(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(
        "autostop_manager.partsapi_smoke.decode_vehicle_identity",
        lambda **kwargs: {
            "confidence": 0.8,
            "confidence_label": "medium",
            "parts_lookup_readiness": {"ready_for_oem_lookup": True},
            "vehicle_profile": {"make": "Skoda", "model": "Rapid"},
            "warnings": [],
            "conflicts": [],
        },
    )

    def fake_partsapi_catalog_lookup(**kwargs):
        calls.append(kwargs["operation"])
        return {
            "ok": True,
            "provider": "partsapi_ru",
            "operation": kwargs["operation"],
            "partsapi_method": kwargs["operation"],
            "request_plan": {"configured": True, "params": {}, "redacted_url": "https://api.partsapi.ru?key=***"},
            "vehicle_profiles": [],
            "oem_candidates": [],
            "cross_candidates": [],
            "article_candidates": [],
        }

    monkeypatch.setattr("autostop_manager.partsapi_smoke.partsapi_catalog_lookup", fake_partsapi_catalog_lookup)

    report = build_partsapi_vin_smoke_report(
        {"vin": "XW8AC2NH9JK106477", "vehicle": "Skoda Rapid", "requested_part": "масляный фильтр"},
        dry_run=False,
    )

    assert report["ok"] is False
    assert report["partsapi_category_resolution"]["category_kind"] == "text_candidate"
    assert report["blockers"][0]["code"] == "category_unresolved"
    assert calls == ["vin_decode", "vin_decode_oe"]
