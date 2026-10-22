"""Identified public observations supply evidence once, without inventing provenance."""

from copy import deepcopy

import pytest

from autostop_manager.automotive_labor import calculate_work_price

OPERATION = "замена масла"
POLICY = {"basis": "public_observations", "version": "identity-test-v1"}
SOURCE = {
    "provider": "public_web",
    "primary_lineage": "sto-a.example",
    "locator": "https://sto-a.example/prices",
}


def quote(price, **fields):
    return {
        "operation_name": OPERATION,
        "price_rub": price,
        "city": "Красноярск",
        "labor_only": True,
        "includes_parts": False,
        **deepcopy(fields),
    }


def independent_quote(price=3000, **fields):
    return quote(price, source="independent.example", **fields)


def calculate(observations, *, operation=OPERATION, **options):
    inputs = {
        "labor": [{"operation_id": "work", "operation_name": operation}],
        "policy": deepcopy(POLICY),
        "observations": deepcopy(observations),
        **deepcopy(options),
    }
    before = deepcopy(inputs)
    response = calculate_work_price(**inputs)
    assert inputs == before
    assert response["ok"] is True
    assert response["tool_id"] == "manager.calculate_work_price"
    assert response["execution"] == {"network_calls": 0, "attempts": []}
    return response


def assert_price(response, price, bounds):
    assert response["outcome"] == "success"
    assert response["missing_fields"] == []
    data = response["data"]
    assert data["total"] == data["known_subtotal"] == price
    assert data["range"] == bounds
    assert len(data["operations"]) == 1
    assert data["operations"][0]["price"] == price
    assert data["operations"][0]["range"] == bounds


@pytest.mark.parametrize(
    "provenance,replay_changes",
    [
        pytest.param({"source": SOURCE}, {}, id="exact-structured"),
        pytest.param(
            {
                "source": "sto-a.example",
                "evidence_source": {**SOURCE, "fetched_at": "2026-10-01T00:00:00Z"},
                "captured_at": "2026-10-01T00:00:00Z",
            },
            {
                "evidence_source": {**SOURCE, "fetched_at": "2026-10-02T00:00:00Z"},
                "captured_at": "2026-10-02T00:00:00Z",
            },
            id="timestamp-only",
        ),
        pytest.param({"source": {"primary_lineage": "sto-a.example"}}, {}, id="lineage-without-locator"),
        pytest.param({"source": {"provider": "public_web"}, "url": SOURCE["locator"]}, {}, id="locator-only"),
        pytest.param({"source": "sto-a.example"}, {}, id="concrete-plain-source-only"),
    ],
)
def test_identified_replay_does_not_change_the_public_price(provenance, replay_changes):
    first = quote(1000, **provenance)
    replay = {**deepcopy(first), **deepcopy(replay_changes)}
    second = independent_quote()
    assert_price(calculate([first, second]), 2000, [1000, 3000])
    assert_price(calculate([first, replay, second]), 2000, [1000, 3000])


@pytest.mark.parametrize("changed", ["source", "locator", "city", "amount"])
def test_distinct_observation_keys_keep_their_individual_weight(changed):
    first = quote(1000, source=SOURCE)
    another = deepcopy(first)
    if changed == "source":
        another["source"]["primary_lineage"] = "other.example"
    elif changed == "locator":
        another["source"]["locator"] = "https://sto-a.example/Prices"
    elif changed == "city":
        another["city"] = "Санкт-Петербург"
    else:
        another["price_rub"] = 2000
    assert_price(
        calculate([first, another, independent_quote(5000)]),
        2000 if changed == "amount" else 1000,
        [1000, 5000],
    )


@pytest.mark.parametrize("negative_first", [False, True])
@pytest.mark.parametrize("scope", [{"includes_parts": True}, {"labor_only": False}], ids=["parts", "not-labor"])
def test_explicit_negative_scope_vetoes_only_its_identified_group(scope, negative_first):
    positive = quote(1000, source=SOURCE)
    negative = {**deepcopy(positive), **scope}
    same_observation = [negative, positive] if negative_first else [positive, negative]
    assert_price(calculate([*same_observation, independent_quote()]), 3000, [3000, 3000])


@pytest.mark.parametrize(
    "provenance",
    [{}, {"source": {"provider": "public_web"}}, {"source": "public_web"}],
    ids=["missing", "generic-provider", "generic-kind"],
)
def test_unknown_provenance_preserves_individual_multiplicity(provenance):
    first = quote(1000, **provenance)
    assert_price(calculate([first, deepcopy(first), independent_quote()]), 1000, [1000, 3000])


def test_boolean_aliases_do_not_create_eligible_observations():
    invalid = [
        quote(1000, source=SOURCE, includes_parts="false"),
        quote(1000, source=SOURCE, labor_only="true"),
        quote(1000, source=SOURCE, includes_parts=0),
        quote(1000, source=SOURCE, labor_only=1),
    ]
    assert_price(calculate([*invalid, independent_quote()]), 3000, [3000, 3000])


def test_zero_and_fractional_observations_retain_unrounded_amount_identity():
    observations = [quote(0, source=SOURCE), quote(0.25, source=SOURCE), independent_quote(0.5)]
    assert_price(calculate(observations, policy={**POLICY, "rounding": 0.01}), 0.25, [0, 0.5])


def test_hourly_rate_and_unknown_expense_keep_their_existing_behavior():
    response = calculate(
        [quote(1000, source=SOURCE)],
        labor=[{"operation_id": "work", "operation_name": OPERATION, "hours": 2.5}],
        policy={"basis": "hourly_rate", "version": "identity-test-v1"},
        hourly_rate=1200,
        unknown_costs=["diagnostic"],
    )
    assert response["outcome"] == "partial"
    assert response["missing_fields"] == ["diagnostic"]
    data = response["data"]
    assert data["known_subtotal"] == data["operations"][0]["price"] == 3000
    assert data["operations"][0]["range"] == [3000, 3000]
    assert data["total"] is None and data["range"] is None


def test_qualified_negative_scope_cannot_veto_a_different_strict_operation_identity():
    operation = "замена тормозных колодок"
    generic = quote(1000, source=SOURCE, operation_name=operation)
    qualified = {
        **deepcopy(generic),
        "operation_name": "замена передних тормозных колодок",
        "labor_only": False,
    }
    second = independent_quote(operation_name=operation)
    assert_price(calculate([generic, qualified, second], operation=operation), 2000, [1000, 3000])
