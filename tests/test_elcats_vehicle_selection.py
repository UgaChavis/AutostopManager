"""Public HTML vehicle-selection constraints survive independently reusable refs."""

from copy import deepcopy
import json
import socket
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from autostop_manager import automotive_parts, elcats_catalog as catalog
from autostop_manager.automotive_contracts import MAX_ROWS, public_oem_catalog_ref, same_oem_catalog_ref
from autostop_manager.public_catalog_http import CatalogPage, CatalogReadError


IDENTITY = {"vehicle_profile": {"make": "Volkswagen", "model": "Golf"}}
PART = {"raw": "передние тормозные колодки"}
ROOT = "https://www.elcats.ru/vw/"


def _row(href, label, notes=""):
    return f'<table><tr><td><a href="{href}">{label}</a></td><td>{notes}</td></tr></table>'


def _shared_selection_html(url):
    if url.path == "/vw/":
        return _row("Models.aspx?Code=LEFT&Title=Golf", "Golf", "PR:2E4; лев.") + _row(
            "Models.aspx?Code=RIGHT&Title=Golf", "Golf", "PR:1ZE; прав."
        )
    if url.path == "/vw/Models.aspx":
        return _row("Modifications.aspx?Code=SHARED&Title=Golf", "Golf")
    assert url.path == "/vw/Modifications.aspx"
    return _row("Groups.aspx?Mdl=SHARED&Title=Golf", "Golf")


@pytest.fixture
def install_reader(monkeypatch):
    def forbid(*_args, **_kwargs):
        raise AssertionError("vehicle-selection fixtures must not access a real provider")

    monkeypatch.setattr(socket, "getaddrinfo", forbid)
    monkeypatch.setenv("AUTOSTOP_ELCATS_ENABLED", "1")

    def install(selection_html):
        class Reader:
            def __init__(self, *, page_budget, deadline_seconds):
                self.page_budget = page_budget
                self.deadline = time.monotonic() + deadline_seconds
                self.network_calls = 0
                self.attempts = []
                self.pages = {}

            def read(self, url, *, route_guard=None):
                if route_guard:
                    route_guard(url)
                if url in self.pages:
                    return self.pages[url]
                if self.network_calls >= self.page_budget:
                    raise CatalogReadError("catalog_page_budget_exceeded")
                self.network_calls += 1
                self.attempts.append({"url": url, "method": "GET", "status": 200})
                parsed = urlsplit(url)
                model = parse_qs(parsed.query).get("Mdl", ["DEMO"])[0]
                if parsed.path == "/vw/Groups.aspx":
                    html = _row(f"Subgroup.aspx?Mdl={model}&GroupId=brakes", "ТОРМОЗНАЯ СИСТЕМА")
                elif parsed.path == "/vw/Subgroup.aspx":
                    html = _row(f"Parts.aspx?Mdl={model}&SubId=pads", "ПЕРЕДНИЕ ТОРМОЗА")
                elif parsed.path == "/vw/Parts.aspx":
                    html = (
                        "<table><tr><th>Код детали</th><th>Наименование</th></tr>"
                        "<tr><td>DEMO100</td><td>PAD SET-FRT BRAKE</td></tr></table>"
                    )
                else:
                    html = selection_html(parsed)
                self.pages[url] = CatalogPage(url, html, 200)
                return self.pages[url]

        monkeypatch.setattr(catalog, "PublicCatalogReader", Reader)
        return Reader

    return install


def _roundtrip(value):
    return json.loads(json.dumps(value))


def _standalone_candidate(modification):
    assert public_oem_catalog_ref(modification)
    assert modification.get("parent_ref") is None
    groups = catalog.elcats_catalog_query("list_groups", deepcopy(IDENTITY), catalog_ref=_roundtrip(modification))
    group = groups["data"]["nodes"][0]["catalog_ref"]
    diagrams = catalog.elcats_catalog_query("list_diagrams", deepcopy(IDENTITY), catalog_ref=_roundtrip(group))
    diagram = diagrams["data"]["nodes"][0]["catalog_ref"]
    return catalog.elcats_catalog_query("list_parts", deepcopy(IDENTITY), catalog_ref=_roundtrip(diagram))["data"][
        "candidates"
    ][0]


