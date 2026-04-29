from __future__ import annotations

from autostop_manager import cli


def test_cli_parser_has_core_commands():
    parser = cli.build_parser()

    args = parser.parse_args(["remember", "test note", "--kind", "fact"])
    assert args.command == "remember"
    assert args.kind == "fact"

    args = parser.parse_args(["today"])
    assert args.command == "today"
