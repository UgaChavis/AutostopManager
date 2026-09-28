from __future__ import annotations

from datetime import UTC, datetime, timedelta

from autostop_manager.avito_listings import _normalize_listing
from autostop_manager.avito_market_assessment import assess_avito_price_sample
from autostop_manager.mcp_tools import register_manager_tools
from autostop_manager.storage import StoreState


def _listing(listing_id: str, price: int, **overrides):
    row = {
        "source": "avito",
        "listing_id": listing_id,
        "url": f"https://www.avito.ru/krasnoyarsk/zapchasti_i_aksessuary/detal_{listing_id}",
        "title": "Ford 1712024 фильтр",
        "description": "Новый оригинальный фильтр",
        "price_rub": price,
        "price_qualifier": "fixed",
        "condition": "Новый",
        "city": "Красноярск",
        "observed_at": datetime.now(UTC).isoformat(),
        "status": "lead",
        "fitment_confirmed": False,
        "availability_confirmed": False,
    }
    row.update(overrides)
    return row


def test_three_distinct_avito_ads_give_only_a_source_scoped_sample_median():
    listings = [_listing(str(12345678 + i), price) for i, price in enumerate((5_000, 7_000, 6_000))]

    result = assess_avito_price_sample(part_number="1712024", listings=listings, city="Красноярск")

    assert result["ok"] is True
    assert result["status"] == "sample_summary"
    assert (result["input_count"], result["accepted_count"], result["excluded_count"]) == (3, 3, 0)
    assert result["independent_source_count"] == 1
    assert result["independent_source_market_median_price_rub"] is None
    assert (result["fitment_confirmed"], result["availability_confirmed"]) == (False, False)
    segment = result["segments"][0]
    assert (segment["min_price_rub"], segment["max_price_rub"], segment["sample_median_price_rub"]) == (
        5_000,
        7_000,
        6_000,
    )
    assert {row["listing_id"] for row in result["accepted_listings"]} == {"12345678", "12345679", "12345680"}
    assert all(
        row["url"].startswith("https://www.avito.ru/") and row["observed_at"] for row in result["accepted_listings"]
    )


def test_normalized_search_row_can_be_assessed_without_additional_fields():
    normalized = _normalize_listing(
        {
            "ad_id": "12345678",
            "url": "https://www.avito.ru/krasnoyarsk/zapchasti_i_aksessuary/filtr_12345678",
            "title": "Новый фильтр Ford 1712024",
            "price": 5_000,
            "price_text": "5 000 ₽",
            "location": {"name": "Красноярск"},
            "condition": "Новый",
        }
    )

    assert normalized is not None
    assert isinstance(normalized["description"], str)
    assert normalized["price_qualifier"] == "fixed"
    result = assess_avito_price_sample(part_number="1712024", listings=[normalized])
    assert result["accepted_count"] == 1
    assert result["status"] == "insufficient_sample"


def test_conditions_and_cities_never_share_a_sample_median():
    rows = [
        _listing("12345678", 5_000),
        _listing("12345679", 6_000),
        _listing("12345680", 2_000, condition="Б/у"),
        _listing("12345681", 8_000, city="Москва"),
    ]

    result = assess_avito_price_sample(part_number="1712024", listings=rows)

    assert result["status"] == "insufficient_sample"
    assert result["accepted_count"] == 4
    assert {(row["condition"], row["city"], row["sample_count"]) for row in result["segments"]} == {
        ("new", "Красноярск", 2),
        ("new", "Москва", 1),
        ("used", "Красноярск", 1),
    }
    assert all(row["sample_median_price_rub"] is None for row in result["segments"])


def test_only_visible_exact_article_and_fixed_positive_prices_enter_the_sample():
    rows = [
        _listing("12345678", 5_000),
        _listing("12345679", 6_000, title="Фильтр Ford", description="Без номера"),
        _listing("12345680", 7_000, title="Ford 17120240 фильтр"),
        _listing("12345681", 8_000, price_qualifier="from"),
        _listing("12345682", 0),
        _listing("12345683", 9_000, source="drom"),
        _listing("12345684", 10_000, url="https://avito.ru.evil.example/detal_12345684"),
        _listing("12345687", 10_500, url="https://www.avito.ru/krasnoyarsk/detal_99999999"),
        _listing("12345685", 11_000, condition=None),
        _listing("12345686", 12_000, city=None),
    ]

    result = assess_avito_price_sample(part_number="1712024", listings=rows)

    assert result["accepted_count"] == 1
    assert result["excluded_count"] == 9
    assert result["status"] == "insufficient_sample"
    assert [row["code"] for row in result["excluded_listings"]] == [
        "part_number_not_in_listing",
        "part_number_not_in_listing",
        "fixed_price_missing",
        "fixed_price_missing",
        "source_identity_invalid",
        "source_identity_invalid",
        "source_identity_invalid",
        "condition_unknown",
        "city_unknown",
    ]
    assert result["segments"][0]["sample_median_price_rub"] is None


def test_newer_read_detail_replaces_search_row_for_the_same_ad():
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    rows = [
        _listing("12345678", 5_000, observed_at=old),
        _listing("12345678", 5_500, description="Ford 1712024, новый фильтр"),
        _listing("12345679", 6_000),
        _listing("12345680", 7_000),
    ]

    result = assess_avito_price_sample(part_number="1712024", listings=rows)

    assert result["accepted_count"] == 3
    assert result["excluded_listings"] == [{"listing_index": 0, "code": "duplicate_listing_id"}]
    assert result["segments"][0]["sample_median_price_rub"] == 6_000
    assert next(row for row in result["accepted_listings"] if row["listing_id"] == "12345678")["price_rub"] == 5_500


def test_invalid_target_filters_and_oversized_sample_fail_without_a_price():
    assert assess_avito_price_sample(part_number="", listings=[])["error_code"] == "part_number_invalid"
    assert (
        assess_avito_price_sample(part_number="1712024", listings=[_listing("12345678", 5_000)] * 61)["error_code"]
        == "listings_invalid"
    )
    assert (
        assess_avito_price_sample(part_number="1712024", listings=[], condition="unknown")["error_code"]
        == "condition_invalid"
    )
    assert assess_avito_price_sample(part_number="1712024", listings=[], city="")["error_code"] == "city_invalid"


class _ToolServer:
    def __init__(self):
        self.tools = {}
        self.options = {}

    def tool(self, *, name, **kwargs):
        def register(function):
            self.tools[name] = function
            self.options[name] = kwargs
            return function

        return register


def test_avito_sample_tool_is_registered_as_read_only(tmp_path):
    server = _ToolServer()
    register_manager_tools(server, StoreState(tmp_path / "memory.sqlite3"), include_tools={"assess_avito_price_sample"})

    assert set(server.tools) == {"assess_avito_price_sample"}
    result = server.tools["assess_avito_price_sample"](part_number="1712024", listings=[])
    assert result["status"] == "no_valid_listings"
    annotations = server.options["assess_avito_price_sample"]["annotations"]
    assert annotations.readOnlyHint is True
    assert annotations.destructiveHint is False
