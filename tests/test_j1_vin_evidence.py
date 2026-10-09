"""Offline evidence and lifecycle tests; all VINs are constructed test inputs."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import time
from typing import Any
from uuid import uuid4

import pytest

from autostop_manager import j1_research as general
from autostop_manager import j1_vin_research as api
from autostop_manager import j1_vin_store as store
from autostop_manager import j1_vin_worker as worker

VIN = "1HGCM82673A000000"
OTHER_VIN = "WVWZZZ1JZXW000000"


@dataclass
class Sandbox:
    root: Path
    monkeypatch: pytest.MonkeyPatch


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Sandbox]:
    if not Path("/dev/shm").is_dir():
        pytest.skip("private tmpfs test storage is unavailable")
    with tempfile.TemporaryDirectory(prefix="autostop-j1-vin-evidence-", dir="/dev/shm") as directory:
        root = Path(directory)
        root.chmod(0o700)
        if not store._is_tmpfs(root):
            pytest.skip("VIN state requires tmpfs")
        monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "general"))
        monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(root))
        monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "1")
        monkeypatch.setattr(general, "_STOP", False)
        monkeypatch.setattr(general.signal, "signal", lambda *_args: None)

        def forbidden(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("This evidence test must not access the network")

        monkeypatch.setattr(worker, "request_vin", forbidden)
        monkeypatch.setattr(worker, "search_vin", forbidden)
        monkeypatch.setattr(worker.fetch, "fetch_browser_document", forbidden)
        yield Sandbox(root, monkeypatch)


def _start(*, fields: list[str] | None = None, complete: bool = True) -> str:
    result = api.j1_research_vin(VIN, f"start-{uuid4().hex}")
    assert result["ok"], result
    job = result["job_id"]
    with store.connect(job, transaction=True) as conn:
        current = store.metadata(conn)
        current["fields"] = fields or ["engine"]
        if complete:
            conn.execute("UPDATE queries SET status='done'")
            current["collection_status"] = "completed"
        store.write_metadata(conn, current)
    if complete:
        store.set_stub(job, "completed")
    return job


def _document(
    job: str,
    text: str = "Engine Alpha; power 100 kW; transmission Manual.",
    *,
    vin: str | None = VIN,
    pages: list[dict[str, Any]] | None = None,
    proof_allowed: bool = True,
    pdf: bool = False,
) -> dict[str, Any]:
    identifier = uuid4().hex
    raw_pages = pages if pages is not None else [{"page": 1, "text": (f"VIN {vin}. " if vin else "") + text}]
    with store.connect(job, transaction=True) as conn:
        current = store.metadata(conn)
        conn.execute(
            """INSERT INTO documents(id,url,title,status,kind,method,source_class,source_tier,source_basis,
               retrieved_at,page_count,raw_file) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                identifier,
                f"https://example.org/synthetic/{identifier}",
                "Synthetic reference",
                "fetched",
                "pdf" if pdf else "html",
                "pdf_text" if pdf else "html_text",
                "manufacturer",
                "A",
                "registry:synthetic-test",
                time.time(),
                max(page["page"] for page in raw_pages),
                f"{identifier}.pdf" if pdf else "",
            ),
        )
        api.ingest_pages(conn, current, identifier, raw_pages, proof_allowed=proof_allowed)
        store.write_metadata(conn, current)
    if pdf:
        raw_file = store.job_directory(job) / f"{identifier}.pdf"
        raw_file.write_bytes(b"%PDF-1.4 synthetic offline input")
        raw_file.chmod(0o600)
    result = api.research_document(job, identifier)
    assert result["ok"], result
    return result


def _claim(document: dict[str, Any], **changes: Any) -> dict[str, Any]:
    result = {
        "claim_id": uuid4().hex,
        "field": "engine",
        "value": "Alpha",
        "unit": "",
        "document_id": document["document_id"],
        "document_revision": document["document_revision"],
        "quote": "Engine Alpha; power 100 kW; transmission Manual.",
        "page": document["page"],
        "relationship": "vin_specific",
        "derivation": "direct",
        "basis_claim_ids": [],
        "match_evidence_id": document["match_evidence_id"],
    }
    result.update(changes)
    return result


def _record(job: str, facts: list[dict[str, Any]], **changes: Any) -> dict[str, Any]:
    options = {
        "expected_revision": api.research_status(job)["revision"],
        "idempotency_key": f"facts-{uuid4().hex}",
        "finalize": False,
    }
    options.update(changes)
    return api.j1_research_record_facts(job, facts=facts, **options)


