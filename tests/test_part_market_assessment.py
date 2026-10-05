from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from autostop_manager.part_market_assessment import assess_part_market


def _observation(
    *,
    source: str,
    host: str,
    price_rub: int,
    observed_at: str | None = None,
    article: str = "1712024",
    brand: str = "Ford",
    kind: str = "original",
    condition: str = "new",
    region: str = "Красноярск, Россия",
    published_at: str | None = None,
) -> dict[str, object]:
    target_reference = " Ford 1712024" if kind == "analog" else ""
    condition_text = "б/у" if condition == "used" else "состояние не указано" if condition == "unknown" else "новый"
    observation: dict[str, object] = {
        "article": article,
        "brand": brand,
        "price_rub": price_rub,
        "source": source,
        "source_excerpt": f"{brand} {article}{target_reference}: {condition_text}, цена {price_rub:,} ₽".replace(
            ",", " "
        ),
        "url": f"https://{host}/offer/{article}",
        "observed_at": observed_at or datetime.now(UTC).date().isoformat(),
        "offer_kind": kind,
        "condition": condition,
        "region": region,
    }
    if published_at is not None:
        observation["published_at"] = published_at
    return observation


def _segment(result: dict, kind: str, condition: str, region: str) -> dict:
    return next(
        item
        for item in result["segments"]
        if item["offer_kind"] == kind and item["condition"] == condition and item["region_scope"] == region
    )


def test_empty_public_study_is_valid_and_never_invents_a_price():
    result = assess_part_market(article="1712024", observations=[])

    assert result["ok"] is True
    assert result["status"] == "no_valid_public_evidence"
    assert result["accepted_offer_count"] == 0
    assert result["rejected_observation_count"] == 0
    assert all(segment["median_price_rub"] is None for segment in result["segments"])

    for malformed in (None, {}, [{}] * 61):
        invalid = assess_part_market(article="1712024", observations=malformed)
        assert invalid["ok"] is False
        assert invalid["error_code"] == "market_observations_invalid"


def test_assessment_returns_median_only_for_three_independent_exact_original_offers():
    result = assess_part_market(
        article="1712024",
        brand="Ford",
        observations=[
            _observation(source="A", host="market-a.example", price_rub=5_000),
            _observation(source="B", host="market-b.example", price_rub=6_000),
            _observation(source="C", host="market-c.example", price_rub=7_000),
        ],
    )

    segment = _segment(result, "original", "new", "krasnoyarsk")
    assert result["ok"] is True
    assert result["status"] == "assessed"
    assert segment["independent_offer_count"] == 3
    assert segment["median_price_rub"] == 6_000
    assert segment["median_available"] is True
    assert _segment(result, "analog", "new", "rf")["median_price_rub"] is None


def test_brand_and_article_survive_vin_redaction_in_source_excerpt():
    observation = _observation(
        source="Catalog", host="catalog.example", price_rub=5_000, article="7700100008", brand="Renault"
    )
    observation["source_excerpt"] = f"{observation['source_excerpt']}; VIN WBA 000000/00000000"

    result = assess_part_market(article="7700100008", brand="Renault", observations=[observation])

    assert result["accepted_offer_count"] == 1
    excerpt = _segment(result, "original", "new", "krasnoyarsk")["offers"][0]["source_excerpt"]
    assert "Renault 7700100008" in excerpt
    assert "WBA 000000/00000000" not in excerpt


def test_assessment_never_combines_three_different_analog_skus_into_one_median():
    observations = [
        _observation(
            source=f"Analog {index}",
            host=f"analog-{index}.example",
            price_rub=price,
            article=article,
            brand=brand,
            kind="analog",
        )
        for index, (article, brand, price) in enumerate(
            (("P111", "Brembo", 4_000), ("P222", "TRW", 5_000), ("P333", "ATE", 6_000))
        )
    ]
    result = assess_part_market(article="1712024", brand="Ford", observations=observations)

    segment = _segment(result, "analog", "new", "krasnoyarsk")
    assert result["accepted_offer_count"] == 3
    assert result["status"] == "insufficient_independent_offers"
    assert segment["median_price_rub"] is None
    assert segment["median_eligible"] is False
    assert segment["median_input_offer_count"] == 0
    assert segment["median_exclusion_reasons"] == {"mixed_analog_skus": 3}
    assert (segment["min_price_rub"], segment["max_price_rub"]) == (4_000, 6_000)


