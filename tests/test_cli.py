from __future__ import annotations

from autostop_manager import cli


def test_cli_parser_has_core_commands():
    parser = cli.build_parser()

    args = parser.parse_args(["remember", "test note", "--kind", "fact"])
    assert args.command == "remember"
    assert args.kind == "fact"

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
