from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path

from autostop_manager.service_labor_experience import (
    LABOR_SNAPSHOT_SCHEMA_VERSION,
    build_service_labor_experience,
    canonicalize_labor_name,
    save_service_labor_artifacts,
)
from autostop_manager.work_pricing import estimate_repair_work_cost


def _work(
    name: str,
    *,
    quantity: str = "1",
    price: str = "1000",
    total: str | None = None,
    executor_id: str = "employee-1",
    executor_name: str = "Мастер Один",
) -> dict:
    return {
        "name": name,
        "quantity": quantity,
        "price": price,
        "total": price if total is None else total,
        "work_executor_id_snapshot": executor_id,
        "work_executor_name_snapshot": executor_name,
        "work_salary_cost_price": "PRIVATE-SALARY",
    }


def _card(
    closed_at: str | None,
    *,
    status: str = "closed",
    works: list[dict] | None = None,
    vehicle: str = "Toyota Camry",
) -> dict:
    return {
        "id": f"private-order-{closed_at}",
        "vehicle": vehicle,
        "repair_order": {
            "status": status,
            "closed_at": closed_at,
            "client": "Private Client",
            "phone": "+70000000000",
            "vin": "PRIVATEVIN1234567",
            "payments": [{"amount": "1000"}],
            "works": works or [],
            "materials": [{"name": "Private part", "price": "5000"}],
        },
    }


def test_full_labor_snapshot_uses_all_closed_orders_and_excludes_materials_and_identity():
    state = {
        "cards": [
            _card("01.04.2026 10:00", works=[_work("Диагностика подвески", price="900")]),
            _card("01.05.2026 10:00", works=[_work("Диагностика ходовой", price="1000")]),
            _card(None, works=[_work("Диагностика подвески", price="1100")]),
            _card("01.06.2026 10:00", status="open", works=[_work("Диагностика подвески", price="9999")]),
        ]
    }

    snapshot, executor_report = build_service_labor_experience(
        state,
        generated_at=datetime(2026, 7, 1, tzinfo=UTC),
        source_sha256="abc123",
    )

    assert snapshot["schema_version"] == LABOR_SNAPSHOT_SCHEMA_VERSION
    assert snapshot["scope"]["selection"] == "all_closed_repair_orders"
    assert snapshot["scope"]["selected_closed_orders"] == 3
    assert snapshot["scope"]["work_rows_total"] == 3
    assert snapshot["data_quality"]["closed_orders_missing_or_invalid_closed_at"] == 1
    baseline = snapshot["labor_baselines"][0]
    assert baseline["sample_count"] == 3
    assert "observed_variants" not in baseline
    serialized = json.dumps(snapshot, ensure_ascii=False)
    assert "Private Client" not in serialized
    assert "PRIVATEVIN" not in serialized
    assert "Private part" not in serialized
    assert "Мастер Один" not in serialized
    assert "PRIVATE-SALARY" not in serialized
    assert executor_report["executors"][0]["executor_name"] == "Мастер Один"
    assert "PRIVATE-SALARY" not in json.dumps(executor_report, ensure_ascii=False)


def test_price_basis_uses_total_over_quantity_and_tracks_invalid_or_conflicting_rows():
    state = {
        "cards": [
            _card(
                "01.07.2026 10:00",
                works=[
                    _work("Замена масла в ДВС", quantity="2", price="900", total="2000"),
                    _work("Замена масла в ДВС", quantity="0", price="1000", total="1000"),
                    _work("Замена масла в ДВС", quantity="1", price="0", total="0"),
                    _work("Замена масла в ДВС", quantity="1", price="5000", total="0"),
                ],
            )
        ]
    }

    snapshot, _ = build_service_labor_experience(
        state,
        generated_at=datetime(2026, 7, 2, tzinfo=UTC),
    )

    baseline = snapshot["labor_baselines"][0]
    assert baseline["sample_count"] == 1
    assert baseline["median_rub"] == 1000
    quality = snapshot["data_quality"]
    assert quality["work_flag_price_total_mismatch"] == 1
    assert quality["work_flag_invalid_quantity"] == 1
    assert quality["work_flag_zero_or_missing_total"] == 2
    assert quality["work_rows_excluded_from_price_baseline"] == 3


