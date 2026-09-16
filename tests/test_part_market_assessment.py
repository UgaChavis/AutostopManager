from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
