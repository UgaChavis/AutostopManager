from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from autostop_manager.elcats_parsers import parse_catalog_html

_FIXTURES = Path(__file__).parent / "fixtures" / "elcats"
_REGISTRY = Path(__file__).parents[1] / "docs" / "agent" / "elcats_catalog_registry.json"
_VW = {"id": "elcats_vw", "brand": "Volkswagen", "provider": "elcats_catalog", "namespace": "elcats_epc"}
_SSANG = {
    "id": "exist_ssangyong",
    "brand": "SsangYong",
    "provider": "exist_ssangyong_catalog",
    "namespace": "exist_ssangyong_epc",
}


def _fixture(name: str, url: str, *, operation: str = "models", entry: dict | None = None) -> dict:
    return parse_catalog_html((_FIXTURES / name).read_text(), url=url, operation=operation, entry=entry or _VW)


@pytest.mark.parametrize(
    ("notes", "pr_codes", "unparsed"),
    [
        ("PR:2E4", ["2E4"], False),
        ("PR:2E4; not for LHD; 2010 - 2018", None, True),
        ("not for LHD", None, True),
        ("2010 - 2018", None, True),
        ("PR:2E4 AND NOT PR:1ZE", None, True),
        ("303X28", None, False),
    ],
)
def test_part_remarks_keep_parsed_and_unresolved_conditions(notes, pr_codes, unparsed):
    html = (
        "<table><tr><th>Код детали</th><th>Наименование</th><th>Примечание</th></tr>"
        f"<tr><td>DEMO100</td><td>Brake pad</td><td>{notes}</td></tr></table>"
    )
    response = parse_catalog_html(
        html, url="https://elcats.ru/vw/Parts.aspx?Mdl=DEMO&SubId=PAD", operation="list_parts", entry=_VW
    )
    row = response["rows"][0]
    assert row["notes"] == row["raw_conditions"] == notes
    assert bool(row["unparsed_conditions"]) is unparsed
    assert bool(row["unparsed_restrictions"]) is unparsed
    if pr_codes is not None:
        assert row["conditions"]["pr_codes"]["all_of"] == pr_codes
    else:
        assert row["conditions"] == {}
    assert row["confirmed"] is False


def test_inventory_registers_all_57_passenger_routes_and_preserves_providers() -> None:
    registry = json.loads(_REGISTRY.read_text())
    entries = registry["entries"]
    assert registry["version"] == registry["registry_version"]
    assert len(entries) == len({e["id"] for e in entries}) == 57
    assert Counter(e["provider"] for e in entries) == {
        "elcats_catalog": 46,
        "japancats_catalog": 10,
        "exist_ssangyong_catalog": 1,
    }
    for entry in entries:
        assert entry["vehicle_scope"] == "passenger"
        assert {
            "brand",
            "aliases",
            "entry_url",
            "provider",
            "namespace",
            "parser_family",
            "readiness",
            "reason",
            "capabilities",
            "required_profile_fields",
            "verification",
            "implementation_state",
            "access_availability",
        } <= entry.keys()
        if entry["provider"] != "exist_ssangyong_catalog":
            assert entry["readiness"] == entry["access_availability"] == "robots_disallowed"
            assert entry["verification"]["robots"]["allowed"] is False
            assert entry["verification"]["number_extraction_verified"] is False
    assert next(e for e in entries if e["id"] == "exist_ssangyong")["verification"]["robots"]["status_code"] == 404
    ssang = next(e for e in entries if e["id"] == "exist_ssangyong")
    assert ssang["readiness"] == "working"
    assert ssang["ocr_required"] is True
    assert ssang["verification"]["number_extraction_verified"] is True
    assert ssang["verification"]["fitment_verified"] is False
    assert next(e for e in entries if e["id"] == "elcats_kiaold")["catalog_variant"] == "legacy"
    assert next(e for e in entries if e["id"] == "elcats_gm_chevrolet")["catalog_variant"] == "USA"
    assert next(e for e in entries if e["id"] == "elcats_bosch")["data_kind"] == "component_catalog"