def _assess_with_independent_primary(candidate):
    modification = candidate["modification_ref"]
    assert public_oem_catalog_ref(modification)
    assert modification.get("parent_ref") is None
    primary = {
        "provider": "demo_manufacturer",
        "primary_lineage": "demo_independent_oem",
        "method": "supplied_document",
        "locator": "https://example.com/epc",
        "fetched_at": "2026-01-01T00:00:00Z",
        "document_kind": "official_epc",
        "fitment_assertion": True,
        "part_number": "DEMO100",
        "brand": "Volkswagen",
        "scope": "modification",
        "catalog_ref": modification,
    }
    vehicle = {**deepcopy(IDENTITY), "catalog_ref": modification}
    response = automotive_parts.assess_part_fitment(vehicle, candidate, {}, [primary, candidate["source"]])
    assert candidate["oem_confirmed"] is candidate["fitment_confirmed"] is False
    assert candidate["source"]["identifier_verified"] is False
    assert response["execution"]["network_calls"] == 0
    return response


@pytest.mark.parametrize("notes", ["PR:2E4", "2010 - 2018", "PR:2E4; 2010 - 2018"])
def test_terminal_modification_constraints_reach_both_workflows_and_e6(install_reader, notes):
    install_reader(lambda _url: _row("Groups.aspx?Mdl=DEMO&Title=Golf", "Golf", notes))
    resolved = catalog.elcats_catalog_query("resolve_vehicle", deepcopy(IDENTITY))
    modification = _roundtrip(resolved["data"]["nodes"][0])
    assert public_oem_catalog_ref(modification)
    assert modification.get("parent_ref") is None
    clause = modification["inherited_conditions"][0]
    assert notes in clause["raw_conditions"]
    assert clause["source_url"] == ROOT
    assert same_oem_catalog_ref(clause["catalog_ref"], modification)
    if "PR" in notes:
        assert clause["conditions"]["pr_codes"]["all_of"] == ["2E4"]
    if "2010" in notes:
        assert any(notes in raw for raw in modification["inherited_restrictions"])
        assert any(notes in raw for raw in clause["unparsed_conditions"])
    sequential = _roundtrip(_standalone_candidate(modification))
    lookup = catalog.elcats_catalog_query("lookup_candidates", deepcopy(IDENTITY), deepcopy(PART))["data"][
        "candidates"
    ][0]
    for field in ("inherited_conditions", "inherited_restrictions"):
        assert sequential[field] == lookup[field]
        assert sequential["source"][field] == modification[field]
    assert _assess_with_independent_primary(sequential)["data"]["state"] == "unknown"
    assert _assess_with_independent_primary(lookup)["data"]["state"] == "unknown"


@pytest.mark.parametrize("first_notes", ["PR:2E4", "2010 - 2018"])
def test_intermediate_pr_year_and_terminal_date_remain_bound_to_their_selection_pages(install_reader, first_notes):
    def html(url):
        if url.path == "/vw/":
            return _row("Models.aspx?Code=DEMO&Title=Golf", "Golf", first_notes)
        if url.path == "/vw/Models.aspx":
            return '<table><tr><td><h3>Golf</h3><a href="Modifications.aspx?Code=YEAR">2018</a></td></tr></table>'
        assert url.path == "/vw/Modifications.aspx"
        return _row("Groups.aspx?Mdl=DEMO&Title=Golf", "Golf", "2010 - 2018")

    install_reader(html)
    resolved = catalog.elcats_catalog_query("resolve_vehicle", deepcopy(IDENTITY))
    modification = _roundtrip(resolved["data"]["nodes"][0])
    assert public_oem_catalog_ref(modification) and modification.get("parent_ref") is None
    clauses = modification["inherited_conditions"]
    if "PR" in first_notes:
        assert clauses[0]["conditions"]["pr_codes"]["all_of"] == ["2E4"]
    else:
        assert clauses[0]["conditions"] == {} and clauses[0]["unparsed_conditions"]
    assert clauses[0]["source_url"] == ROOT
    assert clauses[1]["conditions"] == {"model_year": 2018}
    assert clauses[1]["source_url"].startswith(ROOT + "Models.aspx?")
    for clause in clauses[:2]:
        assert clause["binding"] == "vehicle_selection"
        assert clause["scope"] == "family" and clause["identifier_verified"] is False
        assert "catalog_ref" not in clause  # Selection provenance is not a fabricated modification reference.
        assert clause["row_id"] and clause["selection_url"]
    assert len(clauses) == 3
    terminal = clauses[-1]
    assert terminal["conditions"] == {} and terminal["unparsed_conditions"]
    assert terminal["source_url"].startswith(ROOT + "Modifications.aspx?")
    assert terminal["binding"] == "modification"
    assert same_oem_catalog_ref(terminal["catalog_ref"], modification)
    assert any("2010 - 2018" in raw for raw in modification["inherited_restrictions"])
    sequential = _standalone_candidate(modification)
    lookup = catalog.elcats_catalog_query("lookup_candidates", deepcopy(IDENTITY), deepcopy(PART))["data"][
        "candidates"
    ][0]
    assert sequential["inherited_conditions"] == lookup["inherited_conditions"] == clauses
    assert _assess_with_independent_primary(sequential)["data"]["state"] == "unknown"
    assert _assess_with_independent_primary(lookup)["data"]["state"] == "unknown"


