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