def test_ssangyong_public_chain_keeps_models_groups_and_diagrams_distinct() -> None:
    result = _fixture("ssangyong_models.html", "https://ssangyong.exist.ru/", entry=_SSANG)
    assert result["page_kind"] == "models"
    assert [r["name"] for r in result["rows"]] == ["ACTYON", "KYRON", "TIVOLI"]
    model = result["rows"][0]
    assert model["entity_kind"] == "modification"
    assert model["id"] == "A922235A-0C56-4344-8B2B-853BD0515DF1"
    assert parse_qs(urlsplit(model["url"]).query) == {"Model": [model["id"]], "Title": ["ACTYON"]}
    groups = _fixture("ssangyong_groups.html", model["url"], entry=_SSANG)
    assert groups["page_kind"] == "groups"
    assert len(groups["rows"]) == 2
    brake_group = groups["rows"][1]
    assert brake_group["entity_kind"] == "group"
    assert brake_group["ref_params"]["Group"] == "4"
    assert brake_group["ref_params"]["SubGroup"] == "8"
    units = _fixture("ssangyong_units.html", brake_group["url"], entry=_SSANG)
    assert units["page_kind"] == "diagrams"
    assert len(units["rows"]) == 3  # picture+caption links for one diagram are deduplicated
    front = next(r for r in units["rows"] if r.get("axle") == "front")
    assert front["entity_kind"] == "diagram"
    assert front["id"] == "6b0573dd-4620-4a66-9b88-cb8b199e4668"
    assert parse_qs(urlsplit(front["url"]).query)["Unit"] == [front["id"]]


def test_same_navigation_url_keeps_distinct_row_restrictions_and_deduplicates_identical_rows() -> None:
    def row(notes):
        return (
            f'<table><tr><td><a href="Groups.aspx?Mdl=DEMO&amp;Title=Golf">Golf</a></td><td>{notes}</td></tr></table>'
        )

    left = row("PR:2E4; лев.")
    right = row("PR:1ZE; прав.")
    result = parse_catalog_html(left + right + left, url="https://www.elcats.ru/vw/", operation="models", entry=_VW)
    rows = result["rows"]
    assert len(rows) == 2 and rows[0]["url"] == rows[1]["url"]
    assert [r["conditions"]["pr_codes"]["all_of"] for r in rows] == [["2E4"], ["1ZE"]]
    assert [r["side"] for r in rows] == ["left", "right"]


def test_image_and_caption_navigation_still_deduplicate_identical_metadata() -> None:
    target = "Parts.aspx?Mdl=DEMO&amp;SubId=PAD"
    html = (
        f'<table><tr><td><a href="{target}"><img alt="Front brake" src="diagram.png"></a></td>'
        f'<td><a href="{target}">Front brake</a></td><td>PR:2E4</td></tr></table>'
    )
    result = parse_catalog_html(
        html, url="https://www.elcats.ru/vw/Subgroup.aspx?Mdl=DEMO", operation="list_diagrams", entry=_VW
    )
    assert len(result["rows"]) == 1
    assert result["rows"][0]["conditions"]["pr_codes"]["all_of"] == ["2E4"]


def test_long_navigation_context_retains_published_conditions_and_placement() -> None:
    notes = "published note " * 35 + "; PR:2E4; 2010 - 2018; лев."
    assert len(notes) > 500
    html = f'<table><tr><td><a href="Groups.aspx?Mdl=DEMO&amp;Title=Golf">Golf</a></td><td>{notes}</td></tr></table>'
    row = parse_catalog_html(html, url="https://www.elcats.ru/vw/", operation="models", entry=_VW)["rows"][0]
    assert notes in row["notes"] and notes in row["raw_conditions"]
    assert row["conditions"]["pr_codes"]["all_of"] == ["2E4"]
    assert row["side"] == "left" and row["unparsed_restrictions"]