def _error(result: dict[str, Any], code: str) -> None:
    assert not result["ok"] and result["error"]["code"] == code, result


@pytest.mark.parametrize("source_vin", [None, OTHER_VIN])
def test_no_proof_or_different_vin_can_only_supply_family_fact(sandbox: Sandbox, source_vin: str | None) -> None:
    job = _start()
    document = _document(job, vin=source_vin)
    assert document["match_evidence_id"] is None
    _error(_record(job, [_claim(document)]), "vin_match_evidence_required")
    assert api.research_report(job)["claims"] == []
    result = _record(job, [_claim(document, relationship="family")], finalize=True)
    assert result["ok"] and result["analysis_status"] == "ready_partial"
    report = api.research_report(job)
    assert report["unknown_fields"] == ["engine"] and report["claims"][0]["relationship"] == "family"
    assert VIN not in json.dumps(report) and OTHER_VIN not in json.dumps(report)


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"match_evidence_id": "a" * 32}, "vin_match_evidence_required"),
        ({"document_revision": 2}, "fact_document_revision_invalid"),
        ({"document_id": "a" * 32}, "fact_document_revision_invalid"),
        ({"page": 2}, "vin_match_evidence_required"),
        ({"page": 3}, "fact_document_revision_invalid"),
        ({"quote": "Engine Beta"}, "fact_quote_invalid"),
        ({"value": "Beta"}, "fact_value_not_in_quote"),
        ({"confidence": 99}, "fact_schema_invalid"),
    ],
)
def test_forged_citation_proof_revision_or_page_is_rejected(
    sandbox: Sandbox, changes: dict[str, Any], error: str
) -> None:
    job = _start()
    quote = "Engine Alpha; power 100 kW; transmission Manual."
    document = _document(job, pages=[{"page": 1, "text": VIN + " " + quote}, {"page": 2, "text": quote}])
    _error(_record(job, [_claim(document, **changes)]), error)
    assert api.research_report(job)["claims"] == []


def test_literal_match_is_linkage_and_report_never_certifies_factory_configuration(sandbox: Sandbox) -> None:
    job = _start()
    document = _document(job)
    assert document["match_kind"] == "literal_only" and VIN not in document["text"]
    recorded = _record(job, [_claim(document)], finalize=True)
    assert recorded["ok"]
    report = api.research_report(job)
    claim = report["claims"][0]
    assert claim["citation_verified"] and claim["semantic_assessment"] == "agent"
    assert claim["support"] == "single_source" and claim["evidence_current"]
    assert any("does not prove factory configuration" in limitation for limitation in report["limitations"])
    assert "factory_verified" not in claim and VIN not in json.dumps(report)


def test_browser_redacted_page_cannot_create_vin_specific_proof(sandbox: Sandbox) -> None:
    job = _start()
    document = _document(job, proof_allowed=False)
    assert document["match_evidence_id"] is None
    _error(_record(job, [_claim(document)]), "vin_match_evidence_required")


@pytest.mark.parametrize(
    "basis,reason",
    [([], "reasoned comparison"), (["a" * 32], "reasoned comparison"), (["bad-id"], "reasoned comparison")],
)
def test_inference_needs_existing_current_basis(sandbox: Sandbox, basis: list[str], reason: str) -> None:
    job = _start()
    document = _document(job)
    result = _record(
        job, [_claim(document, value="Derived", derivation="inference", basis_claim_ids=basis, reasoning=reason)]
    )
    _error(result, "fact_inference_basis_required" if not basis else "fact_inference_basis_invalid")


def test_valid_inference_records_basis_and_reasoning_separately(sandbox: Sandbox) -> None:
    job = _start(fields=["engine_code"])
    document = _document(job)
    basis = _claim(document)
    assert _record(job, [basis])["ok"]
    inferred = _claim(
        document,
        field="engine_code",
        value="Derived",
        derivation="inference",
        basis_claim_ids=[basis["claim_id"]],
        reasoning="Interpretation based on the cited Alpha family.",
        support="hypothesis",
    )
    result = _record(job, [inferred], finalize=True)
    assert result["ok"] and result["analysis_status"] == "ready_partial"
    retained = api.research_report(job)["claims"][1]
    assert retained["basis_claim_ids"] == [basis["claim_id"]] and retained["derivation"] == "inference"
    assert retained["support"] == "hypothesis" and "Interpretation" in retained["reasoning"]


