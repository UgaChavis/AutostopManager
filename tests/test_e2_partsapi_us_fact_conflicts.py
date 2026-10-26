"""Conflicting known facts from same-VIN US cards must not select a winner."""

from io import BytesIO
import json
import socket
import sqlite3

import pytest

from autostop_manager import catalog_clients as catalog, vehicle_identity as identity

REQUESTED = "A" * 17
FOREIGN = "B" * 17
CONFLICTS = {
    "Make": "OTHER",
    "Model": "Other Model",
    "ModelYear": "2021",
    "EngineModel": "OTHER2",
    "Trim": "Other Trim",
}


def vehicle(shape="record", identifier=REQUESTED, **fields):
    row = {
        "Make": "SYNTHETIC",
        "Model": "Example Model",
        "ModelYear": "2020",
        "EngineModel": "TEST1",
        "Trim": "Synthetic Trim",
        "ErrorCode": "0",
        "ErrorText": "0 - synthetic",
        **fields,
    }
    if identifier is not None:
        row["VIN"] = identifier
    if shape == "flat":
        return {**row, "source": "NHTSA vPIC"}
    if shape == "variables":
        return {"Results": [{"Variable": key, "Value": value} for key, value in row.items()]}
    return {"Results": [row]}


@pytest.fixture
def lookup(monkeypatch):
    monkeypatch.setattr(
        catalog,
        "_partsapi_credentials",
        lambda: {"base_url": "https://partsapi.example.test/api", "key_param": "key", "method_param": "method"},
    )
    monkeypatch.setattr(catalog, "_partsapi_method_key", lambda _method: ("synthetic-key", "SYNTHETIC_KEY"))

    def forbidden(*_args, **_kwargs):
        pytest.fail("US fact conflict test attempted network or SQLite access")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(catalog, "bounded_urlopen", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)

    def run(payload, operation="decodeVINus"):
        calls = []

        def respond(_request, **_kwargs):
            calls.append(True)
            return BytesIO(json.dumps(payload).encode("utf-8"))

        monkeypatch.setattr(catalog, "urlopen", respond)
        response = catalog.partsapi_catalog_lookup(operation=operation, identifier=REQUESTED, max_attempts=1)
        assert len(calls) == 1
        if operation == "decodeVINus":
            assert response["outcome"] != "group_match"
            assert response.get("binding_kind") != "provider_group_reference"
            assert all(profile["fitment_confirmed"] is False for profile in response["vehicle_profiles"])
        return response

    return run


def assert_conflict(response):
    assert response["ok"] is False
    assert response["outcome"] == "unparsed_response"
    assert response["failure_class"] == "adapter_unparsed_response"
    assert response["vehicle_profiles"] == []
    assert response["requires_fallback"] is True


@pytest.mark.parametrize("shape", ["flat", "record", "variables"])
@pytest.mark.parametrize("field", CONFLICTS)
@pytest.mark.parametrize("reverse", [False, True])
def test_same_vin_common_known_conflicts_reject_without_selecting_first(lookup, shape, field, reverse):
    first, second = vehicle(shape), vehicle(shape, **{field: CONFLICTS[field]})
    if reverse:
        first, second = second, first
    assert_conflict(lookup({"data": first, "result": second}))


@pytest.mark.parametrize("shape", ["flat", "record", "variables"])
@pytest.mark.parametrize("field", CONFLICTS)
def test_real_shared_parent_vin_binds_both_conflicting_cards(lookup, shape, field):
    assert_conflict(
        lookup(
            {
                "Vin": REQUESTED,
                "data": vehicle(shape, None),
                "result": vehicle(shape, None, **{field: CONFLICTS[field]}),
            }
        )
    )


@pytest.mark.parametrize(
    "wrapper_fields",
    [{"Results": "ignored"}, {"Results": []}, {"Results": [0, 1]}, {"Trim": "wrapper-only"}, {"ModelYear": "2020"}],
)
def test_ignored_wrapper_fields_do_not_hide_shared_ancestor_binding(lookup, wrapper_fields):
    response = lookup(
        {
            "VIN": REQUESTED,
            **wrapper_fields,
            "data": vehicle(identifier=None),
            "result": vehicle(identifier=None, Make="OTHER"),
        }
    )
    assert_conflict(response)


@pytest.mark.parametrize("sibling", ["data", "result", "array", "items"])
def test_nested_recognized_card_conflict_survives_wrapper_choice(lookup, sibling):
    response = lookup({**vehicle("flat"), sibling: {"VIN": REQUESTED, "result": vehicle(Make="OTHER")}})
    assert_conflict(response)


