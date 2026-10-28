"""Resolve observed flat TecDoc nodes without asserting vehicle fitment."""

from copy import deepcopy

import pytest

from autostop_manager.automotive_contracts import validate_catalog_context
from autostop_manager.automotive_parts import resolve_catalog_group


MODIFICATION = {
    "provider": "partsapi_ru",
    "namespace": "tecdoc",
    "entity_kind": "modification",
    "id": "13895",
    "carType": "PC",
}

# Two de-identified public nodes from a larger provider tree.  This fixture is
# only a test tree and must never be treated as evidence of actual fitment.
ROOT = {
    "STR_ID": 100005,
    "STR_ID_PARENT": None,
    "STR_LEVEL": 1,
    "STR_NODE_NAME": "Фильтр",
    "STR_PATH": "Фильтр",
}
LEAF = {
    "STR_ID": 100259,
    "STR_ID_PARENT": 100005,
    "STR_LEVEL": 2,
    "STR_NODE_NAME": "Масляный фильтр",
    "STR_PATH": "Фильтр > Масляный фильтр",
}


def _resolve(rows, selected=None, intent="масляный фильтр"):
    return resolve_catalog_group(
        {"modification": MODIFICATION, "rows": rows}, intent, MODIFICATION, selected_node_id=selected
    )


@pytest.mark.parametrize("selected", [None, 100259])
def test_observed_flat_leaf_binds_to_the_tree_and_modification(selected):
    rows = [deepcopy(ROOT), deepcopy(LEAF)]
    response = _resolve(rows, selected)
    assert response["outcome"] == "success"
    assert response["data"]["category"] == "100259"
    assert response["data"]["category_queryable"] is True
    node = response["data"]["nodes"][0]
    assert node["parent"] == MODIFICATION
    assert node["tree_sha256"] == response["data"]["tree_sha256"]
    assert (
        validate_catalog_context(
            "articles",
            {"carId": "13895", "carType": "PC", "strId": "100259"},
            {"modification": MODIFICATION, "tree_node": node},
        )
        == []
    )
    assert response["execution"]["network_calls"] == 0


def test_flat_parent_is_not_automatically_selected_but_can_be_explicitly_selected():
    rows = [ROOT, LEAF]
    assert _resolve(rows, intent="Фильтр")["data"]["category_queryable"] is False
    chosen = _resolve(rows, selected=100005, intent="Фильтр")
    assert chosen["data"]["category"] == "100005"
    assert chosen["data"]["category_mode"] == "explicit_tree_node"


def test_duplicate_leaf_names_are_ambiguous_until_an_actual_id_is_selected():
    rows = [ROOT, LEAF, {**LEAF, "STR_ID": 100470}]
    unresolved = _resolve(rows)
    assert unresolved["outcome"] == "partial"
    assert unresolved["data"]["candidate_node_ids"] == ["100259", "100470"]
    assert unresolved["data"]["category_queryable"] is False
    assert _resolve(rows, selected=100470)["data"]["category"] == "100470"
    assert _resolve(rows, selected=999999)["data"]["category_queryable"] is False


def test_identical_flat_duplicate_does_not_change_the_candidate():
    row = _resolve([ROOT, LEAF, deepcopy(LEAF)])
    assert row["data"]["category"] == "100259"


@pytest.mark.parametrize(
    "corrupt",
    [
        {**LEAF, "STR_NODE_NAME": "Воздушный фильтр"},
        {**LEAF, "STR_ID_PARENT": None},
        {**LEAF, "STR_LEVEL": 3},
        {**LEAF, "STR_PATH": "Противоречащий путь"},
    ],
)
def test_conflicting_duplicate_id_never_picks_a_first_record(corrupt):
    response = _resolve([ROOT, LEAF, corrupt], selected=100259)
    assert response["outcome"] == "partial"
    assert response["data"]["category_queryable"] is False
    assert response["data"]["nodes"] == []