def test_inference_becomes_stale_when_its_basis_document_changes(sandbox: Sandbox) -> None:
    job = _start(fields=["engine_code"])
    basis_document = _document(job)
    inference_document = _document(job)
    basis = _claim(basis_document)
    assert _record(job, [basis])["ok"]
    inferred = _claim(
        inference_document,
        field="engine_code",
        value="Derived",
        derivation="inference",
        basis_claim_ids=[basis["claim_id"]],
        reasoning="An interpretation of the referenced source record.",
    )
    assert _record(job, [inferred], finalize=True)["ok"]
    with store.connect(job, transaction=True) as conn:
        current = store.metadata(conn)
        conn.execute("UPDATE documents SET version=version+1 WHERE id=?", (basis_document["document_id"],))
        api.ingest_pages(conn, current, basis_document["document_id"], [{"page": 1, "text": VIN + " Engine Updated."}])
        store.write_metadata(conn, current)
    report = api.research_report(job)
    by_id = {claim["claim_id"]: claim for claim in report["claims"]}
    assert not by_id[basis["claim_id"]]["evidence_current"]
    assert not by_id[inferred["claim_id"]]["evidence_current"]
    assert "engine_code" in report["unknown_fields"]
    assert report["analysis_status"] == "draft"


def test_conflicting_values_are_retained_and_never_auto_resolved(sandbox: Sandbox) -> None:
    job = _start()
    alpha = _document(job)
    beta = _document(job, "Engine Beta; power 120 kW.")
    first = _claim(alpha)
    second = _claim(beta, value="Beta", quote="Engine Beta; power 120 kW.")
    result = _record(job, [first, second], finalize=True)
    assert result["ok"] and result["analysis_status"] == "ready_partial"
    report = api.research_report(job)
    assert {claim["value"] for claim in report["claims"]} == {"Alpha", "Beta"}
    assert report["conflicts"][0]["requires_agent_review"]
    assert set(report["conflicts"][0]["claim_ids"]) == {first["claim_id"], second["claim_id"]}


@pytest.mark.parametrize("status,inflight", [("queued", False), ("running", False), ("completed", True)])
def test_finalize_requires_collection_quiescence(sandbox: Sandbox, status: str, inflight: bool) -> None:
    job = _start()
    document = _document(job)
    with store.connect(job, transaction=True) as conn:
        current = store.metadata(conn)
        current.update(collection_status=status, inflight=inflight)
        store.write_metadata(conn, current)
    _error(_record(job, [_claim(document)], finalize=True), "collection_not_quiescent")
    assert api.research_report(job)["claims"] == []


def test_revision_and_idempotency_conflicts_do_not_append_or_replay_mutations(sandbox: Sandbox) -> None:
    job = _start()
    document = _document(job)
    claim = _claim(document)
    revision = api.research_status(job)["revision"]
    key = f"facts-{uuid4().hex}"
    first = _record(job, [claim], expected_revision=revision, idempotency_key=key, finalize=True)
    assert first["ok"]
    assert _record(job, [claim], expected_revision=revision, idempotency_key=key, finalize=True) == first
    _error(_record(job, [claim], expected_revision=revision, idempotency_key=key), "idempotency_conflict")
    _error(_record(job, [claim], expected_revision=revision), "revision_conflict")
    assert len(api.research_report(job)["claims"]) == 1
    extended = api.research_add_queries(job, ["Synthetic Alpha engine additional source"])
    assert extended["ok"] and extended["added"] == 1
    replay = _record(job, [claim], expected_revision=revision, idempotency_key=key, finalize=True)
    assert replay == first
    current = api.research_status(job)
    assert current["revision"] == extended["revision"] and current["analysis_status"] == "draft"


def test_claim_collision_and_invalid_batch_leave_previous_claims_unchanged(sandbox: Sandbox) -> None:
    job = _start()
    document = _document(job, "Engine Alpha and Beta; power 100 kW.")
    claim = _claim(document, quote="Engine Alpha and Beta; power 100 kW.")
    assert _record(job, [claim])["ok"]
    _error(_record(job, [{**claim, "value": "Beta"}]), "claim_id_conflict")
    assert _record(job, [claim])["ok"]
    assert len(api.research_report(job)["claims"]) == 1
    another = _claim(document, quote=claim["quote"])
    _error(_record(job, [another, {**another, "claim_id": uuid4().hex, "quote": "forged"}]), "fact_quote_invalid")
    assert [item["claim_id"] for item in api.research_report(job)["claims"]] == [claim["claim_id"]]


