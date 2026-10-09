from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import time
from typing import Any, ClassVar
from urllib.parse import urlsplit

import pytest

from autostop_manager import elcats_catalog as catalog
from autostop_manager.automotive_contracts import public_oem_catalog_ref
from autostop_manager.public_catalog_http import CatalogImage, CatalogPage, CatalogReadError


FIXTURES = Path(__file__).parent / "fixtures/elcats"
IDENTITY = {"vehicle_profile": {"make": "SsangYong", "model": "Actyon"}}
PART = {"item_id": "pads", "raw": "передние тормозные колодки"}


class FixtureReader:
    instances: ClassVar[list[FixtureReader]] = []

    def __init__(self, *, page_budget: int, deadline_seconds: float) -> None:
        self.page_budget = page_budget
        self.deadline = time.monotonic() + deadline_seconds
        self.network_calls = 0
        self.attempts: list[dict[str, Any]] = []
        self.pages: dict[str, CatalogPage] = {}
        self.instances.append(self)

    def _count(self, url: str) -> None:
        if self.network_calls >= self.page_budget:
            raise CatalogReadError("catalog_page_budget_exceeded")
        self.network_calls += 1
        self.attempts.append({"url": url, "method": "GET", "status": 200})

    def read(self, url: str, *, route_guard=None) -> CatalogPage:
        if route_guard:
            route_guard(url)
        if url not in self.pages:
            self._count(url)
            name = {
                "/": "models",
                "/Group.aspx": "groups",
                "/Unit.aspx": "units",
                "/Parts.aspx": "parts",
            }[urlsplit(url).path]
            self.pages[url] = CatalogPage(url, (FIXTURES / f"ssangyong_{name}.html").read_text(), 200)
        return self.pages[url]

    def read_number_image(self, url: str) -> CatalogImage:
        self._count(url)
        return CatalogImage(url, b"fixture-number-image", 200)


@pytest.fixture
def fixture_catalog(monkeypatch):
    monkeypatch.setenv("AUTOSTOP_ELCATS_ENABLED", "1")
    monkeypatch.setattr(catalog, "PublicCatalogReader", FixtureReader)
    FixtureReader.instances.clear()
    monkeypatch.setattr(
        catalog,
        "read_number_ocr",
        lambda body, *, deadline: {
            "raw_number": "48130091A0",
            "normalized_number": "48130091A0",
            "status": "ocr_candidate_unverified",
            "method": "local_tesseract_dual_psm",
            "identifier_verified": False,
        },
    )


def _selected() -> dict[str, Any]:
    return catalog.elcats_catalog_query("resolve_vehicle", deepcopy(IDENTITY))["data"]["nodes"][0]


def test_real_fixture_profile_to_front_pad_keeps_number_provenance_and_conditions(fixture_catalog):
    response = catalog.elcats_catalog_query("lookup_candidates", deepcopy(IDENTITY), deepcopy(PART))
    assert response["outcome"] == "success"
    candidates = response["data"]["candidates"]
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["normalized_number"] == "48130091A0"
    assert candidate["normalized_number"] != "DCE0002A"  # Retail price handler's opaque ID.
    assert candidate["name"] == "PAD SET-FRT BRAKE"
    assert candidate["axle"] == "front"
    assert candidate["position"] == "2"
    assert "03.2006" in candidate["raw_conditions"]
    assert candidate["quantity"] is None
    assert set(candidate["quantity_by_modification"]) == {"D20", "D20R", "E23"}
    assert candidate["oem_confirmed"] is False and candidate["fitment_confirmed"] is False
    assert candidate["source"]["identifier_verified"] is False
    assert candidate["source"]["catalog_ref"] == candidate["modification_ref"]
    assert candidate["scope"] == "modification"
    assert public_oem_catalog_ref(candidate["catalog_ref"], entity_kind="part")
    assert response["execution"]["network_calls"] <= 12
    assert not any("Price" in attempt["url"] for attempt in response["execution"]["attempts"])


def test_independent_operations_reuse_native_parent_refs(fixture_catalog):
    modification = _selected()
    assert public_oem_catalog_ref(modification)
    groups = catalog.elcats_catalog_query("list_groups", deepcopy(IDENTITY), catalog_ref=modification)
    group = next(node["catalog_ref"] for node in groups["data"]["nodes"] if node["name"] == "ТОРМОЗНАЯ СИСТЕМА")
    diagrams = catalog.elcats_catalog_query("list_diagrams", deepcopy(IDENTITY), catalog_ref=group)
    diagram = next(node["catalog_ref"] for node in diagrams["data"]["nodes"] if node["name"] == "ПЕРЕДНИЕ ТОРМОЗА")
    parts = catalog.elcats_catalog_query("list_parts", deepcopy(IDENTITY), catalog_ref=diagram)
    assert len(parts["data"]["candidates"]) == 2
    lookup = catalog.elcats_catalog_query("lookup_candidates", deepcopy(IDENTITY), deepcopy(PART), modification)
    assert len(lookup["data"]["candidates"]) == 1