def test_nested_wrapper_vin_binds_only_its_own_conflicting_card(lookup):
    response = lookup(
        {**vehicle("flat"), "items": {"VIN": REQUESTED, "result": vehicle(identifier=None, Make="OTHER")}}
    )
    assert_conflict(response)


@pytest.mark.parametrize("reverse", [False, True])
def test_all_known_values_are_compared_even_when_family_names_are_compatible(lookup, reverse):
    siblings = [vehicle(Model="Grand Cherokee WK2"), vehicle(Model="Grand Cherokee WL")]
    if reverse:
        siblings.reverse()
    assert_conflict(lookup({**vehicle("flat", Model="Grand Cherokee"), "items": siblings}))


def test_all_engine_representations_remain_compared_despite_equal_compact_text(lookup):
    response = lookup(
        {
            **vehicle("flat", EngineModel="TE ST1"),
            "items": [vehicle(EngineModel="TEST1"), vehicle(EngineModel="Engine ST1")],
        }
    )
    assert_conflict(response)


def variant_cards(count):
    return [
        vehicle(
            Trim=("Synthetic Trim" if i % 2 else "sYNTHETIC tRIM")
            + format(i, "08b").translate(str.maketrans("01", "_-"))
        )
        for i in range(count)
    ]


def test_many_distinct_equivalent_representations_fail_closed_with_bounded_comparisons(lookup, monkeypatch):
    calls = []
    original = identity.identity_values_agree

    def measured(field, left, right):
        calls.append(field)
        return original(field, left, right)

    monkeypatch.setattr(identity, "identity_values_agree", measured)
    cards = variant_cards(128)
    response = lookup({"data": cards[0], "items": cards})
    assert_conflict(response)
    # The public lookup evaluates US semantics twice; each validator is bounded.
    assert len(calls) <= 8192


def test_reasonable_equivalent_representations_still_succeed(lookup):
    cards = variant_cards(64)
    assert lookup({"data": cards[0], "items": cards})["outcome"] == "success"


@pytest.mark.parametrize(("count", "expected"), [(3, "success"), (4, "unparsed_response")])
def test_comparison_budget_boundary_preserves_actual_pairwise_semantics(lookup, monkeypatch, count, expected):
    monkeypatch.setattr(catalog, "_PARTSAPI_US_FACT_COMPARISON_LIMIT", 3, raising=False)
    cards = variant_cards(count)
    response = lookup({"data": cards[0], "items": cards})
    assert response["outcome"] == expected


def test_large_identical_nonlatin_duplicates_do_not_spend_comparison_budget(lookup, monkeypatch):
    def unexpected_comparison(*_args):
        pytest.fail("exact duplicate facts do not need semantic comparisons")

    monkeypatch.setattr(identity, "identity_values_agree", unexpected_comparison)
    cards = [vehicle(Trim="零", EngineModel="電動") for _ in range(256)]
    assert lookup({"data": cards[0], "items": cards})["outcome"] == "success"


@pytest.mark.parametrize("shape", ["flat", "record", "variables"])
def test_identical_full_cards_remain_success(lookup, shape):
    response = lookup({"data": vehicle(shape), "result": vehicle(shape)})
    assert response["outcome"] == "success"
    assert response["identifier_matches_request"] is True


@pytest.mark.parametrize("shape", ["flat", "record", "variables"])
@pytest.mark.parametrize(
    ("field", "value"), [("Make", "製造"), ("Model", "型式"), ("EngineModel", "電動"), ("Trim", "零")]
)
@pytest.mark.parametrize("padding", ["", " \t "])
def test_identical_nonlatin_facts_remain_success_before_semantic_comparison(lookup, shape, field, value, padding):
    response = lookup(
        {"data": vehicle(shape, **{field: value}), "result": vehicle(shape, **{field: padding + value + padding})}
    )
    assert response["outcome"] == "success"
    assert response["identifier_matches_request"] is True


@pytest.mark.parametrize("shape", ["flat", "record", "variables"])
@pytest.mark.parametrize(
    ("field", "first", "second"),
    [("Make", "製造", "他社"), ("Model", "型式", "別型"), ("EngineModel", "電動", "柴油"), ("Trim", "零", "一")],
)
def test_different_known_nonlatin_facts_still_conflict(lookup, shape, field, first, second):
    response = lookup({"data": vehicle(shape, **{field: first}), "result": vehicle(shape, **{field: second})})
    assert_conflict(response)