@pytest.mark.parametrize("terminal", [True, False])
def test_vehicle_selection_placement_excludes_the_wrong_requested_side(install_reader, terminal):
    def html(url):
        if url.path == "/vw/" and not terminal:
            return _row("Models.aspx?Code=DEMO&Title=Golf", "Golf", "лев.")
        return _row("Groups.aspx?Mdl=DEMO&Title=Golf", "Golf", "лев." if terminal else "")

    install_reader(html)
    modification = catalog.elcats_catalog_query("resolve_vehicle", deepcopy(IDENTITY))["data"]["nodes"][0]
    assert modification["placement_context"] == {"side": "left"}
    matching = catalog.elcats_catalog_query("lookup_candidates", deepcopy(IDENTITY), {**PART, "side": "left"})
    assert matching["data"]["candidates"][0]["side"] == "left"
    conflicting = catalog.elcats_catalog_query("lookup_candidates", deepcopy(IDENTITY), {**PART, "side": "right"})
    assert conflicting["data"]["candidates"] == []


def test_sibling_selection_constraints_survive_cached_shared_urls_without_bleeding(install_reader):
    install_reader(_shared_selection_html)
    resolved = catalog.elcats_catalog_query("resolve_vehicle", deepcopy(IDENTITY))
    modifications = _roundtrip(resolved["data"]["nodes"])
    assert len(modifications) == 2
    assert same_oem_catalog_ref(*modifications)  # Same tree ID; different selection constraints remain alternatives.
    for modification in modifications:
        assert public_oem_catalog_ref(modification) and modification.get("parent_ref") is None
    response = catalog.elcats_catalog_query("lookup_candidates", deepcopy(IDENTITY), deepcopy(PART))
    candidates = response["data"]["candidates"]
    assert len(candidates) == 2
    assert response["execution"]["network_calls"] == 7  # Each shared URL is fetched once.
    observed = {}
    for candidate in candidates:
        assert len(candidate["inherited_conditions"]) == 1
        clause = candidate["inherited_conditions"][0]
        observed[candidate["side"]] = clause["conditions"]["pr_codes"]["all_of"]
        assert clause["source_url"] == ROOT
        assert candidate["side"].upper() in clause["selection_url"]
        assert _assess_with_independent_primary(candidate)["data"]["state"] == "unknown"
    assert observed == {"left": ["2E4"], "right": ["1ZE"]}