def test_assessment_requires_one_brand_for_original_median_without_target_brand():
    observations = [
        _observation(source=f"Original {index}", host=f"original-{index}.example", price_rub=price, brand=brand)
        for index, (brand, price) in enumerate((("Ford", 5_000), ("Motorcraft", 6_000), ("FoMoCo", 7_000)))
    ]
    result = assess_part_market(article="1712024", observations=observations, brand=None)

    segment = _segment(result, "original", "new", "krasnoyarsk")
    assert result["accepted_offer_count"] == 3
    assert segment["median_price_rub"] is None
    assert segment["median_exclusion_reasons"] == {"mixed_original_brands": 3}
    assert (segment["min_price_rub"], segment["max_price_rub"]) == (5_000, 7_000)


def test_assessment_keeps_analog_median_for_three_offers_of_same_sku():
    observations = [
        _observation(
            source=f"Analog {index}",
            host=f"same-analog-{index}.example",
            price_rub=price,
            article="P111",
            brand="Brembo",
            kind="analog",
        )
        for index, price in enumerate((4_000, 5_000, 6_000))
    ]
    result = assess_part_market(article="1712024", brand="Ford", observations=observations)

    segment = _segment(result, "analog", "new", "krasnoyarsk")
    assert segment["median_eligible"] is True
    assert segment["median_price_rub"] == 5_000


def test_assessment_deduplicates_same_domain_and_keeps_krasnoyarsk_rf_and_analog_separate():
    duplicate_old = _observation(source="A-old", host="market-a.example", price_rub=5_000, observed_at="2026-09-01")
    duplicate_new = _observation(source="A-new", host="market-a.example", price_rub=7_000, observed_at="2026-09-10")
    analog = _observation(
        source="RF analog",
        host="market-rf.example",
        price_rub=4_000,
        article="P123",
        brand="Brembo",
        kind="analog",
        region="РФ: Москва",
    )
    result = assess_part_market(article="1712024", brand="Ford", observations=[duplicate_old, duplicate_new, analog])

    original = _segment(result, "original", "new", "krasnoyarsk")
    analog_rf = _segment(result, "analog", "new", "rf")
    assert result["status"] == "insufficient_independent_offers"
    assert original["independent_offer_count"] == 1
    assert original["offers"][0]["price_rub"] == 7_000
    assert original["median_price_rub"] is None
    assert analog_rf["independent_offer_count"] == 1
    assert analog_rf["offers"][0]["article"] == "P123"
    assert result["rejected_observations"] == [{"observation_index": 0, "code": "duplicate_source_in_segment"}]


def test_assessment_keeps_used_rf_offers_out_of_the_new_krasnoyarsk_segment():
    result = assess_part_market(
        article="1712024",
        brand="Ford",
        observations=[
            _observation(source="A", host="used-a.example", price_rub=2_000, condition="used", region="РФ: Омск"),
            _observation(source="B", host="used-b.example", price_rub=3_000, condition="used", region="РФ: Омск"),
            _observation(source="C", host="used-c.example", price_rub=4_000, condition="used", region="РФ: Омск"),
        ],
    )

    used_rf = _segment(result, "original", "used", "rf")
    new_local = _segment(result, "original", "new", "krasnoyarsk")
    assert used_rf["median_price_rub"] == 3_000
    assert new_local["independent_offer_count"] == 0