def test_labor_normalization_preserves_scope_unless_an_alias_is_explicit():
    assert canonicalize_labor_name("Диагностика ходовой")["key"] == "диагностика_подвески"
    assert canonicalize_labor_name("Развал-схождение")["key"] == "развал_схождение"
    assert (
        canonicalize_labor_name("Замена передних тормозных колодок")["key"]
        != canonicalize_labor_name("Замена задних тормозных колодок")["key"]
    )
    assert (
        canonicalize_labor_name("Замена переднего левого рычага")["key"]
        != canonicalize_labor_name("Замена заднего правого рычага")["key"]
    )


def test_recency_outliers_segments_and_confidence_are_explicit():
    works = []
    dates = []
    for index, price in enumerate([1000, 1000, 1000, 1000, 1000, 2000, 2000, 2000, 2000, 100_000]):
        works.append(_work("Диагностика подвески", price=str(price)))
        dates.append(f"{index + 1:02d}.06.2026 10:00")
    state = {"cards": [_card(date, works=[work], vehicle="BMW X5") for date, work in zip(dates, works, strict=True)]}

    snapshot, _ = build_service_labor_experience(
        state,
        generated_at=datetime(2026, 7, 1, tzinfo=UTC),
    )

    baseline = snapshot["labor_baselines"][0]
    assert baseline["outlier_count"] == 1
    assert baseline["inlier_sample_count"] == 9
    assert baseline["confidence"] == "working"
    assert baseline["weighted_median_rub"] in {1000, 2000}
    assert baseline["recommended_anchor_rub"] in {1000, 2000}
    assert baseline["vehicle_segment_baselines"][0]["vehicle_segment"] == "premium"
    assert baseline["monthly_baselines"][0]["month"] == "2026-06"


def test_estimator_accepts_full_labor_snapshot_as_internal_anchor():
    state = {
        "cards": [
            _card(f"0{day}.07.2026 10:00", works=[_work("Диагностика подвески", price=price)])
            for day, price in [(1, "1000"), (2, "1000"), (3, "1200")]
        ]
    }
    snapshot, _ = build_service_labor_experience(state)

    estimate = estimate_repair_work_cost(
        vehicle="Toyota Camry",
        work_items=["диагностика ходовой"],
        auto_research=False,
        internal_experience_json=snapshot,
    )

    operation = estimate["operation_estimates"][0]
    assert operation["internal_experience"]["available"] is True
    assert operation["recommendation_basis"] == "internal_experience_provisional"
    assert estimate["pricing_basis"]["internal_experience_schema_version"] == LABOR_SNAPSHOT_SCHEMA_VERSION


def test_private_artifacts_are_atomic_restricted_and_backed_up(tmp_path: Path):
    state = {
        "cards": [
            _card(
                "01.07.2026 10:00",
                works=[_work("Диагностика подвески")],
            )
        ]
    }
    snapshot, executor_report = build_service_labor_experience(state)
    output = tmp_path / "private" / "service_labor_experience.json"
    executor_output = tmp_path / "private" / "restricted" / "executors.json"
    report_output = tmp_path / "private" / "reports" / "analysis.md"

    first = save_service_labor_artifacts(
        snapshot,
        executor_report,
        output_path=output,
        executor_output_path=executor_output,
        report_output_path=report_output,
    )
    second = save_service_labor_artifacts(
        snapshot,
        executor_report,
        output_path=output,
        executor_output_path=executor_output,
        report_output_path=report_output,
    )

    assert first["snapshot_backup_path"] is None
    assert second["snapshot_backup_path"] is not None
    assert Path(str(second["snapshot_backup_path"])).exists()
    for path in (output, executor_output, report_output):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