def test_identical_terminal_href_retains_sibling_constraints_in_public_facade_and_e6(install_reader):
    install_reader(
        lambda _url: (
            _row("Groups.aspx?Mdl=DEMO&Title=Golf", "Golf", "PR:2E4; лев.")
            + _row("Groups.aspx?Mdl=DEMO&Title=Golf", "Golf", "PR:1ZE; прав.")
        )
    )
    resolved = catalog.elcats_catalog_query("resolve_vehicle", deepcopy(IDENTITY))
    modifications = _roundtrip(resolved["data"]["nodes"])
    assert len(modifications) == 2 and same_oem_catalog_ref(*modifications)
    for modification in modifications:
        assert public_oem_catalog_ref(modification) and modification.get("parent_ref") is None
        sequential = _standalone_candidate(modification)
        assert sequential["side"] == modification["placement_context"]["side"]
        assert _assess_with_independent_primary(sequential)["data"]["state"] == "unknown"
    response = catalog.elcats_catalog_query("lookup_candidates", deepcopy(IDENTITY), deepcopy(PART))
    assert response["outcome"] == "success" and response["data"]["coverage"]["complete"] is True
    assert response["execution"]["network_calls"] == 4
    candidates = response["data"]["candidates"]
    assert len(candidates) == 2
    observed = {}
    for candidate in candidates:
        clause = candidate["inherited_conditions"][0]
        assert clause["source_url"] == ROOT
        observed[candidate["side"]] = clause["conditions"]["pr_codes"]["all_of"]
        assert _assess_with_independent_primary(candidate)["data"]["state"] == "unknown"
    assert observed == {"left": ["2E4"], "right": ["1ZE"]}
    right = catalog.elcats_catalog_query("lookup_candidates", deepcopy(IDENTITY), {**PART, "side": "right"})
    assert right["outcome"] == "success" and len(right["data"]["candidates"]) == 1
    assert right["data"]["candidates"][0]["inherited_conditions"][0]["conditions"]["pr_codes"]["all_of"] == ["1ZE"]


@pytest.mark.parametrize("truncated", [False, True])
def test_long_selection_notes_reach_reusable_refs_and_e6_without_assuming_fitment(install_reader, truncated):
    notes = "published note " * (600 if truncated else 35) + "; PR:2E4; лев."
    install_reader(lambda _url: _row("Groups.aspx?Mdl=DEMO&Title=Golf", "Golf", notes))
    modification = _roundtrip(catalog.elcats_catalog_query("resolve_vehicle", deepcopy(IDENTITY))["data"]["nodes"][0])
    assert public_oem_catalog_ref(modification) and modification.get("parent_ref") is None
    clause = modification["inherited_conditions"][0]
    assert clause["source_url"] == ROOT and clause["binding"] == "modification"
    assert same_oem_catalog_ref(clause["catalog_ref"], modification)
    if truncated:
        assert clause["conditions"] == {}
        assert "navigation_row_context_truncated" in clause["unparsed_conditions"]
        assert "navigation_row_context_truncated" in modification["inherited_restrictions"]
        assert modification["placement_context"] == {}
    else:
        assert clause["conditions"]["pr_codes"]["all_of"] == ["2E4"]
        assert notes in clause["raw_conditions"]
        assert modification["placement_context"] == {"side": "left"}
    sequential = _standalone_candidate(modification)
    lookup = catalog.elcats_catalog_query("lookup_candidates", deepcopy(IDENTITY), deepcopy(PART))["data"][
        "candidates"
    ][0]
    assert sequential["inherited_conditions"] == lookup["inherited_conditions"] == [clause]
    assert _assess_with_independent_primary(sequential)["data"]["state"] == "unknown"
    assert _assess_with_independent_primary(lookup)["data"]["state"] == "unknown"


def test_vehicle_selection_branch_count_is_bounded_and_coverage_stays_partial(install_reader):
    html = "".join(_row(f"Groups.aspx?Mdl=MODEL{i}&Title=Golf", "Golf", "PR:2E4") for i in range(MAX_ROWS + 1))
    install_reader(lambda _url: html)
    response = catalog.elcats_catalog_query("resolve_vehicle", deepcopy(IDENTITY), page_budget=1)
    assert len(response["data"]["nodes"]) == MAX_ROWS
    assert response["outcome"] == "partial" and response["data"]["coverage"]["complete"] is False
    assert response["data"]["coverage"]["remaining_branches"][0]["reason"] == "vehicle_selection_branch_budget"
    assert response["execution"]["network_calls"] == 1


def test_intermediate_branch_budget_is_partial_before_any_modification_is_reached(install_reader, monkeypatch):
    monkeypatch.setattr(catalog, "MAX_ROWS", 2)

    def html(url):
        if url.path == "/vw/":
            return _row("Models.aspx?Code=LEFT", "Golf", "PR:2E4") + _row("Models.aspx?Code=RIGHT", "Golf", "PR:1ZE")
        return _row("Groups.aspx?Mdl=DEMO&Title=Golf", "Golf")

    install_reader(html)
    response = catalog.elcats_catalog_query("resolve_vehicle", deepcopy(IDENTITY))
    assert response["data"]["nodes"] == []
    assert response["outcome"] == "partial" and response["data"]["coverage"]["complete"] is False
    assert response["data"]["reasons"] == ["vehicle_selection_branch_budget"]
    assert len(response["data"]["coverage"]["remaining_branches"]) == 2
    assert response["execution"]["network_calls"] == 3


