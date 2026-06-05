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

    args = parser.parse_args(["catalog-status", "--stage", "oem_catalog"])
    assert args.command == "catalog-status"
    assert args.stage == "oem_catalog"

    args = parser.parse_args(["provider-smoke", "--provider", "all", "--mode", "dry-run"])
    assert args.command == "provider-smoke"
    assert args.provider == "all"
    assert args.mode == "dry-run"

    args = parser.parse_args(["oem-parts-provider-plan", "MR41S123456", "--part", "колодки"])
    assert args.command == "oem-parts-provider-plan"
    assert args.identifier == "MR41S123456"
    assert args.requested_part == "колодки"

    args = parser.parse_args(["vin17-decode", "LFMGJE720DS070251", "--dry-run"])
    assert args.command == "vin17-decode"
    assert args.identifier == "LFMGJE720DS070251"
    assert args.dry_run is True

    args = parser.parse_args(
        ["vin17-search-part", "LFMGJE720DS070251", "--epc", "toyota", "--part-number", "091140G010", "--dry-run"]
    )
    assert args.command == "vin17-search-part"
    assert args.epc == "toyota"
    assert args.part_number == "091140G010"

    args = parser.parse_args(
        [
            "partsapi-lookup",
            "--operation",
            "crosses_with_brand",
            "--part-number",
            "04465-60280",
            "--brand",
            "Toyota",
            "--timeout",
            "7",
            "--max-attempts",
            "2",
            "--dry-run",
        ]
    )
    assert args.command == "partsapi-lookup"
    assert args.operation == "crosses_with_brand"
    assert args.part_number == "04465-60280"
    assert args.timeout == 7
    assert args.max_attempts == 2

    args = parser.parse_args(
        [
            "partsapi-lookup",
            "--operation",
            "crosses_title",
            "--part-number",
            "06D109244E",
            "--lang",
            "en",
            "--dry-run",
        ]
    )
    assert args.command == "partsapi-lookup"
    assert args.operation == "crosses_title"
    assert args.part_number == "06D109244E"
    assert args.lang == "en"

    args = parser.parse_args(
        [
            "partsapi-lookup",
            "--operation",
            "article_crosses",
            "--article-id",
            "1878343",
            "--lang-id",
            "16",
            "--dry-run",
        ]
    )
    assert args.command == "partsapi-lookup"
    assert args.operation == "article_crosses"
    assert args.article_id == "1878343"
    assert args.lang_id == 16

    args = parser.parse_args(
        [
            "partsapi-lookup",
            "--operation",
            "engine_info",
            "--type-id",
            "1404",
            "--vehicle-type",
            "PC",
            "--dry-run",
        ]
    )
    assert args.command == "partsapi-lookup"
    assert args.operation == "engine_info"
    assert args.type_id == "1404"
    assert args.vehicle_type == "PC"

    args = parser.parse_args(
        [
            "partsapi-lookup",
            "--operation",
            "search_tree",
            "--type-id",
            "1404",
            "--dry-run",
        ]
    )
    assert args.command == "partsapi-lookup"
    assert args.operation == "search_tree"

    args = parser.parse_args(["partsapi-category-index", "explain", "--intent", "front_brake_pads", "--query", "колодки"])
    assert args.command == "partsapi-category-index"
    assert args.category_index_command == "explain"
    assert args.intent_id == "front_brake_pads"

    args = parser.parse_args(
        [
            "public-catalog-lookup",
            "--provider",
            "all",
            "--part-number",
            "90919-01275",
            "--page-size",
            "2",
            "--no-detail",
            "--dry-run",
        ]
    )
    assert args.command == "public-catalog-lookup"
    assert args.provider == "all"
    assert args.part_number == "90919-01275"
    assert args.page_size == 2
    assert args.no_detail is True

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
            "oem-catalog-lookup",
            "JTEBU3FJX05027767",
            "--part",
            "передние колодки",
            "--catalog-id",
            "toyota",
            "--car-id",
            "car-1",
            "--group-id",
            "front-brake",
            "--epc",
            "toyota",
            "--dry-run",
        ]
    )
    assert args.command == "oem-catalog-lookup"
    assert args.identifier == "JTEBU3FJX05027767"
    assert args.requested_part == "передние колодки"
    assert args.catalog_id == "toyota"
    assert args.epc == "toyota"
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
            "resolve-vin-oem-parts",
            "1HGCM82633A004352",
            "--part",
            "передние колодки",
            "--live-partsapi-identity",
            "--max-live-calls",
            "2",
            "--partsapi-category-index",
            "docs/agent/partsapi_category_index.json",
        ]
    )
    assert args.command == "resolve-vin-oem-parts"
    assert args.requested_part == "передние колодки"
    assert args.max_live_calls == 2

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

    args = parser.parse_args(["control-report", "--format", "markdown", "--output", "reports/control-report.md"])
    assert args.command == "control-report"
    assert args.format == "markdown"
    assert args.output == "reports/control-report.md"

    args = parser.parse_args(["environment-report", "--format", "json", "--output", "reports/environment-report.json"])
    assert args.command == "environment-report"
    assert args.format == "json"
    assert args.output == "reports/environment-report.json"

    args = parser.parse_args(["memory-review"])
    assert args.command == "memory-review"

    args = parser.parse_args(["memory-review-apply", "--id", "duplicate:note:1-2", "--action", "archive_duplicate"])
    assert args.command == "memory-review-apply"
    assert args.id == "duplicate:note:1-2"
    assert args.action == "archive_duplicate"

    args = parser.parse_args(["knowledge-intake", "--path", "docs/agent/knowledge_map.json", "--dry-run"])
    assert args.command == "knowledge-intake"
    assert args.path == "docs/agent/knowledge_map.json"
    assert args.dry_run is True
    assert args.apply is False

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