@pytest.mark.parametrize(
    "rows",
    [
        [LEAF],
        [ROOT, {**LEAF, "STR_ID_PARENT": 100259}],
        [ROOT, {**LEAF, "STR_ID_PARENT": 999999}],
        [ROOT, {**LEAF, "STR_LEVEL": 4}],
        [ROOT, {**LEAF, "STR_NODE_NAME": ""}],
        [ROOT, {**LEAF, "STR_ID": True}],
        [{**ROOT, "STR_ID_PARENT": 100259, "STR_LEVEL": 2}, LEAF],
    ],
)
def test_missing_parent_and_malformed_flat_nodes_fail_closed(rows):
    response = _resolve(rows, selected=100259)
    assert response["outcome"] == "partial"
    assert response["data"]["category_queryable"] is False


def test_mixed_legacy_and_flat_tree_preserves_consistent_node_and_conflicts():
    matching = {
        "ROOT_NODE_TEXT": "Фильтр",
        "ROOT_NODE_STR_ID": 100005,
        "NODE_1_TEXT": "Масляный фильтр",
        "NODE_1_STR_ID": 100259,
    }
    good = _resolve([ROOT, LEAF, matching], selected=100259)
    assert good["data"]["category"] == "100259"
    bad_name = {**matching, "NODE_1_TEXT": "Воздушный фильтр"}
    assert _resolve([ROOT, LEAF, bad_name], selected=100259)["data"]["category_queryable"] is False
    assert _resolve([ROOT, LEAF, matching, bad_name], selected=100259)["data"]["category_queryable"] is False
    assert _resolve([ROOT, LEAF, bad_name, matching], selected=100259)["data"]["category_queryable"] is False
    bad_parent = {**matching, "ROOT_NODE_STR_ID": 999}
    assert _resolve([ROOT, LEAF, bad_parent], selected=100259)["data"]["category_queryable"] is False


def test_existing_legacy_tree_keeps_explicit_ancestor_and_auto_leaf():
    rows = [
        {
            "STR_LEVEL": 2,
            "ROOT_NODE_TEXT": "Фильтр",
            "ROOT_NODE_STR_ID": 100005,
            "NODE_1_TEXT": "Масляный фильтр",
            "NODE_1_STR_ID": 100259,
        }
    ]
    assert _resolve(rows)["data"]["category"] == "100259"
    assert _resolve(rows, selected=100005)["data"]["category"] == "100005"


@pytest.mark.parametrize(
    "partial",
    [
        {"STR_ID": 100259},
        {"STR_NODE_NAME": "Масляный фильтр"},
        {"STR_LEVEL": 2},
        {"STR_ID_PARENT": 1},
        {"STR_ID_PARENT": 1, "NODE_3_TEXT": "Масляный фильтр"},
    ],
)
def test_partial_flat_rows_do_not_fall_back_to_a_legacy_match(partial):
    legacy = {"NODE_3_TEXT": "Масляный фильтр", "NODE_3_STR_ID": 100259}
    assert _resolve([legacy, partial], selected=100259)["data"]["category_queryable"] is False


@pytest.mark.parametrize("partial", [{"STR_ID": 100259}, {"STR_NODE_NAME": "Масляный фильтр"}])
def test_partial_flat_fields_cannot_be_hidden_inside_a_legacy_row(partial):
    legacy = {"NODE_3_TEXT": "Масляный фильтр", "NODE_3_STR_ID": 100259}
    assert _resolve([{**legacy, **partial}], selected=100259)["data"]["category_queryable"] is False


def test_legacy_row_with_parent_metadata_keeps_legacy_selection():
    legacy = {"NODE_3_TEXT": "Масляный фильтр", "NODE_3_STR_ID": 100259, "STR_ID_PARENT": 1}
    assert _resolve([legacy], selected=100259)["data"]["category"] == "100259"


def test_mixed_tree_does_not_treat_flat_root_as_a_leaf_when_legacy_row_has_child():
    legacy = {
        "ROOT_NODE_TEXT": "Фильтр",
        "ROOT_NODE_STR_ID": 100005,
        "NODE_1_TEXT": "Масляный фильтр",
        "NODE_1_STR_ID": 100259,
    }
    for rows in ([ROOT, legacy], [legacy, ROOT]):
        assert _resolve(rows, intent="Фильтр")["data"]["category_queryable"] is False
        assert _resolve(rows, selected=100005, intent="Фильтр")["data"]["category"] == "100005"
        assert _resolve(rows)["data"]["category"] == "100259"


