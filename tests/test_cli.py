from __future__ import annotations

import json

import pytest

from autostop_manager import cli


def test_cli_parser_has_core_commands():
    parser = cli.build_parser()

    args = parser.parse_args(
        [
            "integration-audit",
            "--full",
            "--gmail-proof",
            "/tmp/proof.json",
            "--output",
            "/tmp/report.json",
        ]
    )
    assert args.command == "integration-audit"
    assert args.full is True
    assert args.gmail_proof == "/tmp/proof.json"
    assert args.output == "/tmp/report.json"

    args = parser.parse_args(["remember", "test note", "--kind", "fact", "--confidence", "0.7"])
    assert args.command == "remember"
    assert args.kind == "fact"
    assert args.confidence == 0.7

    args = parser.parse_args(
        ["recall", "живой стиль", "--kind", "fact", "--category", "style", "--tags", "карточки,стиль"]
    )
    assert args.command == "recall"
    assert args.kind == "fact"
    assert args.category == "style"
    assert args.tags == "карточки,стиль"

    args = parser.parse_args(["today"])
    assert args.command == "today"

    args = parser.parse_args(
        [
            "director-journal",
            "create",
            "--event",
            "обезличенный сигнал",
            "--category",
            "operations",
            "--status",
            "open",
        ]
    )
    assert args.command == "director-journal"
    assert args.operation == "create"
    assert args.category == "operations"

    args = parser.parse_args(
        [
            "decode-vehicle",
            "MR41S123456",
            "--make",
            "Suzuki",
            "--model",
            "Hustler",
            "--model-year",
            "2018",
            "--no-live-vpic",
        ]
    )
    assert args.command == "decode-vehicle"
    assert args.identifier == "MR41S123456"
    assert args.make == "Suzuki"
    assert args.no_live_vpic is True

    args = parser.parse_args(["decode-vehicles", "--items-json", "[]", "--no-live-vpic", "--no-vpic-batch"])
    assert args.command == "decode-vehicles"
    assert args.items_json == "[]"
    assert args.no_live_vpic is True
    assert args.no_vpic_batch is True

    args = parser.parse_args(["provider-smoke", "--provider", "all", "--mode", "dry-run"])
    assert args.command == "provider-smoke"
    assert args.provider == "all"
    assert args.mode == "dry-run"

    args = parser.parse_args(
        [
            "exist-price-lookup",
            "--part-number",
            "9091901164",
            "--brand",
            "Toyota",
            "--office-id",
            "905",
            "--include-more-offers",
            "--dry-run",
        ]
    )
    assert args.command == "exist-price-lookup"
    assert args.part_number == "9091901164"
    assert args.brand == "Toyota"
    assert args.office_id == 905
    assert args.include_more_offers is True
    assert args.dry_run is True

    args = parser.parse_args(
        [
            "crm-vin-parts-plan",
            "--card-id",
            "card_123",
            "--vin",
            "JH4DA9350LS000000",
            "--part",
            "свечи",
            "--make",
            "Honda",
        ]
    )
    assert args.command == "crm-vin-parts-plan"
    assert args.card_id == "card_123"
    assert args.requested_part == "свечи"

    args = parser.parse_args(
        [
            "vin-parts-benchmark",
            "--items-json",
            "[]",
            "--part",
            "передние колодки",
            "--no-live-vpic",
            "--no-vpic-batch",
            "--skip-partsapi-dry-run",
            "--live-partsapi-identity",
            "--live-partsapi-oem",
            "--resolve-oem",
            "--max-live-calls",
            "2",
            "--max-candidates",
            "1",
        ]
    )
    assert args.command == "vin-parts-benchmark"
    assert args.items_json == "[]"
    assert args.requested_part == "передние колодки"
    assert args.no_live_vpic is True
    assert args.skip_partsapi_dry_run is True
    assert args.live_partsapi_identity is True
    assert args.live_partsapi_oem is True
    assert args.resolve_oem is True
    assert args.max_candidates == 1

    args = parser.parse_args(
        [
            "vin-parts-work-order",
            "--items-json",
            "[]",
            "--part",
            "передние колодки",
            "--no-live-vpic",
            "--no-vpic-batch",
            "--live-partsapi-identity",
            "--resolve-oem",
        ]
    )
    assert args.command == "vin-parts-work-order"
    assert args.items_json == "[]"
    assert args.requested_part == "передние колодки"
    assert args.no_live_vpic is True
    assert args.live_partsapi_identity is True
    assert args.resolve_oem is True

    args = parser.parse_args(
        [
            "maintenance-fluids",
            "--brand",
            "Toyota",
            "--unit",
            "engine_oil",
            "--year",
            "2019",
            "--model",
            "Camry",
            "--engine",
            "A25A-FKS",
            "--market",
            "Russia",
            "--service-operation",
            "oil and filter change",
            "--unit-variant",
            "A25A-FKS engine",
            "--fluid-spec",
            "Toyota 0W-16 approval",
            "--level-check-procedure",
            "warm level check",
        ]
    )
    assert args.command == "maintenance-fluids"
    assert args.unit == "engine_oil"
    assert args.engine_code == "A25A-FKS"
    assert args.service_operation == "oil and filter change"
    assert args.unit_variant == "A25A-FKS engine"
    assert args.fluid_spec == "Toyota 0W-16 approval"
    assert args.level_check_procedure == "warm level check"

    args = parser.parse_args(["control-report", "--format", "markdown", "--output", "reports/control-report.md"])
    assert args.command == "control-report"
    assert args.format == "markdown"
    assert args.output == "reports/control-report.md"

    args = parser.parse_args(["memory-review"])
    assert args.command == "memory-review"

    args = parser.parse_args(["knowledge-intake", "--path", "docs/agent/knowledge_map.json", "--dry-run"])
    assert args.command == "knowledge-intake"
    assert args.path == "docs/agent/knowledge_map.json"
    assert args.dry_run is True
    assert args.apply is False

    args = parser.parse_args(
        [
            "estimate-work",
            "--vehicle",
            "BMW X5",
            "--work",
            "замена рулевой рейки",
            "--quotes-json",
            "quotes.json",
            "--no-auto-research",
            "--labor-time-policy",
            "public_only",
        ]
    )
    assert args.command == "estimate-work"
    assert args.vehicle == "BMW X5"
    assert args.work_items == ["замена рулевой рейки"]
    assert args.quotes_json == "quotes.json"
    assert args.auto_research is False
    assert args.labor_time_policy == "public_only"

    args = parser.parse_args(
        [
            "service-labor-refresh",
            "--state-json",
            "/opt/autostopcrm/data/state.json",
            "--half-life-days",
            "90",
        ]
    )
    assert args.command == "service-labor-refresh"
    assert args.state_json == "/opt/autostopcrm/data/state.json"
    assert args.half_life_days == 90

    args = parser.parse_args(["knowledge-sync"])
    assert args.command == "knowledge-sync"

    args = parser.parse_args(["knowledge-search", "BMW F15 N63", "--domain", "bmw_f15_n63", "--limit", "5"])
    assert args.command == "knowledge-search"
    assert args.query == "BMW F15 N63"
    assert args.domain == "bmw_f15_n63"
    assert args.limit == 5

    args = parser.parse_args(["knowledge-probe", "clutch gearbox", "--limit", "3"])
    assert args.command == "knowledge-probe"
    assert args.query == "clutch gearbox"
    assert args.limit == 3

    args = parser.parse_args(["knowledge-audit"])
    assert args.command == "knowledge-audit"

    args = parser.parse_args(["cleanup-audit"])
    assert args.command == "cleanup-audit"

    args = parser.parse_args(["doctor"])
    assert args.command == "doctor"

    args = parser.parse_args(
        [
            "learn",
            "Писать живее",
            "--applies-to",
            "crm_cleanup",
            "--signal",
            "owner_correction",
            "--recommendation",
            "Одна короткая строка",
            "--avoid",
            "Длинный шаблон",
            "--importance",
            "0.8",
            "--confidence",
            "1.0",
            "--tags",
            "карточки,стиль",
        ]
    )
    assert args.command == "learn"
    assert args.applies_to == "crm_cleanup"
    assert args.importance == 0.8

    args = parser.parse_args(["lessons", "карточки", "--applies-to", "crm_cleanup", "--tags", "стиль"])
    assert args.command == "lessons"
    assert args.applies_to == "crm_cleanup"
    assert args.tags == "стиль"

    args = parser.parse_args(["agent-brief", "Приберись", "--intent", "board_cleanup", "--limit", "5"])
    assert args.command == "agent-brief"
    assert args.query == "Приберись"
    assert args.intent == "board_cleanup"
    assert args.limit == 5

    args = parser.parse_args(["skills-audit"])
    assert args.command == "skills-audit"

    command_action = next(action for action in parser._actions if action.dest == "command")
    for retired_command in {"run-start", "run-event", "run-finish", "run-list"}:
        assert retired_command not in command_action.choices


