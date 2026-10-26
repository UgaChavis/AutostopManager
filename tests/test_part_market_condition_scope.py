"""A listing's condition must describe the priced part, not another object."""

from datetime import UTC, datetime
import socket

import pytest

from autostop_manager.part_market_assessment import assess_part_market


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("part market assessment must remain a pure operation")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def observation(excerpt, *, condition="new", index=0, price=5000):
    return {
        "article": "1712024",
        "brand": "Ford",
        "price_rub": price,
        "source": f"Synthetic {index}",
        "source_excerpt": excerpt,
        "url": f"https://condition-scope-{index}.example/offer/1712024",
        "observed_at": datetime.now(UTC).date().isoformat(),
        "offer_kind": "original",
        "condition": condition,
        "region": "Красноярск, Россия",
    }


def assess(rows):
    return assess_part_market(article="1712024", brand="Ford", observations=rows)


def local_original(result, condition):
    return next(
        segment
        for segment in result["segments"]
        if (segment["offer_kind"], segment["condition"], segment["region_scope"])
        == ("original", condition, "krasnoyarsk")
    )


@pytest.mark.parametrize(("condition", "marker"), [("new", "новый"), ("used", "б/у")])
@pytest.mark.parametrize("foreign_first", [False, True])
def test_other_part_condition_does_not_classify_priced_target(condition, marker, foreign_first):
    target = "Ford 1712024 цена 5000 ₽"
    foreign = f"Brembo P111 {marker}"
    excerpt = f"{foreign}; {target}" if foreign_first else f"{target}; {foreign}"

    result = assess([observation(excerpt, condition=condition)])

    assert result["accepted_offer_count"] == 0
    assert result["rejected_observations"] == [{"observation_index": 0, "code": "condition_not_in_source_excerpt"}]
    assert local_original(result, condition)["median_price_rub"] is None


@pytest.mark.parametrize("packaging", ["упаковка новая", "новая упаковка"])
def test_new_packaging_does_not_prove_part_is_new(packaging):
    result = assess([observation(f"Ford 1712024 цена 5000 ₽; {packaging}")])

    assert result["accepted_offer_count"] == 0
    assert result["rejected_observations"] == [{"observation_index": 0, "code": "condition_not_in_source_excerpt"}]


def test_three_foreign_condition_claims_cannot_create_new_part_median():
    rows = [
        observation(f"Ford 1712024 цена {price} ₽; Brembo P111 новый", index=index, price=price)
        for index, price in enumerate((5000, 6000, 7000))
    ]

    result = assess(rows)

    assert result["status"] == "no_valid_public_evidence"
    assert result["accepted_offer_count"] == 0
    segment = local_original(result, "new")
    assert segment["median_available"] is False
    assert segment["median_price_rub"] is None


@pytest.mark.parametrize(("condition", "marker"), [("new", "новый"), ("used", "б/у")])
@pytest.mark.parametrize(
    "layout",
    [
        "{marker} Ford 1712024 цена 5000 ₽",
        "Ford 1712024 {marker} цена 5000 ₽",
        "Ford 1712024 цена 5000 ₽; Состояние: {marker}",
    ],
)
def test_explicit_target_condition_remains_accepted(condition, marker, layout):
    result = assess([observation(layout.format(marker=marker), condition=condition)])

    assert result["accepted_offer_count"] == 1
    assert result["rejected_observations"] == []
    assert local_original(result, condition)["offers"][0]["condition"] == condition


def test_explicit_neuter_condition_after_price_remains_accepted():
    result = assess([observation("Ford 1712024 цена 5000 ₽; Состояние: новое")])

    assert result["accepted_offer_count"] == 1
    assert local_original(result, "new")["offers"][0]["condition"] == "new"


@pytest.mark.parametrize("foreign_condition", ["новый", "б/у"])
def test_explicit_unknown_is_preserved_when_other_part_has_known_condition(foreign_condition):
    result = assess([observation(f"Ford 1712024 цена 5000 ₽; Brembo P111 {foreign_condition}", condition="unknown")])

    assert result["accepted_offer_count"] == 1
    segment = local_original(result, "unknown")
    assert segment["offers"][0]["condition"] == "unknown"
    assert segment["median_available"] is False


def test_own_condition_does_not_override_foreign_sku_price_binding():
    result = assess([observation("Ford 1712024 новый; Brembo P111 цена 5000 ₽")])

    assert result["accepted_offer_count"] == 0
    assert result["rejected_observations"] == [
        {"observation_index": 0, "code": "price_not_tied_to_article_in_source_excerpt"}
    ]


@pytest.mark.parametrize("reference", ["SKU AB12", "артикул: 1999", "ARTICLE XY9", "Brembo P111"])
def test_foreign_product_closes_scope_for_following_condition_field(reference):
    result = assess([observation(f"Ford 1712024 цена 5000 ₽; {reference}; состояние: новый")])
    assert result["accepted_offer_count"] == 0
    assert result["rejected_observations"] == [{"observation_index": 0, "code": "condition_not_in_source_excerpt"}]


@pytest.mark.parametrize("condition,marker,foreign_marker", [("new", "новый", "б/у"), ("used", "б/у", "новый")])
def test_explicit_target_condition_is_not_contradicted_by_another_product(condition, marker, foreign_marker):
    result = assess(
        [observation(f"Ford 1712024 {marker} цена 5000 ₽; Brembo P111 {foreign_marker}", condition=condition)]
    )
    assert result["accepted_offer_count"] == 1


@pytest.mark.parametrize("packaging", ["new packaging", "box: new", "коробка новая", "новая коробка"])
def test_packaging_does_not_contradict_explicit_used_part(packaging):
    result = assess([observation(f"Ford 1712024 б/у цена 5000 ₽; {packaging}", condition="used")])
    assert result["accepted_offer_count"] == 1


@pytest.mark.parametrize(
    "excerpt",
    [
        "Ford 1712024 новый; Brembo P111 цена 5000 ₽",
        "Brembo P111 для Ford 1712024 цена 5000 ₽; Ford 1712024 новый",
    ],
)
def test_target_cross_reference_condition_does_not_classify_analog(excerpt):
    row = observation(excerpt)
    row.update(article="P111", brand="Brembo", offer_kind="analog")
    result = assess([row])
    assert result["accepted_offer_count"] == 0
    assert result["rejected_observations"] == [{"observation_index": 0, "code": "condition_not_in_source_excerpt"}]


@pytest.mark.parametrize("kind,article", [("original", "1712024"), ("analog", "AN12345")])
@pytest.mark.parametrize("brand", ["555", "Synth 555", "SKU 555"])
def test_known_numeric_brand_cannot_become_foreign_sku(kind, article, brand):
    reference = " для Ford 1712024" if kind == "analog" else ""
    row = observation(f"{brand} {article}{reference} новый цена 5000 ₽")
    row.update(article=article, brand=brand, offer_kind=kind)
    result = assess_part_market(article="1712024", brand=brand if kind == "original" else "Ford", observations=[row])
    assert result["accepted_offer_count"] == 1
