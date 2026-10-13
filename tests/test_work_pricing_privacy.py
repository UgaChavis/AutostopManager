from __future__ import annotations

import builtins
import io
import json
import os
import socket
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

import pytest

from autostop_manager import automotive_labor as labor
from autostop_manager import vin_lookup
from autostop_manager import work_pricing as pricing
from autostop_manager import work_pricing_research as research

_VIN = "WBA00000000000000"
_CHASSIS = "ES19999999"
_SECRET = "api_key=synthetic-sentinel-credential"
_QUOTED_SECRET = '{"api_key":"synthetic-sentinel-credential"}'
_PRIVATE_INPUTS = [
    pytest.param(_VIN, id="inline-vin"),
    pytest.param(_VIN[:3] + "-" + _VIN[3:], id="split-inline-vin"),
    pytest.param("synthetic@example.invalid", id="email"),
    pytest.param("+7 999 000-00-00", id="ru-phone"),
    pytest.param(_SECRET, id="secret"),
    pytest.param("secret:synthetic-sentinel-credential", id="colon-secret"),
    pytest.param(quote(_SECRET, safe=""), id="encoded-secret"),
    pytest.param(quote(quote(_SECRET, safe=""), safe=""), id="double-encoded-secret"),
    pytest.param(_QUOTED_SECRET, id="quoted-secret-key"),
    pytest.param(quote(_QUOTED_SECRET, safe=""), id="encoded-quoted-secret"),
    pytest.param(quote(quote(_QUOTED_SECRET, safe=""), safe=""), id="double-encoded-quoted-secret"),
]
_SEARCH_HTML = (
    '<div class="result results_links"><a class="result__a" href="https://example.org/labor">'
    'Замена тестовой детали</a><a class="result__snippet">'
    "Замена тестовой детали 1000 руб. Только работа без запчастей Красноярск."
    "</a></div></div>"
).encode()


def _decoded(value: str) -> str:
    for _ in range(4):
        value = unquote(value)
    return value.casefold()