def test_assessment_keeps_unknown_condition_but_excludes_it_from_median():
    observations = [
        _observation(source=f"A{index}", host=f"unknown-{index}.example", price_rub=3_000, condition="unknown")
        for index in range(3)
    ]
    result = assess_part_market(article="1712024", brand="Ford", observations=observations)

    unknown = _segment(result, "original", "unknown", "krasnoyarsk")
    assert unknown["independent_offer_count"] == 3
    assert unknown["median_eligible"] is False
    assert unknown["median_price_rub"] is None


def test_assessment_excludes_stale_page_from_current_median():
    today = datetime.now(UTC).date().isoformat()
    stale_date = (datetime.now(UTC).date() - timedelta(days=31)).isoformat()
    result = assess_part_market(
        article="1712024",
        brand="Ford",
        observations=[
            _observation(source="old", host="date-old.example", price_rub=9_000, published_at=stale_date),
            _observation(source="A", host="date-a.example", price_rub=5_000, published_at=today),
            _observation(source="B", host="date-b.example", price_rub=6_000, published_at=today),
            _observation(source="C", host="date-c.example", price_rub=7_000, published_at=today),
        ],
    )

    segment = _segment(result, "original", "new", "krasnoyarsk")
    assert segment["median_price_rub"] == 6_000
    assert segment["median_input_offer_count"] == 3
    assert segment["excluded_from_current_median_count"] == 1
    assert segment["median_exclusion_reasons"] == {"stale_published_page": 1}
    assert segment["median_confidence"] == "medium"


def test_assessment_rejects_overflowing_price_without_internal_error():
    observation = _observation(source="Synthetic", host="synthetic.example", price_rub=5_000)
    observation["price_rub"] = 10**400

    result = assess_part_market(article="1712024", brand="Ford", observations=[observation])

    assert result["ok"] is False
    assert result["accepted_offer_count"] == 0
    assert result["rejected_observations"] == [{"observation_index": 0, "code": "observation_fields_invalid"}]


@pytest.mark.parametrize("field", ["observed_at", "published_at"])
@pytest.mark.parametrize("variant", ["trailing_text", "timestamp", "compact", "week_date", "numeric"])
def test_assessment_requires_calendar_date_without_silent_truncation(field, variant):
    today = datetime.now(UTC).date()
    week = today.isocalendar()
    invalid = {
        "trailing_text": today.isoformat() + "INVALID",
        "timestamp": today.isoformat() + "T00:00:00Z",
        "compact": today.strftime("%Y%m%d"),
        "week_date": f"{week.year}-W{week.week:02d}-{week.weekday}",
        "numeric": int(today.strftime("%Y%m%d")),
    }[variant]
    observation = _observation(source="Synthetic", host="synthetic.example", price_rub=5_000)
    observation[field] = invalid

    result = assess_part_market(article="1712024", brand="Ford", observations=[observation])

    assert result["ok"] is False
    assert result["accepted_offer_count"] == 0
    assert result["rejected_observations"] == [{"observation_index": 0, "code": "observation_fields_invalid"}]


def test_assessment_lowers_current_median_confidence_for_missing_page_date_or_old_observation():
    today = datetime.now(UTC).date()
    old_observation = (today - timedelta(days=8)).isoformat()
    missing_page_date = assess_part_market(
        article="1712024",
        brand="Ford",
        observations=[
            _observation(source="A", host="missing-a.example", price_rub=5_000),
            _observation(source="B", host="missing-b.example", price_rub=6_000),
            _observation(source="C", host="missing-c.example", price_rub=7_000),
        ],
    )
    old_observation_date = assess_part_market(
        article="1712024",
        brand="Ford",
        observations=[
            _observation(source="A", host="recent-a.example", price_rub=5_000, published_at=today.isoformat()),
            _observation(
                source="B",
                host="old-observed.example",
                price_rub=6_000,
                observed_at=old_observation,
                published_at=today.isoformat(),
            ),
            _observation(source="C", host="recent-c.example", price_rub=7_000, published_at=today.isoformat()),
        ],
    )

    missing_segment = _segment(missing_page_date, "original", "new", "krasnoyarsk")
    old_segment = _segment(old_observation_date, "original", "new", "krasnoyarsk")
    assert missing_segment["median_price_rub"] == 6_000
    assert missing_segment["median_confidence"] == "low"
    assert {offer["price_freshness"] for offer in missing_segment["offers"]} == {"publication_date_missing"}
    assert old_segment["median_price_rub"] == 6_000
    assert old_segment["median_confidence"] == "low"
    assert {offer["observation_freshness"] for offer in old_segment["offers"]} == {"recent", "stale"}