def test_offline_worker_marks_source_copies_without_auto_corroboration(sandbox: Sandbox) -> None:
    urls = ["https://www.nhtsa.gov/synthetic-source-one.html", "https://www.nhtsa.gov/synthetic-source-two.html"]
    calls: list[str] = []
    sandbox.monkeypatch.setattr(
        worker,
        "search_vin",
        lambda *_args, **_kwargs: {
            "ok": True,
            "results": [{"url": url} for url in urls],
            "providers": ["bing"],
            "errors": [],
        },
    )

    def request(url: str, *_args: Any, **_kwargs: Any) -> tuple[int, dict[str, str], bytes, str]:
        calls.append(url)
        return (
            200,
            {"content-type": "text/html"},
            f"<p>{VIN}</p><p>Engine Alpha; power 100 kW; transmission Manual.</p>".encode(),
            url,
        )

    sandbox.monkeypatch.setattr(worker, "request_vin", request)
    job = _start(complete=False)
    general.run_worker(once=True)
    status = api.research_status(job)
    assert status["collection_status"] == "completed" and status["analysis_status"] == "draft"
    results = api.research_results(job)
    items = results["results"]
    assert len(items) == 2 and calls == urls and sum(bool(item["duplicate_of"]) for item in items) == 1
    documents = [api.research_document(job, item["document_id"]) for item in items]
    assert _record(job, [_claim(document) for document in documents], finalize=True)["ok"]
    report = api.research_report(job)
    assert all(claim["support"] == "single_source" for claim in report["claims"])
    assert sum(bool(source["duplicate_of"]) for source in report["sources"]) == 1
    assert report["conflicts"] == []


def test_ocr_version_invalidates_old_claims_and_current_report_keeps_history(sandbox: Sandbox) -> None:
    job = _start()
    document = _document(
        job,
        pages=[
            {"page": 1, "text": VIN + " Engine Alpha; power 100 kW; transmission Manual."},
            {"page": 2, "text": "scanned page"},
        ],
        pdf=True,
    )
    old_claim = _claim(document)
    assert _record(job, [old_claim], finalize=True)["ok"]
    sandbox.monkeypatch.setattr(
        worker,
        "ocr_pages",
        lambda *_args, **_kwargs: {
            "ok": True,
            "kind": "pdf",
            "extraction_method": "pdf_ocr",
            "pages": [{"page": 2, "text": VIN + " Engine Beta; power 120 kW."}],
        },
    )
    queued = api.research_document(job, document["document_id"], page=2, ocr=True)
    assert queued["ok"] and queued["ocr_status"] == "queued"
    assert api.research_status(job)["analysis_status"] == "draft"
    general.run_worker(once=True)
    current_page = api.research_document(job, document["document_id"], page=2)
    assert current_page["document_revision"] == document["document_revision"] + 1
    assert current_page["match_kind"] == "literal_only"
    _error(_record(job, [old_claim]), "fact_document_revision_invalid")
    current_claim = _claim(current_page, value="Beta", quote="Engine Beta; power 120 kW.")
    assert _record(job, [current_claim], finalize=True)["ok"]
    report = api.research_report(job)
    assert [claim["evidence_current"] for claim in report["claims"]] == [False, True]
    assert report["unknown_fields"] == [] and report["conflicts"] == []
    assert report["analysis_status"] == "ready_partial"


def test_expired_job_cannot_accept_claims_and_removes_ephemeral_evidence(sandbox: Sandbox) -> None:
    job = _start()
    document = _document(job)
    with store.connect(job, transaction=True) as conn:
        current = store.metadata(conn)
        current["expires_at"] = time.time() - 1
        store.write_metadata(conn, current)
    result = api.j1_research_record_facts(
        job, expected_revision=0, facts=[_claim(document)], idempotency_key=f"expired-{uuid4().hex}"
    )
    assert not result["ok"] and result["error"]["code"] in {"vin_job_expired", "vin_ephemeral_state_lost"}
    assert not (sandbox.root / job).exists()
    assert not api.research_document(job, document["document_id"])["ok"]
    with general._db() as conn:
        stub = conn.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
    assert stub["status"] == "failed" and VIN not in json.dumps(dict(stub))