def test_truncated_navigation_context_is_unresolved_and_distinct_sibling_rows_survive() -> None:
    from autostop_manager.elcats_parsers import _MAX_NAVIGATION_CONTEXT_CHARS

    prefix = "PR:2E4; лев.; " + "published note " * 600
    assert len(prefix) > _MAX_NAVIGATION_CONTEXT_CHARS
    html = "".join(
        '<table><tr><td><a href="Groups.aspx?Mdl=DEMO&amp;Title=Golf">Golf</a></td>'
        f"<td>{prefix}; {tail}</td></tr></table>"
        for tail in ("PR:1ZE; прав.", "2010 - 2018")
    )
    rows = parse_catalog_html(html, url="https://www.elcats.ru/vw/", operation="models", entry=_VW)["rows"]
    assert len(rows) == 2
    for row in rows:
        assert len(row["raw_conditions"]) == len(row["notes"]) == _MAX_NAVIGATION_CONTEXT_CHARS
        assert row["conditions"] == {} and "side" not in row
        assert "navigation_row_context_truncated" in row["unparsed_conditions"]
        assert row["unparsed_conditions"] == row["unparsed_restrictions"]
    assert rows[0]["unparsed_conditions"] != rows[1]["unparsed_conditions"]


def test_ssangyong_masked_number_is_not_price_identifier_or_image_token() -> None:
    result = _fixture(
        "ssangyong_parts.html", "https://ssangyong.exist.ru/Parts.aspx?Model=model&Unit=unit&Title=front", entry=_SSANG
    )
    assert result["page_kind"] == "parts"
    assert result["access_flags"] == ["rendered_evidence_required"]
    assert len(result["rows"]) == 2
    pad = next(r for r in result["rows"] if r["row_id"] == "12")
    assert pad["name"] == "PAD SET-FRT BRAKE"
    assert pad["position"] == "2"
    assert pad["raw_number"] is pad["normalized_number"] is None
    assert pad["confirmed"] is False
    assert pad["ocr_required"] is True
    assert urlsplit(pad["number_image_url"]).path == "/PCode.ashx"
    assert "DCE0002A" not in json.dumps(pad)
    assert pad["quantity"] is None
    assert pad["quantity_by_modification"] == {
        "D20": {"raw": "1", "quantity": 1},
        "D20R": {"raw": "1", "quantity": 1},
        "E23": {"raw": "1", "quantity": 1},
    }
    assert "03.2006 - ..." in pad["raw_conditions"]
    assert pad["unparsed_restrictions"]
    assert pad["axle"] == "front"


def test_vag_native_diagram_id_and_pr_alternatives_are_preserved() -> None:
    result = _fixture(
        "vag_subgroup.html",
        "https://www.elcats.ru/vw/SubGroup.aspx?GroupId=6&Model=181ef4be-6708-4504-a641-fa6cb5587dd0",
    )
    assert result["page_kind"] == "diagrams"
    assert len(result["rows"]) == 3
    first, second, rear = result["rows"]
    assert first["id"] == "18B01168"
    assert second["id"] == "1CD01168"
    assert first["conditions"]["pr_codes"]["all_of"] == ["2E3"]
    assert second["conditions"]["pr_codes"]["all_of"] == ["2E4"]
    assert first["axle"] == second["axle"] == "front"
    assert rear["axle"] == "rear"
    assert parse_qs(urlsplit(second["url"]).query) == {
        "Mdl": ["181ef4be-6708-4504-a641-fa6cb5587dd0"],
        "SubId": ["1CD01168"],
    }


def test_economy_row_remains_an_alternative_at_same_callout_not_supersession() -> None:
    result = _fixture("vag_parts_2010.html", "https://www.elcats.ru/vw/Parts.aspx?Mdl=mdl&SubId=subid")
    pad = result["rows"][1]
    economy = result["rows"][2]
    assert pad["raw_number"] == "2H0 698 151 A"
    assert pad["normalized_number"] == "2H0698151A"
    assert pad["position"] == "12"
    assert economy["position"] == "(12)"
    assert economy["normalized_number"] == "JZW698151AM"
    assert economy["variant"] == "Economy"
    assert pad["id"] != economy["id"]
    assert all(not r["confirmed"] and "supersedes" not in r for r in result["rows"])