def test_assessment_supports_cyrillic_identifiers_and_ruble_abbreviation():
    observations = [
        _observation(
            source=f"A{index}",
            host=f"cyrillic-{index}.example",
            price_rub=price,
            article="АБ-123",
            brand="Тормоз",
        )
        for index, price in enumerate((5_000, 6_000, 7_000))
    ]
    for observation in observations:
        observation["source_excerpt"] = str(observation["source_excerpt"]).replace("₽", "р.")

    result = assess_part_market(article="АБ-123", brand="Тормоз", observations=observations)

    assert result["ok"] is True
    assert result["target"]["article"] == "АБ123"
    assert result["target"]["brand"] == "ТОРМОЗ"
    assert _segment(result, "original", "new", "krasnoyarsk")["median_price_rub"] == 6_000


def test_assessment_rejects_unverified_article_brand_price_and_condition_claims():
    missing_article = _observation(source="A", host="a.example", price_rub=5_000)
    missing_article["source_excerpt"] = "Ford 1712025 новый цена 5 000 ₽"
    missing_brand = _observation(source="B", host="b.example", price_rub=5_000)
    missing_brand["source_excerpt"] = "Oxford 1712024 новый цена 5 000 ₽"
    missing_price = _observation(source="C", host="c.example", price_rub=5_000)
    missing_price["source_excerpt"] = "Ford 1712024 новый цена 4 000 ₽"
    missing_condition = _observation(source="D", host="d.example", price_rub=5_000)
    missing_condition["source_excerpt"] = "Ford 1712024 цена 5 000 ₽"

    result = assess_part_market(
        article="1712024",
        brand="Ford",
        observations=[missing_article, missing_brand, missing_price, missing_condition],
    )

    assert result["ok"] is False
    assert {item["code"] for item in result["rejected_observations"]} == {
        "article_not_in_source_excerpt",
        "brand_not_in_source_excerpt",
        "price_not_in_source_excerpt",
        "condition_not_in_source_excerpt",
    }


def test_assessment_rejects_price_from_another_product_on_the_same_page():
    observations = [
        _observation(source=f"Catalog {index}", host=f"catalog-{index}.example", price_rub=1_000) for index in range(3)
    ]
    for observation in observations:
        observation["source_excerpt"] = "Ford 1712024 новый 7 000 ₽; Ford 1712025 новый 1 000 ₽"

    result = assess_part_market(article="1712024", brand="Ford", observations=observations)

    assert result["ok"] is False
    assert result["status"] == "no_valid_public_evidence"
    assert result["accepted_offer_count"] == 0
    assert result["rejected_observations"] == [
        {"observation_index": index, "code": "price_ambiguous_in_source_excerpt"} for index in range(3)
    ]
    assert all(segment["median_price_rub"] is None for segment in result["segments"])


def test_assessment_rejects_single_price_after_another_sku():
    observations = [
        _observation(source=f"Catalog {index}", host=f"adjacent-{index}.example", price_rub=1_000) for index in range(3)
    ]
    for observation in observations:
        observation["source_excerpt"] = "Ford 1712024 новый; Ford 1712025 новый 1 000 ₽"

    result = assess_part_market(article="1712024", brand="Ford", observations=observations)

    assert result["ok"] is False
    assert result["status"] == "no_valid_public_evidence"
    assert result["rejected_observations"] == [
        {"observation_index": index, "code": "price_not_tied_to_article_in_source_excerpt"} for index in range(3)
    ]
    assert all(segment["median_price_rub"] is None for segment in result["segments"])