def test_proof_from_another_document_or_job_cannot_be_reused(sandbox: Sandbox) -> None:
    first_job = _start()
    first_document = _document(first_job)
    second_job = _start()
    second_document = _document(second_job)
    _error(_record(second_job, [_claim(first_document)]), "fact_document_revision_invalid")
    _error(
        _record(second_job, [_claim(second_document, match_evidence_id=first_document["match_evidence_id"])]),
        "vin_match_evidence_required",
    )
    assert api.research_report(second_job)["claims"] == []


def test_inference_rejects_stale_basis_and_empty_reasoning(sandbox: Sandbox) -> None:
    job = _start()
    document = _document(job)
    basis = _claim(document)
    assert _record(job, [basis])["ok"]
    inferred = _claim(document, value="Derived", derivation="inference", basis_claim_ids=[basis["claim_id"]])
    _error(_record(job, [inferred]), "fact_inference_basis_required")
    with store.connect(job, transaction=True) as conn:
        conn.execute("UPDATE documents SET version=version+1 WHERE id=?", (document["document_id"],))
    current_document = api.research_document(job, document["document_id"])
    inferred = _claim(
        current_document,
        value="Derived",
        derivation="inference",
        basis_claim_ids=[basis["claim_id"]],
        reasoning="Source-based inference.",
    )
    _error(_record(job, [inferred]), "fact_inference_basis_invalid")
    assert len(api.research_report(job)["claims"]) == 1


def test_shared_inference_basis_graph_has_bounded_traversal(sandbox: Sandbox) -> None:
    job = _start()
    document = _document(job)
    previous = [_claim(document), _claim(document)]
    assert _record(job, previous)["ok"]
    count = len(previous)
    for _level in range(5):
        current = [
            _claim(
                document,
                value="Derived",
                derivation="inference",
                basis_claim_ids=[claim["claim_id"] for claim in previous],
                reasoning="Inference from two existing source-linked claims.",
                support="hypothesis",
            )
            for _ in range(2)
        ]
        assert _record(job, current)["ok"]
        previous = current
        count += len(current)
    with store.connect(job) as conn:
        reads = [0]

        class CountedConnection:
            def execute(self, sql: str, values: tuple[Any, ...]) -> Any:
                reads[0] += 1
                return conn.execute(sql, values)

        assert api._claim_current(CountedConnection(), previous[0])  # type: ignore[arg-type]
        assert reads[0] <= 5 * count, "Shared evidence dependencies must not trigger exponential database traversal"


def test_corroborated_support_requires_source_linked_basis(sandbox: Sandbox) -> None:
    job = _start()
    document = _document(job)
    _error(_record(job, [_claim(document, support="corroborated")]), "fact_corroboration_basis_required")
    assert api.research_report(job)["claims"] == []


def test_content_copy_cannot_corroborate_original_source(sandbox: Sandbox) -> None:
    job = _start()
    original = _document(job)
    copied = _document(job)
    with store.connect(job, transaction=True) as conn:
        conn.execute("UPDATE documents SET duplicate_of=? WHERE id=?", (original["document_id"], copied["document_id"]))
    basis = _claim(original)
    assert _record(job, [basis])["ok"]
    _error(
        _record(job, [_claim(copied, support="corroborated", basis_claim_ids=[basis["claim_id"]])]),
        "fact_corroboration_basis_required",
    )
    assert len(api.research_report(job)["claims"]) == 1


def test_separate_content_cluster_with_same_fact_can_corroborate(sandbox: Sandbox) -> None:
    job = _start()
    first = _document(job)
    second = _document(
        job, "Engine Alpha; power 100 kW; transmission Manual. Additional independent publication context."
    )
    basis = _claim(first)
    assert _record(job, [basis])["ok"]
    claim = _claim(second, support="corroborated", basis_claim_ids=[basis["claim_id"]])
    result = _record(job, [claim], finalize=True)
    assert result["ok"]
    report = api.research_report(job)
    assert report["claims"][1]["support"] == "corroborated"
    assert report["claims"][1]["basis_claim_ids"] == [basis["claim_id"]]
    assert report["unknown_fields"] == [] and report["conflicts"] == []
    assert len({source["document_id"] for source in report["sources"]}) == 2