def test_legacy_root_is_not_auto_selected_when_another_row_contains_a_child():
    root_only = {"ROOT_NODE_TEXT": "Фильтр", "ROOT_NODE_STR_ID": 100005}
    path = {**root_only, "NODE_1_TEXT": "Масляный фильтр", "NODE_1_STR_ID": 100259}
    for rows in ([root_only, path], [path, root_only]):
        assert _resolve(rows, intent="Фильтр")["data"]["category_queryable"] is False


def test_leading_zero_aliases_cannot_hide_conflicting_flat_or_legacy_nodes():
    conflicting = {**LEAF, "STR_ID": "0100259", "STR_NODE_NAME": "Воздушный фильтр"}
    for rows in ([ROOT, LEAF, conflicting], [ROOT, conflicting, LEAF]):
        assert _resolve(rows, selected=100259)["data"]["category_queryable"] is False
    legacy_conflicting = {"NODE_3_TEXT": "Воздушный фильтр", "NODE_3_STR_ID": "0100259"}
    assert _resolve([ROOT, LEAF, legacy_conflicting], selected=100259)["data"]["category_queryable"] is False


def test_matching_leading_zero_aliases_resolve_to_one_canonical_id():
    matching = {**LEAF, "STR_ID": "0100259", "STR_ID_PARENT": "0100005"}
    assert _resolve([ROOT, LEAF, matching], selected="0100259")["data"]["category"] == "100259"


@pytest.mark.parametrize("field", ["STR_ID", "STR_ID_PARENT", "STR_LEVEL"])
def test_unreasonably_long_flat_numeric_fields_fail_closed_without_an_exception(field):
    too_long = {**LEAF, field: "9" * 5000}
    assert _resolve([ROOT, too_long], selected=100259)["data"]["category_queryable"] is False


def test_unreasonably_long_selected_and_legacy_ids_do_not_raise():
    assert _resolve([ROOT, LEAF], selected="9" * 5000)["data"]["category_queryable"] is False
    legacy = {"NODE_3_TEXT": "Масляный фильтр", "NODE_3_STR_ID": "9" * 5000}
    assert _resolve([legacy], selected="9" * 5000)["data"]["category_queryable"] is False


def test_legacy_duplicate_id_with_conflicting_names_never_selects_first():
    first = {"NODE_3_TEXT": "Масляный фильтр", "NODE_3_STR_ID": 100259}
    second = {"NODE_3_TEXT": "Воздушный фильтр", "NODE_3_STR_ID": "0100259"}
    for rows in ([first, second], [second, first]):
        assert _resolve(rows)["data"]["category_queryable"] is False
        assert _resolve(rows, selected=100259)["data"]["category_queryable"] is False


@pytest.mark.parametrize("node_type", ["CV", "Motorcycle"])
def test_article_context_rejects_node_vehicle_type_from_another_catalog(node_type):
    node = _resolve([ROOT, LEAF], selected=100259)["data"]["nodes"][0]
    wrong_type = {**node, "carType": node_type}
    context = {"modification": MODIFICATION, "tree_node": wrong_type}
    assert validate_catalog_context("articles", {"carId": "13895", "carType": "PC", "strId": "100259"}, context) == [
        "tree_node_reference_mismatch"
    ]
    assert validate_catalog_context("articles", {"carId": "13895", "strId": "100259"}, context) == [
        "tree_node_reference_mismatch"
    ]