def test_assessment_accepts_one_price_after_the_requested_sku():
    observation = _observation(source="Single item", host="single.example", price_rub=1_000)
    observation["source_excerpt"] = "Другая деталь Ford 1712025; Ford 1712024 новый 1 000 ₽"

    result = assess_part_market(article="1712024", brand="Ford", observations=[observation])

    assert result["ok"] is True
    assert result["accepted_offer_count"] == 1
    assert result["rejected_observations"] == []


@pytest.mark.parametrize(
    ("money_text", "claimed_price"),
    [
        ("1999,50 ₽", 50),
        ("99,50 рублей", 50),
        ("49.90 RUB", 90),
        ("1 999,50 руб.", 50),
        ("1.500 ₽", 500),
        ("19 99 ₽", 99),
        ("-5000 ₽", 5000),
        ("- 5 000 ₽", 5000),
        ("−5000 ₽", 5000),
        ("ZX5000 ₽", 5000),
        ("1'999 ₽", 999),
        ("1’999 ₽", 999),
        ("10–50 ₽", 50),
        ("10—50 ₽", 50),
        ("−1999/50 ₽", 50),
        ("99:50 ₽", 50),
    ],
)
def test_assessment_never_uses_a_money_suffix_as_the_full_ruble_price(money_text, claimed_price):
    observations = [
        _observation(source=f"Synthetic {index}", host=f"money-{index}.example", price_rub=claimed_price)
        for index in range(3)
    ]
    for observation in observations:
        observation["source_excerpt"] = f"Ford 1712024 новый, цена {money_text}"

    result = assess_part_market(article="1712024", brand="Ford", observations=observations)

    assert result["ok"] is False
    assert result["status"] == "no_valid_public_evidence"
    assert result["accepted_offer_count"] == 0
    assert all(segment["median_price_rub"] is None for segment in result["segments"])


@pytest.mark.parametrize(
    "other_money",
    [
        "99,50 ₽",
        "-50 ₽",
        "19 99 ₽",
        "1.500 ₽",
        "10–50 ₽",
        "1’050 ₽",
        "(99,50 ₽)",
        "[99,50 ₽]",
        "«99,50 ₽»",
        "99:50 ₽",
    ],
)
def test_assessment_does_not_hide_an_unsupported_money_amount_beside_a_valid_price(other_money):
    observations = [
        _observation(source=f"Synthetic {index}", host=f"mixed-money-{index}.example", price_rub=50)
        for index in range(3)
    ]
    for observation in observations:
        observation["source_excerpt"] = f"Ford 1712024 новый, {other_money}; цена 50 ₽"

    result = assess_part_market(article="1712024", brand="Ford", observations=observations)

    assert result["accepted_offer_count"] == 0
    assert result["rejected_observations"] == [
        {"observation_index": index, "code": "price_ambiguous_in_source_excerpt"} for index in range(3)
    ]
    assert all(segment["median_price_rub"] is None for segment in result["segments"])


@pytest.mark.parametrize(
    "excerpt",
    [
        "Ford 1712024 новый цена 5000 ₽",
        "Ford 1712024 новый цена 5 000 рублей.",
        "Ford 1712024 новый цена 5\u00a0000 руб.",
        "Ford 1712024 новый цена 5\u202f000 RUB",
        "Ford 1712024 новый цена 5000,00 ₽",
        "Ford 1712024 новый цена 5 000.00 р.",
        "Ford 1712024 5000 ₽ новый",
        "Ford 1712024 новый цена (5000 ₽)",
        "Ford 1712024 новый цена [5 000 руб.]",
        "Ford 1712024 новый цена «5000руб»",
        "Ford: 1712024 5000 ₽ новый",
        "Ford; 1712024 5000 ₽ новый",
        "Ford ( 1712024 5000 ₽) новый",
        "Ford 1712024:5000 ₽ новый",
        "Ford 171-2024 5000 ₽ новый",
        "Ford 171.2024 5000 ₽ новый",
        "Ford 171/2024 5000 ₽ новый",
    ],
)
def test_assessment_preserves_exact_whole_ruble_prices_and_numeric_article_boundary(excerpt):
    observations = [
        _observation(source=f"Synthetic {index}", host=f"whole-money-{index}.example", price_rub=5000)
        for index in range(3)
    ]
    for observation in observations:
        observation["source_excerpt"] = excerpt

    result = assess_part_market(article="1712024", brand="Ford", observations=observations)

    assert result["ok"] is True
    assert result["accepted_offer_count"] == 3
    assert _segment(result, "original", "new", "krasnoyarsk")["median_price_rub"] == 5000