def test_scheme_pr_inherited_into_all_parts_unknown_quantity_and_absent_part() -> None:
    result = _fixture("vag_parts_2018.html", "https://www.elcats.ru/vw/Parts.aspx?Mdl=mdl&SubId=subid")
    assert len(result["rows"]) == 4  # 'not for this model' with no number is not an OEM candidate
    assert all(r["conditions"]["pr_codes"]["all_of"] == ["2E4"] for r in result["rows"])
    assert all(r["inherited_restrictions"] == [] for r in result["rows"])
    assert all(r["inherited_conditions"][0]["binding"] == "diagram" for r in result["rows"])
    assert result["rows"][0]["side"] == "left"
    assert result["rows"][1]["side"] == "right"
    grease = result["rows"][-1]
    assert grease["quantity"] is None and grease["raw_quantity"] == "X"
    assert "non_number_row_preserved_as_catalog_note" in result["warnings"]


@pytest.mark.parametrize(
    ("signature", "assignments", "route", "call", "expected"),
    [
        (
            "type,code,value,t",
            "Mdl=type;Kpp=code;T=t",
            "MdlYear.aspx",
            "'A2200025','N','0','1 E81 3-дверный (116d)'",
            {"Mdl": "A2200025", "Kpp": "N", "T": "1 E81 3-дверный (116d)"},
        ),
        (
            "modelId",
            "Model=modelId",
            "Modification.aspx",
            "'ACCENT/SOLARIS 11 (2010-) RUSSIA'",
            {"Model": "ACCENT/SOLARIS 11 (2010-) RUSSIA"},
        ),
        (
            "key,title,value",
            "Key=key;Title=title",
            "Groups.aspx",
            "'g49KdKeGprA6TO2b/bBPAg==','500L 2012','0'",
            {"Key": "g49KdKeGprA6TO2b/bBPAg==", "Title": "500L 2012"},
        ),
        (
            "modelId",
            "Model=modelId",
            "Group.aspx",
            "'fb869f67-30ce-4dc7-bf1e-d3867a9df1b8'",
            {"Model": "fb869f67-30ce-4dc7-bf1e-d3867a9df1b8"},
        ),
        (
            "type,reg,value,title",
            "Type=type;Reg=reg;Title=title",
            "Models.aspx",
            "'1','1','0','ЛЕГКОВОЙ (ОСНОВНОЙ РЫНОК)'",
            {"Type": "1", "Reg": "1", "Title": "ЛЕГКОВОЙ (ОСНОВНОЙ РЫНОК)"},
        ),
    ],
)
def test_published_literal_get_template_families(
    signature: str, assignments: str, route: str, call: str, expected: dict
) -> None:
    # Reconstructed minimal templates from public root observations; no live brand-readiness claim.
    body = "".join(f"document.forms[1].{v};" for v in assignments.replace("=", ".value=").split(";"))
    html = f'<form id="GetForm" method="get"></form><script>function submit({signature}){{var form=document.getElementById("GetForm");{body}document.forms[1].action="{route}";}}</script><a href="javascript:submit({call})">Модель</a>'
    result = parse_catalog_html(html, url="https://www.elcats.ru/fiat/", operation="models", entry=_VW)
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["ref_params"] == expected
    assert parse_qs(urlsplit(row["url"]).query) == {k: [v] for k, v in expected.items()}
    assert urlsplit(row["url"]).path == "/fiat/" + route


def test_published_japancats_named_container_navigation_is_kept_distinct() -> None:
    html = "<div id=\"lF\" name=\"Groups.aspx\"><input name=\"Model\"></div><a href=\"javascript:submit('lF','n0','29700000;','true')\">COROLLA</a>"
    result = parse_catalog_html(
        html, url="https://www.japancats.ru/Toyota/", operation="models", entry={"brand": "Toyota"}
    )
    assert result["rows"][0]["id"] == "29700000"
    assert result["rows"][0]["url"] == "https://www.japancats.ru/Toyota/Groups.aspx?Model=29700000"