@pytest.fixture
def synthetic_public_http(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Unexpected provider, private loader, or external request")

    for key, value in {
        "AUTOSTOP_MANAGER_ENV_FILE": "/dev/null",
        "AUTOSTOP_MANAGER_DB": str(tmp_path / "synthetic.sqlite3"),
        "AUTOSTOP_AUTOMOTIVE_OFFLINE_RUNTIME": str(tmp_path / "no-runtime"),
        "AUTOSTOP_J1_CACHE_DIR": str(tmp_path / "j1"),
        "AUTOSTOP_J1_VIN_CACHE_DIR": str(tmp_path / "vin"),
        "AUTOSTOP_J1_VIN_RESEARCH_ENABLED": "0",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(pricing, "_load_labor_experience", denied)
    monkeypatch.setattr(vin_lookup, "decode_vin_vpic", denied)
    monkeypatch.setattr(vin_lookup, "decode_wmi_vpic", denied)
    monkeypatch.setattr(vin_lookup, "urlopen", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "bind", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)

    protected = (
        "/opt/AutostopManager",
        "/var/lib/autostop-manager",
        "/run/autostop-j1-vin",
        "/opt/autostop-automotive",
        "/opt/autostop-manager-releases",
        str(Path(__file__).resolve().parents[1] / "data"),
    )

    def guarded(original):
        def call(*args, **kwargs):
            for value in args:
                if isinstance(value, (str, bytes, os.PathLike)):
                    path = os.path.abspath(os.fsdecode(value))
                    if any(path == root or path.startswith(root + "/") for root in protected):
                        raise AssertionError("Default private state must not be accessed")
                    if os.path.basename(path) == ".env":
                        raise AssertionError("Ambient env file must not be accessed")
            return original(*args, **kwargs)

        return call

    for module, names in (
        (builtins, ("open",)),
        (io, ("open",)),
        (os, ("open", "listdir", "scandir", "remove", "rmdir", "rename", "mkdir")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, guarded(getattr(module, name)))

    class Queries(list[str]):
        private_queries = 0

    queries = Queries()
    markers = (
        _VIN,
        _VIN[:3] + "-" + _VIN[3:],
        _CHASSIS,
        "19999999",
        "synthetic@example.invalid",
        "+7 999 000-00-00",
        "synthetic-sentinel-credential",
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            assert limit == 250_000
            return _SEARCH_HTML

    def fake_urlopen(request, *, timeout):
        assert 0 < timeout <= 4
        assert urlsplit(request.full_url).hostname == "html.duckduckgo.com"
        query = parse_qs(urlsplit(request.full_url).query)["q"][0]
        if any(marker.casefold() in _decoded(query) for marker in markers):
            queries.private_queries += 1
            queries.append("[private query captured]")
        else:
            queries.append(query)
        return Response()

    monkeypatch.setattr(research, "urlopen", fake_urlopen)
    return queries


def _estimate(**kwargs):
    return pricing.estimate_repair_work_cost(
        **{
            "make": "DEMO",
            "model": "MODEL",
            "work_items": ["замена тестовой детали"],
            "use_internal_experience": False,
            **kwargs,
        }
    )


@pytest.mark.parametrize("private_input", _PRIVATE_INPUTS)
@pytest.mark.parametrize("field", ["vehicle", "make", "model", "year", "engine", "transmission", "work_items", "city"])
def test_legacy_public_research_never_sends_private_completed_query(synthetic_public_http, private_input, field):
    value = "DEMO " + private_input
    supplied = ["замена тестовой детали " + private_input] if field == "work_items" else value
    result = _estimate(**{field: supplied})
    marker = _decoded(private_input)
    assert synthetic_public_http.private_queries == 0
    assert all(marker not in _decoded(query) for query in synthetic_public_http)
    public_research = {"research": result["research"], "sources_checked": result["sources_checked"]}
    assert marker not in _decoded(json.dumps(public_research, ensure_ascii=False))


@pytest.mark.parametrize("private_input", _PRIVATE_INPUTS)
def test_shared_ddg_boundary_rejects_private_query_before_http(synthetic_public_http, private_input):
    with pytest.raises(ValueError):
        research._ddg_search("замена тестовой детали " + private_input)
    assert synthetic_public_http == []


@pytest.mark.parametrize("private_input", _PRIVATE_INPUTS)
@pytest.mark.parametrize("field", ["make", "work_items", "city"])
def test_new_price_evidence_collector_rejects_private_input_before_http(synthetic_public_http, private_input, field):
    kwargs: dict[str, Any] = {"vehicle_context": {"make": "DEMO"}, "work_items": ["замена тестовой детали"]}
    if field == "make":
        kwargs["vehicle_context"] = {"make": "DEMO " + private_input}
    elif field == "work_items":
        kwargs["work_items"] = ["замена тестовой детали " + private_input]
    else:
        kwargs["city"] = "DEMO " + private_input
    result = labor.collect_work_price_evidence(**kwargs)
    assert result["outcome"] == "invalid_input"
    assert result["execution"]["network_calls"] == 0
    assert synthetic_public_http == []


def test_healthy_legacy_queries_and_actual_ddg_parser_still_work(synthetic_public_http):
    result = _estimate()
    assert result["ok"] is True
    assert len(synthetic_public_http) == 4
    price, city, time, generic_time = synthetic_public_http
    for vehicle_query in (price, time):
        assert vehicle_query.startswith("замена тестовой детали DEMO")
        assert "MODEL" in vehicle_query
    assert price.endswith("стоимость работы без запчастей СТО Россия")
    assert city == "замена тестовой детали прайс СТО работа Красноярск"
    assert time.endswith("нормо часы")
    assert generic_time == "замена тестовой детали норма времени трудоемкость снять установить"
    assert any(checked.get("rows_found") == 1 for checked in result["sources_checked"])


def test_provided_identifiers_are_stripped_from_vehicle_query_and_entire_output(synthetic_public_http):
    result = _estimate(vehicle=f"DEMO {_VIN} {_CHASSIS}", vin=_VIN, chassis=_CHASSIS)
    assert len(synthetic_public_http) == 4
    assert synthetic_public_http.private_queries == 0
    assert all(
        _VIN.casefold() not in query.casefold() and _CHASSIS.casefold() not in query.casefold()
        for query in synthetic_public_http
    )
    rendered = json.dumps(result, ensure_ascii=False)
    assert _VIN not in rendered and _CHASSIS not in rendered
    assert result["vehicle_context"]["vin"] == "WBA***000"
    assert result["vehicle_context"]["chassis"] == "ES1***999"
    assert result["privacy"]["raw_vehicle_identifier_removed_from_public_research_queries"] is True


def test_healthy_numeric_vehicle_fields_do_not_combine_into_a_false_vin(synthetic_public_http):
    result = _estimate(make="Mazda", model="MX5", year=2015, engine="P5VPS")
    assert result["ok"] is True
    assert len(synthetic_public_http) == 4
    for query in (synthetic_public_http[0], synthetic_public_http[2]):
        assert all(value in query for value in ("Mazda", "MX5", "2015", "P5VPS"))
    assert result["vehicle_context"]["year"] == 2015
    assert type(result["vehicle_context"]["year"]) is int


def test_new_collector_preserves_healthy_structured_public_context(synthetic_public_http):
    result = labor.collect_work_price_evidence(
        ["замена тестовой детали"], {"make": "Mazda", "model": "MX5", "year": 2015, "engine": "P5VPS"}
    )
    assert result["outcome"] in {"success", "partial"}
    assert result["execution"]["network_calls"] == 4
    assert len(synthetic_public_http) == 4
    assert any(row["price_rub"] == 1000 for row in result["data"]["observations"])


@pytest.mark.parametrize("identifier_field, identifier", [("vin", _VIN), ("chassis", _CHASSIS)])
@pytest.mark.parametrize("field", ["vehicle", "make", "model", "year", "engine", "transmission", "work_items", "city"])
def test_provided_identifiers_are_redacted_from_all_fields_and_public_queries(
    synthetic_public_http, identifier_field, identifier, field
):
    supplied = ["замена тестовой детали " + identifier] if field == "work_items" else "DEMO " + identifier
    result = _estimate(**{identifier_field: identifier, field: supplied})
    assert synthetic_public_http.private_queries == 0
    assert all(identifier.casefold() not in query.casefold() for query in synthetic_public_http)
    assert identifier.casefold() not in json.dumps(result, ensure_ascii=False).casefold()
    assert result["privacy"]["raw_vehicle_identifier_removed_from_public_research_queries"] is True


@pytest.mark.parametrize(
    "secret",
    [{"api_key": "synthetic-sentinel-credential"}, {"credentials": {"token": "synthetic-sentinel-credential"}}],
)
def test_structured_secret_context_is_rejected_before_http(synthetic_public_http, secret):
    result = labor.collect_work_price_evidence(["замена тестовой детали"], {"make": secret})
    assert result["outcome"] == "invalid_input"
    assert result["execution"]["network_calls"] == 0
    assert synthetic_public_http == []


def test_known_chassis_is_removed_in_mixed_case_before_research_and_output(synthetic_public_http):
    result = _estimate(chassis=_CHASSIS, city="DEMO eS19999999")
    assert synthetic_public_http.private_queries == 0
    assert _CHASSIS.casefold() not in json.dumps(result, ensure_ascii=False).casefold()
    assert result["privacy"]["raw_vehicle_identifier_removed_from_public_research_queries"] is True


def test_known_numeric_chassis_in_year_is_removed_before_research_and_public_output(synthetic_public_http):
    result = _estimate(chassis="19999999", year=19999999)
    assert synthetic_public_http.private_queries == 0
    assert result["vehicle_context"]["year"] not in (19999999, "19999999")
    assert result["privacy"]["raw_vehicle_identifier_removed_from_public_research_queries"] is True


def test_numeric_identity_redaction_preserves_healthy_year_and_financial_values(synthetic_public_http):
    quotes = [
        {
            "source": f"synthetic_source_{index}",
            "city": "Красноярск",
            "operation_name": "замена тестовой детали",
            "price_rub": 19999999,
            "labor_only": True,
            "includes_parts": False,
        }
        for index in range(3)
    ]
    result = _estimate(chassis="19999999", year=2015, quotes_json=quotes)
    assert result["vehicle_context"]["year"] == 2015
    assert type(result["vehicle_context"]["year"]) is int
    assert result["market_sample"]["valid_count"] == 3
    assert type(result["market_sample"]["valid_count"]) is int
    assert all(
        row["price_rub"] == 19999999 and type(row["price_rub"]) is int for row in result["market_sample"]["quotes"]
    )
    assert synthetic_public_http == []