@pytest.mark.parametrize("leaf_level", [1, 3])
def test_legacy_node_with_two_different_roots_is_not_confirmed(leaf_level):
    rows = [
        {
            "ROOT_NODE_TEXT": "Первая группа",
            "ROOT_NODE_STR_ID": 1,
            f"NODE_{leaf_level}_TEXT": "Масляный фильтр",
            f"NODE_{leaf_level}_STR_ID": 2,
        },
        {
            "ROOT_NODE_TEXT": "Другая группа",
            "ROOT_NODE_STR_ID": 3,
            f"NODE_{leaf_level}_TEXT": "Масляный фильтр",
            f"NODE_{leaf_level}_STR_ID": 2,
        },
    ]
    for ordered in (rows, list(reversed(rows))):
        assert _resolve(ordered)["data"]["category_queryable"] is False
        assert _resolve(ordered, selected=2)["data"]["category_queryable"] is False


@pytest.mark.parametrize("leaf_level", [1, 3])
def test_legacy_cycle_is_not_confirmed_even_when_levels_are_missing(leaf_level):
    rows = [
        {
            "ROOT_NODE_TEXT": "Фильтр",
            "ROOT_NODE_STR_ID": 1,
            f"NODE_{leaf_level}_TEXT": "Масляный фильтр",
            f"NODE_{leaf_level}_STR_ID": 2,
        },
        {
            "ROOT_NODE_TEXT": "Масляный фильтр",
            "ROOT_NODE_STR_ID": 2,
            f"NODE_{leaf_level}_TEXT": "Фильтр",
            f"NODE_{leaf_level}_STR_ID": 1,
        },
    ]
    assert _resolve(rows, selected=2)["data"]["category_queryable"] is False


@pytest.mark.parametrize("leaf_level", [1, 3])
def test_legacy_self_link_is_not_confirmed_even_when_levels_are_missing(leaf_level):
    row = {
        "ROOT_NODE_TEXT": "Масляный фильтр",
        "ROOT_NODE_STR_ID": 2,
        f"NODE_{leaf_level}_TEXT": "Масляный фильтр",
        f"NODE_{leaf_level}_STR_ID": 2,
    }
    assert _resolve([row], selected=2)["data"]["category_queryable"] is False


def test_single_legacy_leaf_remains_supported_without_ancestors():
    row = {"NODE_3_TEXT": "Масляный фильтр", "NODE_3_STR_ID": 2}
    assert _resolve([row])["data"]["category"] == "2"


def test_partial_and_complete_legacy_paths_with_consistent_ancestors_remain_usable():
    partial = {"ROOT_NODE_TEXT": "Фильтр", "ROOT_NODE_STR_ID": 1, "NODE_3_TEXT": "Масляный фильтр", "NODE_3_STR_ID": 2}
    expanded = {**partial, "NODE_1_TEXT": "Промежуточная группа", "NODE_1_STR_ID": 3}
    assert _resolve([partial, expanded], selected=2)["data"]["category"] == "2"


def test_mixed_flat_and_legacy_cycle_is_not_confirmed():
    flat_root = {"STR_ID": 1, "STR_ID_PARENT": None, "STR_LEVEL": 1, "STR_NODE_NAME": "Фильтр"}
    rows = [
        flat_root,
        {"ROOT_NODE_TEXT": "Фильтр", "ROOT_NODE_STR_ID": 1, "NODE_1_TEXT": "Масляный фильтр", "NODE_1_STR_ID": 2},
        {"ROOT_NODE_TEXT": "Масляный фильтр", "ROOT_NODE_STR_ID": 2, "NODE_1_TEXT": "Фильтр", "NODE_1_STR_ID": 1},
    ]
    assert _resolve(rows, selected=2)["data"]["category_queryable"] is False


def test_mixed_flat_root_cannot_have_a_different_legacy_ancestor_across_a_gap():
    flat_root = {"STR_ID": 1, "STR_ID_PARENT": None, "STR_LEVEL": 1, "STR_NODE_NAME": "Фильтр"}
    legacy = {"ROOT_NODE_TEXT": "Другая группа", "ROOT_NODE_STR_ID": 3, "NODE_3_TEXT": "Фильтр", "NODE_3_STR_ID": 1}
    assert _resolve([flat_root, legacy], selected=1, intent="Фильтр")["data"]["category_queryable"] is False