def test_lookup_branch_budget_counts_cached_states_and_preserves_partial_coverage(install_reader, monkeypatch):
    monkeypatch.setattr(catalog, "MAX_ROWS", 5)
    install_reader(
        lambda _url: (
            _row("Groups.aspx?Mdl=DEMO&Title=LEFT", "Golf", "PR:2E4; лев.")
            + _row("Groups.aspx?Mdl=DEMO&Title=RIGHT", "Golf", "PR:1ZE; прав.")
        )
    )
    response = catalog.elcats_catalog_query("lookup_candidates", deepcopy(IDENTITY), deepcopy(PART))
    assert response["outcome"] == "partial" and response["data"]["coverage"]["complete"] is False
    candidates = response["data"]["candidates"]
    assert len(candidates) == 1 and candidates[0]["side"] == "left"
    assert _assess_with_independent_primary(candidates[0])["data"]["state"] == "unknown"
    pending = response["data"]["coverage"]["remaining_branches"]
    assert len(pending) == 1 and pending[0]["reason"] == "navigation_branch_budget"
    assert pending[0]["catalog_ref"]["placement_context"]["side"] == "right"
    assert response["execution"]["network_calls"] == 5


def test_lookup_capacity_marks_the_unvisited_remainder_once_per_page(install_reader, monkeypatch):
    monkeypatch.setattr(catalog, "MAX_ROWS", 3)
    reader = install_reader(lambda _url: _row("Groups.aspx?Mdl=DEMO&Title=Golf", "Golf", "PR:2E4"))
    read = reader.read

    def many_diagrams(self, url, *, route_guard=None):
        page = read(self, url, route_guard=route_guard)
        if urlsplit(url).path != "/vw/Subgroup.aspx":
            return page
        html = "".join(_row(f"Parts.aspx?Mdl=DEMO&SubId=PAD{i}", "ПЕРЕДНИЕ ТОРМОЗА") for i in range(10))
        return CatalogPage(page.url, html, page.status)

    monkeypatch.setattr(reader, "read", many_diagrams)
    response = catalog.elcats_catalog_query("lookup_candidates", deepcopy(IDENTITY), deepcopy(PART))
    assert response["outcome"] == "partial" and response["data"]["coverage"]["complete"] is False
    assert len(response["data"]["candidates"]) == 1
    pending = response["data"]["coverage"]["remaining_branches"]
    assert len(pending) == 1 and pending[0]["reason"] == "navigation_branch_budget"
    assert response["execution"]["network_calls"] == 4


def test_expired_deadline_stops_cached_page_reparsing_for_another_selection_branch(install_reader, monkeypatch):
    from autostop_manager import elcats_parsers

    clock = [0]
    monkeypatch.setattr(catalog.time, "monotonic", lambda: clock[0])
    reader = install_reader(_shared_selection_html)
    read = reader.read
    parsed_groups = []
    read_groups = []
    parse = elcats_parsers.parse_catalog_html

    def record_read(self, url, *, route_guard=None):
        if urlsplit(url).path == "/vw/Groups.aspx":
            read_groups.append(url)
        return read(self, url, route_guard=route_guard)

    def expire_after_group_parse(html, *, url, operation, entry):
        result = parse(html, url=url, operation=operation, entry=entry)
        if urlsplit(url).path == "/vw/Groups.aspx":
            parsed_groups.append(url)
            clock[0] = 46
        return result

    monkeypatch.setattr(reader, "read", record_read)
    monkeypatch.setattr(elcats_parsers, "parse_catalog_html", expire_after_group_parse)
    response = catalog.elcats_catalog_query("lookup_candidates", deepcopy(IDENTITY), deepcopy(PART))
    assert len(parsed_groups) == len(read_groups) == 1
    assert response["data"]["candidates"] == []
    assert response["outcome"] == "partial" and response["data"]["coverage"]["complete"] is False
    assert response["data"]["reasons"] == ["catalog_deadline_exceeded"]
    assert response["execution"]["network_calls"] == 5
