from __future__ import annotations

from autostop_manager import cli


def test_cli_parser_has_core_commands():
    parser = cli.build_parser()

    args = parser.parse_args(["remember", "test note", "--kind", "fact", "--confidence", "0.7"])
    assert args.command == "remember"
    assert args.kind == "fact"
    assert args.confidence == 0.7

    args = parser.parse_args(["recall", "живой стиль", "--kind", "fact", "--category", "style", "--tags", "карточки,стиль"])
    assert args.command == "recall"
    assert args.kind == "fact"
    assert args.category == "style"
    assert args.tags == "карточки,стиль"

    args = parser.parse_args(["today"])
    assert args.command == "today"

    args = parser.parse_args(["lookup-oem", "JH4DA9350LS000000"])
    assert args.command == "lookup-oem"
    assert args.identifier == "JH4DA9350LS000000"
    assert args.make is None

    args = parser.parse_args(
        [
            "lookup-oem",
            "WBA00000000000000",
            "--part-name",
            "рулевая рейка",
            "--side",
            "left",
            "--position",
            "front",
            "--old-part-number",
            "7852 123 456",
            "--captured-oem",
            "32 10 6 888 999",
            "--captured-source",
            "BMW AIR/ETK via AOS",
        ]
    )
    assert args.part_name == "рулевая рейка"
    assert args.side == "left"
    assert args.position == "front"
    assert args.old_part_number == "7852 123 456"
    assert args.captured_oem == "32 10 6 888 999"
    assert args.captured_source == "BMW AIR/ETK via AOS"

    args = parser.parse_args(["source-route", "--brand", "Toyota", "--data-type", "repair_manuals"])
    assert args.command == "source-route"
    assert args.brand == "Toyota"
    assert args.data_type == "repair_manuals"

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
        ]
    )
    assert args.command == "maintenance-fluids"
    assert args.unit == "engine_oil"
    assert args.engine_code == "A25A-FKS"

    args = parser.parse_args(
        [
            "service-plan",
            "--area",
            "parts",
            "--city",
            "Красноярск",
            "--vehicle",
            "Lexus RX200T",
            "--part-number",
            "90311-89014",
        ]
    )
    assert args.command == "service-plan"
    assert args.area == "parts"
    assert args.part_number == "90311-89014"

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

    args = parser.parse_args(["knowledge-sync"])
    assert args.command == "knowledge-sync"

    args = parser.parse_args(["knowledge-search", "BMW F15 N63", "--domain", "bmw_f15_n63", "--limit", "5"])
    assert args.command == "knowledge-search"
    assert args.query == "BMW F15 N63"
    assert args.domain == "bmw_f15_n63"
    assert args.limit == 5

    args = parser.parse_args(["knowledge-probe", "Toyota Yaris GR clutch", "--limit", "3"])
    assert args.command == "knowledge-probe"
    assert args.query == "Toyota Yaris GR clutch"
    assert args.limit == 3

    args = parser.parse_args(["knowledge-audit"])
    assert args.command == "knowledge-audit"

    args = parser.parse_args(["cleanup-audit"])
    assert args.command == "cleanup-audit"

    args = parser.parse_args(["system-audit"])
    assert args.command == "system-audit"

    args = parser.parse_args(["doctor"])
    assert args.command == "doctor"

    args = parser.parse_args(
        [
            "crm-health-plan",
            "--board-review-json",
            "board_review.json",
            "--today-json",
            "today_context.json",
        ]
    )
    assert args.command == "crm-health-plan"
    assert args.board_review_json == "board_review.json"
    assert args.today_json == "today_context.json"

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

    args = parser.parse_args(["memory-map"])
    assert args.command == "memory-map"

    args = parser.parse_args(["memory-topics"])
    assert args.command == "memory-topics"

    args = parser.parse_args(["memory-context", "crm карточки"])
    assert args.command == "memory-context"
    assert args.task == "crm карточки"

    args = parser.parse_args(["memory-gaps"])
    assert args.command == "memory-gaps"

    args = parser.parse_args(["annotations-audit"])
    assert args.command == "annotations-audit"

    args = parser.parse_args(["memory-audit"])
    assert args.command == "memory-audit"

    args = parser.parse_args(["memory-curate", "--apply"])
    assert args.command == "memory-curate"
    assert args.apply is True

    args = parser.parse_args(["prepare-context", "Приберись", "--intent", "board_cleanup", "--limit", "5"])
    assert args.command == "prepare-context"
    assert args.query == "Приберись"
    assert args.intent == "board_cleanup"
    assert args.limit == 5

    args = parser.parse_args(["agent-brief", "Приберись", "--intent", "board_cleanup", "--limit", "5"])
    assert args.command == "agent-brief"
    assert args.query == "Приберись"
    assert args.intent == "board_cleanup"
    assert args.limit == 5

    args = parser.parse_args(["skills-audit"])
    assert args.command == "skills-audit"

    args = parser.parse_args(["run-start", "Приберись", "--intent", "board_cleanup", "--dry-run"])
    assert args.command == "run-start"
    assert args.query == "Приберись"
    assert args.intent == "board_cleanup"
    assert args.dry_run is True

    args = parser.parse_args(["run-event", "1", "--type", "planned_action", "--message", "test"])
    assert args.command == "run-event"
    assert args.run_id == 1
    assert args.event_type == "planned_action"
    assert args.message == "test"

    args = parser.parse_args(["run-finish", "1", "--status", "completed", "--summary", "done"])
    assert args.command == "run-finish"
    assert args.run_id == 1
    assert args.status == "completed"
    assert args.summary == "done"

    args = parser.parse_args(["run-list", "--limit", "3", "--events"])
    assert args.command == "run-list"
    assert args.limit == 3
    assert args.events is True