@pytest.mark.parametrize(
    ("money_text", "claimed_price", "accepted"),
    [("123 500 ₽", 500, False), ("123 500 ₽", 123500, True), ("123 456 789 ₽", 456789, False)],
)
def test_assessment_never_strips_a_known_article_from_a_valid_whole_money_amount(money_text, claimed_price, accepted):
    observations = [
        _observation(
            source=f"Synthetic {index}", host=f"numeric-sku-{index}.example", price_rub=claimed_price, article="123"
        )
        for index in range(3)
    ]
    for observation in observations:
        observation["source_excerpt"] = f"Ford 123 новый, цена {money_text}"

    result = assess_part_market(article="123", brand="Ford", observations=observations)

    assert result["accepted_offer_count"] == (3 if accepted else 0)
    assert _segment(result, "original", "new", "krasnoyarsk")["median_price_rub"] == (
        claimed_price if accepted else None
    )


def test_assessment_cannot_reinterpret_a_repeated_article_as_the_prefix_of_a_malformed_price():
    observation = _observation(source="Synthetic", host="repeated-article.example", price_rub=50, article="123")
    observation["source_excerpt"] = "Ford 123 новый, цена 123:50 ₽"

    result = assess_part_market(article="123", brand="Ford", observations=[observation])

    assert result["accepted_offer_count"] == 0
    assert result["rejected_observations"] == [{"observation_index": 0, "code": "price_not_in_source_excerpt"}]


def test_assessment_cannot_label_another_target_city_as_krasnoyarsk():
    result = assess_part_market(
        article="1712024",
        brand="Ford",
        target_region="Москва",
        observations=[_observation(source="Moscow", host="moscow.example", price_rub=1_000, region="Москва")],
    )

    assert result == {"ok": False, "schema": "PartMarketAssessmentV1", "error_code": "market_target_invalid"}


def test_assessment_rejects_original_brand_or_article_mismatch_and_unsafe_url():
    wrong_original = _observation(source="A", host="a.example", price_rub=5_000, article="1712025")
    unsafe_url = _observation(source="B", host="b.example", price_rub=5_000)
    unsafe_url["url"] = "http://127.0.0.1/private"

    result = assess_part_market(article="1712024", brand="Ford", observations=[wrong_original, unsafe_url])

    assert result["ok"] is False
    assert result["rejected_observations"] == [
        {"observation_index": 0, "code": "original_not_exact_target_match"},
        {"observation_index": 1, "code": "observation_fields_invalid"},
    ]