@pytest.mark.parametrize(
    "target",
    [
        "https://other.example/Parts.aspx?Model=one",
        "http://www.elcats.ru/vw/Parts.aspx?Model=one",
        "Price.aspx?PcId=123456789",
        "Parts.aspx?VIN=123",
        "Login.aspx?Model=one",
        "https://www.elcats.ru:bad/Parts.aspx?Model=one",
    ],
)
def test_navigation_rejects_external_private_sensitive_and_invalid_urls(target: str) -> None:
    result = parse_catalog_html(
        f'<a href="{target}">деталь</a>', url="https://www.elcats.ru/vw/", operation="models", entry=_VW
    )
    assert result["rows"] == []


def test_no_execution_or_post_form_reconstruction() -> None:
    html = '<form id="GetForm" method="post"></form><script>function submit(x){var form=document.getElementById("GetForm");document.forms[1].Model.value=x;document.forms[1].action="Group.aspx";}</script><a href="javascript:submit(\'a\')">A</a><a href="javascript:submit(document.cookie)">B</a>'
    result = parse_catalog_html(html, url="https://www.elcats.ru/vw/", operation="models", entry=_VW)
    assert result["rows"] == []


@pytest.mark.parametrize("restriction", ["PR:2E4 AND NOT PR:1ZE", "PR:2E4XYZ", "PR:2E4-1ZE", "PR:2E4 кроме 1ZE"])
def test_complex_boolean_pr_grammar_stays_unparsed(restriction: str) -> None:
    html = (_FIXTURES / "vag_parts_2018.html").read_text().replace("PR:2E4", restriction)
    result = parse_catalog_html(html, url="https://www.elcats.ru/vw/Parts.aspx?Mdl=mdl", operation="parts", entry=_VW)
    assert result["rows"][0]["conditions"] == {}
    assert result["rows"][0]["unparsed_restrictions"]
    assert result["rows"][0]["inherited_restrictions"]


def test_combined_placement_does_not_silently_select_first_side() -> None:
    html = (
        (_FIXTURES / "vag_parts_2018.html")
        .read_text()
        .replace("лев.", "лев./прав.")
        .replace("передн.", "передн./задн.")
    )
    result = parse_catalog_html(html, url="https://www.elcats.ru/vw/Parts.aspx?Mdl=mdl", operation="parts", entry=_VW)
    assert "side" not in result["rows"][0]
    assert "axle" not in result["rows"][0]


def test_native_selection_ids_include_catalog_dimensions() -> None:
    html = '<a href="Models.aspx?Type=1&amp;Reg=1&amp;Title=main">Легковой основной рынок</a><a href="Models.aspx?Type=1&amp;Reg=F&amp;Title=USA">Легковой USA</a>'
    result = parse_catalog_html(
        html, url="https://www.elcats.ru/mercedes/", operation="models", entry={"brand": "Mercedes-Benz"}
    )
    assert [r["id"] for r in result["rows"]] == ["Type=1;Reg=1", "Type=1;Reg=F"]
    assert all(r["page_kind"] == "selections" for r in result["rows"])


@pytest.mark.parametrize(
    ("html", "flags"),
    [
        ("<a href='/Login.aspx'>Войти</a>", []),
        ("<p>Для доступа требуется авторизация</p>", ["auth_required"]),
        ("<title>Just a moment...</title>", ["challenge"]),
        ("<select onchange=\"__doPostBack('x','')\"><option>A</option></select>", ["unsupported_postback_navigation"]),
    ],
)
def test_access_flags_require_actual_access_evidence(html: str, flags: list[str]) -> None:
    result = parse_catalog_html(
        html, url="https://www.japancats.ru/Honda/", operation="models", entry={"brand": "Honda"}
    )
    assert result["access_flags"] == flags