@pytest.mark.parametrize("budget", [1, 2, 3, 4])
def test_budget_preserves_partial_coverage_without_empty_claim(fixture_catalog, budget):
    response = catalog.elcats_catalog_query("lookup_candidates", deepcopy(IDENTITY), deepcopy(PART), page_budget=budget)
    assert response["outcome"] == "partial"
    assert response["data"]["coverage"]["complete"] is False
    assert response["execution"]["network_calls"] <= budget
    assert response["data"]["coverage"]["remaining_branches"]


@pytest.mark.parametrize("error", ["catalog_ocr_unavailable", "catalog_ocr_timeout", "catalog_ocr_failed"])
def test_optional_ocr_failure_keeps_image_and_missing_number(fixture_catalog, monkeypatch, error):
    def fail(body, *, deadline):
        raise CatalogReadError(error)

    monkeypatch.setattr(catalog, "read_number_ocr", fail)
    response = catalog.elcats_catalog_query("lookup_candidates", deepcopy(IDENTITY), deepcopy(PART))
    assert response["outcome"] == "partial"
    row = response["data"]["candidates"][0]
    assert row["raw_number"] is None
    assert row["number_image_url"]
    assert row["number_extraction_reason"] == error
    assert response["missing_fields"] == ["catalog_number"]


def test_ocr_disagreement_cannot_become_part_number(fixture_catalog, monkeypatch):
    monkeypatch.setattr(
        catalog,
        "read_number_ocr",
        lambda body, *, deadline: {
            "raw_number": None,
            "normalized_number": None,
            "status": "ocr_inconclusive",
            "method": "test",
        },
    )
    response = catalog.elcats_catalog_query("lookup_candidates", deepcopy(IDENTITY), deepcopy(PART))
    assert response["outcome"] == "partial"
    assert response["data"]["candidates"][0]["raw_number"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"path": "/Price.aspx"},
        {"path": "https://127.0.0.1/"},
        {"parameters": {"VIN": "1HGCM82633A004352"}},
        {"entry_id": "elcats_vw"},
        {"vehicle_context": {"make": "SsangYong", "model": "Kyron"}},
        {"parameters": {"Model": 123}},
        {"parameters": {"secret_token": "DEMO"}},
        {"parameters": {"frame": "DEMO"}},
    ],
)
def test_forged_or_mismatched_reference_is_rejected_before_transport(fixture_catalog, changes):
    reference = {**_selected(), **changes}
    FixtureReader.instances.clear()
    response = catalog.elcats_catalog_query("list_groups", deepcopy(IDENTITY), catalog_ref=reference)
    assert response["outcome"] == "invalid_input"
    assert response["execution"]["network_calls"] == 0
    assert not FixtureReader.instances


@pytest.mark.parametrize(
    "arguments",
    [
        {"operation": "wrong"},
        {"page_budget": 0},
        {"page_budget": 25},
        {"page_budget": True},
        {"vehicle_identity": []},
        {"catalog_ref": "url"},
        {"part_request_item": {"raw": "колодки", "axle": []}},
        {"vehicle_identity": {"vehicle_profile": {"make": "", "model": "Kyron"}}},
        {"vehicle_identity": {"vehicle_profile": {"make": "SsangYong", "model": "1HGCM82633A004352"}}},
        {"vehicle_identity": {"vehicle_profile": {"make": "SsangYong", "model": "Kyron", "engine": object()}}},
    ],
)
def test_invalid_input_is_bounded_without_creating_reader(fixture_catalog, arguments):
    response = catalog.elcats_catalog_query(
        **{"operation": "resolve_vehicle", "vehicle_identity": deepcopy(IDENTITY), **arguments}
    )
    assert response["outcome"] == "invalid_input"
    assert not FixtureReader.instances


def test_disputed_or_failed_identity_does_not_get_public_lookup(fixture_catalog):
    identity = {"ok": False, "outcome": "provider_error", "data": deepcopy(IDENTITY)}
    assert catalog.elcats_catalog_query("resolve_vehicle", identity)["outcome"] == "invalid_input"
    assert not FixtureReader.instances