@pytest.mark.parametrize(
    ("source_condition", "supported_condition"),
    [
        ("новый", "new"),
        ("НОВАЯ", "new"),
        ("новое", "new"),
        ("новые", "new"),
        ("new", "new"),
        ("brand new", "new"),
        ("brand-new", "new"),
        ("brand‑new", "new"),
        ("unused", "new"),
        ("не использовался", "new"),
        ("не использовалась", "new"),
        ("не использовалось", "new"),
        ("не использовались", "new"),
        ("не использован", "new"),
        ("не использована", "new"),
        ("не использовано", "new"),
        ("не использованы", "new"),
        ("новый, не использовался", "new"),
        ("не б/у, новый", "new"),
        ("not used, new", "new"),
        ("не совсем б/у, новый", "new"),
        ("б/у", "used"),
        ("(б/у)", "used"),
        ("б / у", "used"),
        ("бу,", "used"),
        ("used", "used"),
        ("с разборки", "used"),
        ("контрактный", "used"),
        ("контрактная", "used"),
        ("контрактное", "used"),
        ("контрактные", "used"),
        ("б/у, не новый", "used"),
        ("не новый, с разборки", "used"),
        ("not new, used", "used"),
        ("б/у, как новый", "used"),
        ("used, like new", "used"),
        ("used, as new", "used"),
        ("продаётся как б/у", "used"),
        ("sold as used", "used"),
        ("like used", None),
        ("не как б/у", None),
        ("not as used", None),
        ("not like used", None),
        ("состояние не указано", None),
        ("renewed", None),
        ("newton", None),
        ("misused", None),
        ("контрактник", None),
        ("не используется", None),
        ("не новый", None),
        ("не совсем новый", None),
        ("не очень новый", None),
        ("не полностью новый", None),
        ("не «новый»", None),
        ("не (новый)", None),
        ("не — новый", None),
        ("не – новый", None),
        ("не‑новый", None),
        ("не—новый", None),
        ("не−новый", None),
        ("not — new", None),
        ("новый, но не — новый", None),
        ("б/у, но не — б/у", None),
        ("не как новый", None),
        ("not as new", None),
        ("not like new", None),
        ("not new", None),
        ("not brand new", None),
        ("not-brand-new", None),
        ("не б/у", None),
        ("not used", None),
        ("never used", None),
        ("not unused", None),
        ("как новый", None),
        ("like new", None),
        ("as new", None),
        ("новый, б/у", None),
        ("новый/б/у", None),
        ("new and used", None),
        ("unused, used", None),
        ("новый, но не новый", None),
        ("used, not used", None),
        ("не новый и не б/у", None),
        ("не не использовался", None),
    ],
)
@pytest.mark.parametrize("declared_condition", ["new", "used", "unknown"])
def test_source_condition_evidence_controls_known_segments_and_medians(
    source_condition, supported_condition, declared_condition
):
    today = datetime.now(UTC).date().isoformat()
    observations = []
    for index, price_rub in enumerate((5_000, 6_000, 7_000)):
        observation = _observation(
            source=f"Synthetic condition {index}",
            host=f"condition-{index}.example",
            price_rub=price_rub,
            condition=declared_condition,
            published_at=today,
        )
        observation["source_excerpt"] = f"Ford 1712024: {source_condition}; цена {price_rub} ₽"
        observations.append(observation)

    result = assess_part_market(article="1712024", brand="Ford", observations=observations)
    accepted = declared_condition == "unknown" or declared_condition == supported_condition
    assert result["accepted_offer_count"] == (3 if accepted else 0)
    segment = _segment(result, "original", declared_condition, "krasnoyarsk")
    expected_median = 6_000 if accepted and declared_condition != "unknown" else None
    assert segment["median_price_rub"] == expected_median
    assert segment["median_input_offer_count"] == (3 if expected_median is not None else 0)
    assert segment["independent_offer_count"] == (3 if accepted else 0)
    assert all(offer["condition"] == declared_condition for offer in segment["offers"])
    assert all(
        item["median_price_rub"] is None for item in result["segments"] if item["condition"] != declared_condition
    )
    if not accepted:
        assert [row["code"] for row in result["rejected_observations"]] == ["condition_not_in_source_excerpt"] * 3
    if declared_condition == "unknown":
        assert segment["median_eligible"] is False
        assert segment["median_exclusion_reasons"] == {"unknown_condition": 3}


