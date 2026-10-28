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