def test_feature_flag_default_and_registry_are_network_free(monkeypatch):
    monkeypatch.delenv("AUTOSTOP_ELCATS_ENABLED", raising=False)
    status = catalog.elcats_catalog_status()
    assert status["enabled"] is False and status["network_calls"] == 0
    assert len(status["entries"]) == 57
    response = catalog.elcats_catalog_query("resolve_vehicle", deepcopy(IDENTITY))
    assert response["outcome"] == "configuration_error"
    assert response["data"]["reason"] == "feature_disabled"


def test_unknown_make_and_missing_part_are_explicit(fixture_catalog):
    response = catalog.elcats_catalog_query("resolve_vehicle", {"vehicle_profile": {"make": "Unknown", "model": "X"}})
    assert response["outcome"] == "unsupported"
    assert catalog.elcats_catalog_query("lookup_candidates", deepcopy(IDENTITY))["missing_fields"] == [
        "part_request_item"
    ]
    assert catalog.elcats_catalog_query("list_groups", deepcopy(IDENTITY))["outcome"] == "invalid_input"


def test_named_front_or_system_alone_does_not_route_brake_lookup_to_doors(fixture_catalog):
    intent, missing = catalog._part(PART)
    assert not missing and intent is not None
    assert not catalog._group_matches({"name": "ПЕРЕДНИЕ ДВЕРИ"}, intent)
    assert not catalog._group_matches({"name": "СИСТЕМА ВПРЫСКА"}, intent)
    assert not catalog._group_matches({"name": "ПАРКОВОЧНЫЙ ТОРМОЗ"}, intent)
    assert catalog._group_matches({"name": "ТОРМОЗНАЯ СИСТЕМА"}, intent)


def test_permission_refusal_is_explicit_and_does_not_hide_transport_count(fixture_catalog, monkeypatch):
    def deny(self, url, *, route_guard=None):
        self._count("https://ssangyong.exist.ru/robots.txt")
        raise CatalogReadError("robots_disallowed")

    monkeypatch.setattr(FixtureReader, "read", deny)
    response = catalog.elcats_catalog_query("resolve_vehicle", deepcopy(IDENTITY))
    assert response["outcome"] == "permission_error"
    assert response["data"]["reasons"] == ["robots_disallowed"]
    assert response["execution"]["network_calls"] == 1


def test_full_identifier_in_existing_identity_is_not_sent_or_returned(fixture_catalog):
    identity = deepcopy(IDENTITY)
    identity["vehicle_profile"]["vin"] = "1HGCM82633A004352"
    response = catalog.elcats_catalog_query("resolve_vehicle", identity)
    assert "1HGCM82633A004352" not in str(response)


def test_year_and_engine_conflicts_are_not_relaxed_by_model_label():
    profile = {"make": "VW", "model": "Golf", "model_year": 2018, "engine": "E1"}
    assert not catalog._model_matches({"name": "Golf", "vehicle_context": {"model_year": 2010}}, profile)
    assert not catalog._context_matches({"vehicle_context": {"engine": "E2"}}, profile)


def test_stage_jump_without_group_returns_partial_not_invalid_native_ref(fixture_catalog, monkeypatch):
    modification = _selected()

    def page(self, entry, url, operation):
        return {
            "page_kind": "diagrams",
            "rows": [
                {
                    "entity_kind": "diagram",
                    "url": "https://ssangyong.exist.ru/Parts.aspx?Model=M&Unit=U",
                    "name": "BRAKES",
                }
            ],
        }

    monkeypatch.setattr(catalog._Navigation, "page", page)
    response = catalog.elcats_catalog_query("list_groups", deepcopy(IDENTITY), catalog_ref=modification)
    assert response["outcome"] == "partial"
    assert response["data"]["nodes"] == []
    assert response["data"]["reasons"] == ["unsupported_catalog_hierarchy"]


def test_one_diagram_redirect_keeps_valid_parent_chain_and_actual_route(fixture_catalog, monkeypatch):
    modification = _selected()
    groups = catalog.elcats_catalog_query("list_groups", deepcopy(IDENTITY), catalog_ref=modification)
    group = groups["data"]["nodes"][0]["catalog_ref"]
    actual = "https://ssangyong.exist.ru/Parts.aspx?Model=a922235a-0c56-4344-8b2b-853bd0515df1&Unit=single&Title=BRAKES"

    def page(self, entry, url, operation):
        self.evidence.append({"locator": actual})
        return {"page_kind": "parts", "rows": []}

    monkeypatch.setattr(catalog._Navigation, "page", page)
    response = catalog.elcats_catalog_query("list_diagrams", deepcopy(IDENTITY), catalog_ref=group)
    reference = response["data"]["nodes"][0]["catalog_ref"]
    assert public_oem_catalog_ref(reference, entity_kind="diagram")
    assert reference["parameters"]["Unit"] == "single" and reference["parent_ref"] == group