@pytest.mark.parametrize(
    ("article", "brand", "source_condition", "supported_condition"),
    [
        ("NEW-1712024", "Ford", "", None),
        ("USED-1712024", "Ford", "", None),
        ("UNUSED-1712024", "Ford", "", None),
        ("1712024", "New Era", "", None),
        ("1712024", "Used Parts", "", None),
        ("1712024", "Brand New", "", None),
        ("NEW-1712024", "Ford", "новый", "new"),
        ("NEW-1712024", "Ford", "б/у", "used"),
        ("USED-1712024", "Ford", "new", "new"),
        ("USED-1712024", "Ford", "used", "used"),
        ("1712024", "New Era", "new", "new"),
        ("1712024", "New Era", "б/у", "used"),
        ("1712024", "Used Parts", "новый", "new"),
        ("1712024", "Used Parts", "used", "used"),
        ("1712024", "Brand New", "new", "new"),
        ("1712024", "Brand New", "б/у", "used"),
        ("NEW", "Ford", "новый", "new"),
        ("USED", "Ford", "б/у", "used"),
        ("1712024", "New", "новый", "new"),
        ("1712024", "Used", "б/у", "used"),
    ],
)
@pytest.mark.parametrize("declared_condition", ["new", "used", "unknown"])
def test_condition_evidence_excludes_known_identity_without_losing_separate_state(
    article, brand, source_condition, supported_condition, declared_condition
):
    observations = []
    for index, price in enumerate((5_000, 6_000, 7_000)):
        row = _observation(
            source="Synthetic",
            host=f"identity-{index}.example",
            price_rub=price,
            article=article,
            brand=brand,
            condition=declared_condition,
        )
        row["source_excerpt"] = f"{brand} {article}: {source_condition}; цена {price} ₽"
        observations.append(row)
    result = assess_part_market(article=article, brand=brand, observations=observations)
    accepted = declared_condition == "unknown" or declared_condition == supported_condition
    assert result["accepted_offer_count"] == (3 if accepted else 0)
    segment = _segment(result, "original", declared_condition, "krasnoyarsk")
    assert segment["median_price_rub"] == (6_000 if accepted and declared_condition != "unknown" else None)
    assert all(offer["condition"] == declared_condition for offer in segment["offers"])
    if not accepted:
        assert [row["code"] for row in result["rejected_observations"]] == ["condition_not_in_source_excerpt"] * 3


def test_masked_identity_cannot_join_non_use_phrase_across_brand():
    row = _observation(source="Synthetic", host="identity-gap.example", price_rub=5_000, brand="New Era")
    row["source_excerpt"] = "1712024: не New Era использован; цена 5000 ₽"
    result = assess_part_market(article="1712024", brand="New Era", observations=[row])
    assert result["accepted_offer_count"] == 0
    assert result["rejected_observations"] == [{"observation_index": 0, "code": "condition_not_in_source_excerpt"}]


@pytest.mark.parametrize("target_brand", ["Ford", "New Era", "Used Parts"])
@pytest.mark.parametrize("condition", ["new", "used"])
def test_analog_target_identity_does_not_supply_conflicting_condition(target_brand, condition):
    row = _observation(
        source="Synthetic",
        host="analog-condition.example",
        price_rub=5_000,
        kind="analog",
        article="AN12345",
        brand="SynthBrand",
        condition=condition,
    )
    source_condition = "новый" if condition == "new" else "б/у"
    row["source_excerpt"] = f"{target_brand} NEW-1712024; SynthBrand AN12345: {source_condition}; цена 5000 ₽"
    result = assess_part_market(article="NEW-1712024", brand=target_brand, observations=[row])
    assert result["accepted_offer_count"] == 1
    assert _segment(result, "analog", condition, "krasnoyarsk")["independent_offer_count"] == 1


@pytest.mark.parametrize(("brand", "source_condition"), [("Not", "not new"), ("Like", "like new"), ("As", "as new")])
def test_identity_mask_preserves_original_negation_and_comparison_operators(brand, source_condition):
    row = _observation(source="Synthetic", host="identity-operator.example", price_rub=5_000, brand=brand)
    row["source_excerpt"] = f"{brand} 1712024: {source_condition}; цена 5000 ₽"
    result = assess_part_market(article="1712024", brand=brand, observations=[row])
    assert result["accepted_offer_count"] == 0
    assert result["rejected_observations"] == [{"observation_index": 0, "code": "condition_not_in_source_excerpt"}]