@pytest.mark.parametrize(
    ("field", "compatible"),
    [
        ("Make", " synthetic "),
        ("Model", " example   model "),
        ("ModelYear", 2020),
        ("EngineModel", "Engine TEST1"),
        ("Trim", " synthetic   trim "),
    ],
)
def test_existing_identity_comparator_accepts_compatible_values(lookup, field, compatible):
    response = lookup({"data": vehicle(), "result": vehicle(**{field: compatible})})
    assert response["outcome"] == "success"


@pytest.mark.parametrize("field", CONFLICTS)
@pytest.mark.parametrize("unknown", [None, "", "  ", "unknown", " Not Applicable ", "NULL"])
def test_empty_or_unknown_sibling_fact_does_not_create_a_conflict(lookup, field, unknown):
    response = lookup({"data": vehicle(), "result": vehicle(**{field: unknown})})
    assert response["outcome"] == "success"


@pytest.mark.parametrize("field", CONFLICTS)
def test_complementary_facts_do_not_merge_or_upgrade_the_selected_card(lookup, field):
    response = lookup({"data": vehicle(**{field: None}), "result": vehicle()})
    assert response["outcome"] == "partial_result"
    assert response["requires_fallback"] is True
    assert len(response["vehicle_profiles"]) == 1


@pytest.mark.parametrize("sibling", ["data", "result", "array", "items"])
def test_unbound_competing_card_does_not_contribute_conflicting_facts(lookup, sibling):
    response = lookup({**vehicle("flat"), sibling: vehicle(identifier=None, Make="OTHER")})
    assert response["outcome"] == "success"


@pytest.mark.parametrize("echo", ["request", "params", "metadata", "echo", "requestParams", "requestParameters"])
def test_echo_full_card_is_not_used_for_fact_comparison(lookup, echo):
    response = lookup({**vehicle("flat"), echo: vehicle(Make="OTHER")})
    assert response["outcome"] == "success"


@pytest.mark.parametrize("shape", ["flat", "record", "variables"])
def test_foreign_full_vin_mismatch_keeps_priority_over_fact_conflict(lookup, shape):
    response = lookup(
        {
            **vehicle(shape),
            "data": vehicle(shape, Make="OTHER"),
            "items": [vehicle(shape, identifier=FOREIGN, Make="THIRD")],
        }
    )
    assert response["outcome"] == "identifier_mismatch"
    assert response["identifier_matches_request"] is False
    assert response["vehicle_profiles"] == []


def test_nonzero_selected_diagnostics_remain_partial_without_a_fact_conflict(lookup):
    response = lookup({"data": vehicle(ErrorCode="1"), "result": vehicle()})
    assert response["outcome"] == "partial_result"
    assert response["provider_diagnostics"]["error_codes"] == ["1"]


def test_fact_conflict_keeps_selected_diagnostics(lookup):
    response = lookup({"data": vehicle(ErrorCode="1"), "result": vehicle(Make="OTHER")})
    assert_conflict(response)
    assert response["provider_diagnostics"]["error_codes"] == ["1"]
    assert response["provider_diagnostics"]["has_error_text"] is True
    assert response["provider_diagnostics"]["diagnostics_status"] == "reported"


def test_diagnostic_only_selected_card_does_not_acquire_sibling_facts_or_conflicts(lookup):
    response = lookup(
        {
            "VIN": REQUESTED,
            "source": "NHTSA vPIC",
            "ErrorCode": "0",
            "ErrorText": "0 - synthetic",
            "items": [vehicle(), vehicle(Make="OTHER")],
        }
    )
    assert response["outcome"] == "empty_result"
    assert response["vehicle_profiles"] == []


def test_bound_sibling_conflicts_do_not_bind_or_reject_an_unbound_selected_card(lookup):
    response = lookup({**vehicle("flat", identifier=None), "items": [vehicle(), vehicle(Make="OTHER")]})
    assert response["outcome"] == "partial_result"
    assert response["identifier_matches_request"] is None


@pytest.mark.parametrize("diagnostics", [{}, {"ErrorCode": True, "ErrorText": "synthetic"}])
def test_unrecognized_or_malformed_diagnostic_sibling_is_not_a_fact_source(lookup, diagnostics):
    sibling = {"VIN": REQUESTED, "Make": "OTHER", "source": "NHTSA vPIC", **diagnostics}
    response = lookup({**vehicle("flat"), "items": [sibling]})
    assert response["outcome"] == "success"


def test_vindecode_group_representative_policy_remains_separate(lookup):
    response = lookup(
        {"vin": FOREIGN, "carId": "123", "carType": "PC", "manuName": "SYNTHETIC", "modelName": "Example"},
        operation="vin_decode",
    )
    assert response["outcome"] == "group_match"
    assert response["identifier_matches_request"] is False
    assert catalog.partsapi_identifier_allows_candidate_lookup(response) is True
