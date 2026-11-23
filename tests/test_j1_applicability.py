"""Evidence-backed applicability and source-only automotive context."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from autostop_manager import j1_fetch
from autostop_manager import j1_research as j1
from autostop_manager.j1_applicability import Passage, assess_applicability

CONTEXT = {"make": "Ficta", "model": "Norvale", "year": "2021", "engine": "MX204", "dtc": "P1A27"}
URL = "https://static.nhtsa.gov/synthetic/technical-bulletin"
SCOPE = "This bulletin applies to model year 2021 Ficta Norvale vehicles with engine MX204."
SUBJECT = "P1A27 means a connector feedback fault."


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "vin"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "0")
    monkeypatch.setenv("AUTOSTOP_J1_SEARXNG_URL", "")
    monkeypatch.setattr(j1, "_STOP", False)
    monkeypatch.setattr(j1.signal, "signal", lambda *_args: None)


@pytest.mark.parametrize(
    ("sentences", "classification"),
    [
        (
            [
                "This bulletin applies to model year 2021 Ficta Norvale vehicles with engine MX204.",
                "P1A27 means a connector feedback fault.",
            ],
            "exact",
        ),
        (
            [
                "This bulletin applies to model year 2021 Ficta Norvale vehicles with engine MX204.",
                "This bulletin explicitly excludes model year 2020 Ficta Norvale vehicles with engine MX204.",
                "P1A27 means a connector feedback fault.",
            ],
            "exact",
        ),
        (
            [
                "This bulletin applies to model year 2021 Ficta Norvale vehicles with engine MX204.",
                "See also DTC P1A27 in a separate bulletin.",
            ],
            "analog",
        ),
        (
            [
                "This bulletin applies to Ficta Norvale vehicles with engine MX204 built in 2021.",
                "P1A27 means a connector feedback fault.",
            ],
            "analog",
        ),
        (
            [
                "Ficta Norvale workshop supplement",
                "Model year 2021 vehicles with engine MX204 are covered.",
                "This bulletin does not apply to engine MX204.",
                "P1A27 means a connector feedback fault.",
            ],
            "conflict",
        ),
        (
            [
                "This bulletin applies to model year 2021 Ficta Norvale vehicles with engine MX208.",
                "This bulletin explicitly excludes engine MX204.",
                "P1A28 means a connector feedback fault.",
            ],
            "excluded",
        ),
        (
            [
                "This bulletin applies to model year 2020 Ficta Norvale vehicles with engine MX204.",
                "Model year 2021 appears in the list of previous repairs.",
                "P1A27 means a connector feedback fault.",
            ],
            "analog",
        ),
        (
            [
                "This bulletin applies to model year 2021 Ficta Norvale vehicles with engine MX204.",
                "P1A27-01 means a different fault.",
            ],
            "analog",
        ),
        (
            [
                "Ficta Norvale model year 2021 vehicles with engine MX204 may qualify for a campaign.",
                "P1A27 means a connector feedback fault.",
            ],
            "analog",
        ),
    ],
)
def test_scope_requires_a_cited_vehicle_and_requested_subject(sentences, classification):
    result = assess_applicability(CONTEXT, (Passage(sentence, 1, URL) for sentence in sentences))
    assert result.label == classification
    assert all(citation["quote"] in sentences for citation in result.evidence)


@pytest.mark.parametrize(
    ("overrides", "sentences", "classification"),
    [
        (
            {},
            [
                "This bulletin applies to 2020 model year Ficta Norvale vehicles with engine MX204 for DTC P1A27.",
                SCOPE.removesuffix(".") + " for DTC P1A28.",
            ],
            "analog",
        ),
        ({}, [SCOPE, "In model year 2020, P1A27 means a connector feedback fault."], "analog"),
        ({}, [SCOPE, "DTC P1A27 is defined for engine MX208."], "analog"),
        ({}, [SCOPE, "DTC P1A27 is not covered in this bulletin."], "excluded"),
        ({}, [SCOPE.removesuffix(".") + " unless DTC P1A27 is present."], "excluded"),
        ({}, ["Do not use this procedure on vehicles with engine MX204.", SCOPE, SUBJECT], "conflict"),
        (
            {},
            ["Запрещается использовать процедуру на автомобилях с двигателем MX204.", SCOPE, SUBJECT],
            "conflict",
        ),
        (
            {},
            [
                SCOPE.replace("2021", "2020").removesuffix(".") + " unless DTC P1A27 is present.",
                SCOPE,
                SUBJECT,
            ],
            "exact",
        ),
        ({}, [SCOPE, "This bulletin excludes engine MX208, not MX204.", SUBJECT], "exact"),
        (
            {},
            [SCOPE, "This bulletin excludes engine MX204 unless DTC P1A27 is present.", SUBJECT],
            "exact",
        ),
        (
            {},
            [SCOPE, "This bulletin excludes engine MX204 unless DTC P1A28 is present.", SUBJECT],
            "conflict",
        ),
        (
            {},
            [SCOPE, "This bulletin excludes engine MX204 unless the revised harness was installed.", SUBJECT],
            "analog",
        ),
        (
            {},
            [
                SCOPE,
                "This bulletin excludes model year 2021 Ficta Norvale vehicles with engine MX208.",
                SUBJECT,
            ],
            "exact",
        ),
        ({}, [SCOPE.replace("MX204.", "MX204-T only."), SUBJECT], "analog"),
        (
            {"production_date": "2020-12-14"},
            [
                SCOPE.removesuffix(".") + ", built on 2020-12-14.",
                "This bulletin excludes vehicles built before 2021-01-01.",
                SUBJECT,
            ],
            "conflict",
        ),
        (
            {"production_date": "2020-12-14"},
            [
                SCOPE.removesuffix(".") + ", built after 2020-12-01 and before 2020-12-10.",
                SUBJECT,
            ],
            "analog",
        ),
        (
            {"production_date": "2020-12-14"},
            [
                SCOPE.removesuffix(".") + ', built between "2020-12-01" and "2021-01-01".',
                SUBJECT,
            ],
            "exact",
        ),
        (
            {"production_date": "2020-12-14"},
            [
                SCOPE.removesuffix(".") + ", built on 2020-12-14.",
                "This bulletin excludes model year 2021 Ficta Norvale vehicles with engine MX204 "
                "manufactured on 2020-12-10.",
                SUBJECT,
            ],
            "exact",
        ),
        (
            {},
            [SCOPE.removesuffix(".") + ", bui[redacted] and 2021-01-01.", SUBJECT],
            "analog",
        ),
        (
            {},
            [SCOPE, "This bulletin excludes vehicles built before 2021-01-01.", SUBJECT],
            "analog",
        ),
    ],
)
def test_applicability_keeps_scope_exceptions_and_subjects_together(overrides, sentences, classification):
    passages = [Passage(sentence, page, URL) for page, sentence in enumerate(sentences, 1)]
    result = assess_applicability(CONTEXT | overrides, passages)
    assert result.label == classification
    for evidence in result.evidence:
        assert evidence["quote"] in sentences
    if classification == "conflict":
        assert {citation["kind"] for citation in result.evidence} >= {"vehicle_scope", "subject", "exclusion"}


@pytest.mark.parametrize("majority_exact", [False, True])
def test_report_keeps_independent_confidence_sources_in_shortlist(monkeypatch, majority_exact):
    major = [f"https://static.nhtsa.gov/synthetic/{index:02}" for index in range(20)]
    minor = ["https://www.denso.com/synthetic/primary", "https://www.bosch.com/synthetic/secondary"]
    documents = {
        url: SCOPE + f" {SUBJECT} Bulletin edition {index}."
        if majority_exact or url in minor
        else f"This bulletin explicitly excludes model year 2021 Ficta Norvale vehicles. Edition {index}."
        for index, url in enumerate([*major, *minor])
    }

    def fetch(url, **_kwargs):
        return {"ok": True, "url": url, "kind": "text", "text": documents[url]}

    monkeypatch.setattr(j1, "fetch_document", fetch)
    started = j1.research_sources(
        objective="Check synthetic evidence coverage",
        source_urls=major,
        idempotency_key="scope-coverage-1",
        automotive_context=CONTEXT,
    )
    assert started["ok"], started
    job_id = started["job_id"]
    assert j1.research_sources(job_id=job_id, source_urls=minor)["ok"]
    j1.run_worker(once=True)
    report = j1.research_report(job_id)["report"]
    assert report["evidence_confidence"]["level"] == "high"
    assert report["evidence_confidence"]["retrieved_sources"] == len(documents)
    assert len(report["sources"]) == 20
    assert {source["url"] for source in report["sources"]} == set(
        [*major[:19], minor[1]] if majority_exact else [*major[:18], *minor]
    )
    assert [source["url"] for source in report["sources"] if source["source_tier"] == "A"] == (
        major[:19] if majority_exact else major[:18]
    )


def test_sources_only_context_is_immutable_and_scope_citations_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    context = {
        **CONTEXT,
        "market": "Europe",
        "production_date": "2020-12-14",
        "position": "front left",
    }
    scope = (
        "This bulletin applies to European model year 2021 Ficta Norvale vehicles with engine MX204, "
        "produced on 2020-12-14, in the front left position."
    )
    subject = "P1A27 means a connector feedback fault."
    html = (
        f'<h1 id="scope">Vehicle applicability</h1><p>{scope}</p><h2 id="code">Technical details</h2><p>{subject}</p>'
    )

    def no_search(*_args, **_kwargs):
        pytest.fail("Reading supplied sources must not issue searches")

    def fetch(url, **_kwargs):
        return j1_fetch._extract_static_content("text/html", html.encode(), url, {}) | {"url": url}

    monkeypatch.setattr(j1, "search_public", no_search)
    monkeypatch.setattr(j1, "fetch_document", fetch)
    args = {"objective": "Check a synthetic bulletin", "source_urls": [URL], "idempotency_key": "scope-test-1"}
    created = j1.research_sources(**args, automotive_context=context)
    assert created["ok"], created
    job_id = created["job_id"]
    replay = j1.research_sources(**args, automotive_context=dict(reversed(list(context.items()))))
    assert replay["ok"] and replay["job_id"] == job_id and replay["replayed"]
    assert j1.research_sources(**args, automotive_context={**context, "year": 2020})["error"] == {
        "code": "idempotency_conflict"
    }
    assert j1.research_sources(job_id=job_id, source_urls=[URL], automotive_context=context)["error"] == {
        "code": "source_target_invalid"
    }
    j1.run_worker(once=True)
    status = j1.research_status(job_id)
    assert status["acquisition_mode"] == "sources_only"
    assert status["automotive_context"] == context
    assert status["queries_total"] == 0 and status["queries_remaining"] == 0
    report = j1.research_report(job_id)["report"]
    source = report["confirmed"][0]
    assert source["applicability"] == "exact"
    assert not report["fitment_confirmed"]
    assert {item["source_url"] for item in source["applicability_evidence"]} == {URL + "#scope", URL + "#code"}
    for item in source["applicability_evidence"]:
        doc = j1.research_document(job_id, source["document_id"], page=item["page"])
        assert item["quote"] in doc["text"]


def test_source_context_invalid_and_legacy_receipt_hash_unchanged(tmp_path: Path) -> None:
    objective = "Check a synthetic bulletin"
    urls = [URL]
    arguments = {"objective": objective, "source_urls": urls, "idempotency_key": "legacy-scope-1"}
    private = "WBA" + "0" * 14
    for fields in (
        {"production_date": "2020-02-30"},
        {"market": "safe " * 100 + private},
        {"position": "owner@example.com"},
    ):
        assert not j1.research_sources(**arguments, automotive_context={**CONTEXT, **fields})["ok"]
    assert not (tmp_path / "public").exists()
    created = j1.research_sources(**arguments)
    assert created["ok"]
    legacy_hash = hashlib.sha256(
        json.dumps([objective, urls], ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()
    with j1._db(readonly=True) as conn:
        assert conn.execute("SELECT request_hash FROM supplied_source_starts").fetchone()[0] == legacy_hash
    assert j1.research_sources(**arguments, automotive_context=None)["job_id"] == created["job_id"]