def test_service_labor_refresh_cli_writes_private_artifacts(tmp_path, capsys):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "cards": [
                    {
                        "repair_order": {
                            "status": "closed",
                            "closed_at": "25.07.2026 10:00",
                            "vehicle": "Toyota Camry",
                            "works": [
                                {
                                    "name": "Замена масла в ДВС",
                                    "quantity": "1",
                                    "price": "1500",
                                    "total": "1500",
                                }
                            ],
                        }
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "private" / "labor.json"
    executor_output = tmp_path / "private" / "restricted" / "executors.json"
    report_output = tmp_path / "private" / "reports" / "labor.md"

    exit_code = cli.main(
        [
            "service-labor-refresh",
            "--state-json",
            str(state_path),
            "--output",
            str(output),
            "--executor-output",
            str(executor_output),
            "--report-output",
            str(report_output),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["scope"]["selected_closed_orders"] == 1
    assert payload["scope"]["valid_work_rows"] == 1
    assert output.exists()
    assert executor_output.exists()
    assert report_output.exists()


def test_doctor_returns_nonzero_when_audit_fails(monkeypatch, capsys):
    monkeypatch.setattr(cli, "build_system_audit", lambda **_kwargs: {"ok": False, "warnings": ["broken"]})

    exit_code = cli.main(["doctor"])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_doctor_returns_zero_when_audit_passes(monkeypatch, capsys):
    monkeypatch.setattr(cli, "build_system_audit", lambda **_kwargs: {"ok": True, "warnings": []})

    exit_code = cli.main(["doctor"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_partsapi_vin_smoke_returns_nonzero_when_no_case_can_be_selected(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_json_value", lambda *_args, **_kwargs: {"data": {"repair_orders": []}})
    monkeypatch.setattr(
        cli,
        "select_crm_partsapi_smoke_case",
        lambda *_args, **_kwargs: {"ok": False, "error": "no_eligible_case"},
    )

    exit_code = cli.main(["partsapi-vin-smoke", "--repair-orders-json", "orders.json"])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "no_eligible_case"}


def test_every_top_level_cli_command_has_working_help(capsys):
    parser = cli.build_parser()
    command_action = next(action for action in parser._actions if action.dest == "command")

    for command in sorted(command_action.choices):
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args([command, "--help"])
        assert exc_info.value.code == 0, command

    assert "usage:" in capsys.readouterr().out
