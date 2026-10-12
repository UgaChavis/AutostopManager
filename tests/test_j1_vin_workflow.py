"""Native MCP workflow with constructed identifiers and offline transports."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
import io
import json
import logging
from pathlib import Path
import shutil
import socket
import tempfile
import time
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolRequest, CallToolRequestParams
import pytest

from autostop_manager import config, j1_research as general
from autostop_manager import j1_vin_store as store
from autostop_manager import j1_vin_worker as worker
from autostop_manager import mcp_telemetry, mcp_tools
from autostop_manager.storage import StoreState

VIN = "1HGCM82673A000000"
TOOLS = {
    "j1_research_vin",
    "j1_research_status",
    "j1_research_results",
    "j1_research_document",
    "j1_research_record_facts",
    "j1_research_report",
    "j1_research_add_queries",
    "j1_research_cancel",
}


@pytest.fixture
def native_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[FastMCP, Path, io.StringIO]]:
    if not Path("/dev/shm").is_dir():
        pytest.skip("VIN workflow requires private tmpfs")
    with tempfile.TemporaryDirectory(prefix="autostop-vin-native-workflow-", dir="/dev/shm") as folder:
        root = Path(folder)
        root.chmod(0o700)
        if not store._is_tmpfs(root):
            pytest.skip("VIN workflow requires private tmpfs")
        monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
        monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "manager.sqlite3"))
        monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "general"))
        monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(root))
        monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "1")
        monkeypatch.setattr(config, "_ENV_LOADED", False)
        monkeypatch.setattr(general, "_STOP", False)
        monkeypatch.setattr(general.signal, "signal", lambda *_args: None)

        def forbidden(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("native workflow must not access a network or business service")

        monkeypatch.setattr(socket, "getaddrinfo", forbidden)
        monkeypatch.setattr(socket, "create_connection", forbidden)
        monkeypatch.setattr(worker.fetch, "fetch_browser_document", forbidden)
        monkeypatch.setattr(general, "search_public", forbidden)
        trace = io.StringIO()
        monkeypatch.setattr(mcp_telemetry.LOGGER, "handlers", [logging.StreamHandler(trace)])
        server = FastMCP("isolated-vin-workflow")
        mcp_tools.register_manager_tools(server, StoreState(tmp_path / "manager.sqlite3"), include_tools=TOOLS)
        yield server, root, trace


def _call(server: FastMCP, name: str, **arguments: Any) -> dict[str, Any]:
    async def invoke() -> dict[str, Any]:
        handler = server._mcp_server.request_handlers[CallToolRequest]
        response = await handler(CallToolRequest(params=CallToolRequestParams(name=name, arguments=arguments)))
        result = response.root
        assert result.isError is False, result
        assert result.structuredContent is not None
        payload = result.structuredContent
        assert len(result.content) == 1
        assert json.loads(result.content[0].text) == payload
        assert VIN not in json.dumps(payload)
        return payload

    return asyncio.run(invoke())


def _pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 40 700 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
    ]
    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(body)
    body.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    body.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]))
    body.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(body)


def _fact(document: dict[str, Any], value: str) -> dict[str, Any]:
    return {
        "field": "engine",
        "value": value,
        "quote": f"Engine {value}.",
        "document_id": document["document_id"],
        "document_revision": document["document_revision"],
        "page": 1,
        "relationship": "vin_specific",
        "match_evidence_id": document["match_evidence_id"],
    }


@pytest.mark.parametrize("kind", ["html", "pdf"])
def test_native_collection_analysis_ocr_cancel_and_expiry(native_workflow, monkeypatch, kind: str) -> None:
    if kind == "pdf" and not shutil.which("pdftotext"):
        pytest.skip("real PDF text extraction is unavailable")
    server, root, trace = native_workflow
    exact_url = f"https://example.org/synthetic/{VIN}/reference.{kind}"
    family_url = "https://example.org/synthetic/family.html"
    calls: list[str] = []

    def search(*_args: Any) -> dict[str, Any]:
        calls.append("search")
        return {
            "ok": True,
            "results": [{"url": url, "title": "Synthetic reference"} for url in (exact_url, family_url)],
            "providers": [{"provider": "bing", "outcome": "results"}],
            "errors": [],
        }

    def download(url: str, *_args: Any, **_kwargs: Any) -> tuple[int, dict[str, str], bytes, str]:
        calls.append("download")
        text = f"VIN {VIN}; Engine Alpha." if url == exact_url else "Family engine Alpha."
        pdf = kind == "pdf" and url == exact_url
        body = _pdf(text) if pdf else f"<p>{text}</p>".encode()
        return 200, {"content-type": "application/pdf" if pdf else "text/html"}, body, url

    monkeypatch.setattr(worker, "search_vin", search)
    monkeypatch.setattr(worker, "request_vin", download)
    started = _call(server, "j1_research_vin", vin=VIN, idempotency_key="native-workflow-start")
    assert started["ok"]
    job = started["job_id"]
    replay = _call(server, "j1_research_vin", vin=VIN, idempotency_key="native-workflow-start")
    assert replay["job_id"] == job
    general.run_worker(once=True)
    status = _call(server, "j1_research_status", job_id=job)
    assert status["collection_status"] == "completed"
    assert status["analysis_status"] == "draft"
    assert status["fetched"] == 2
    results = _call(server, "j1_research_results", job_id=job, query="Alpha")
    assert results["total"] == 2
    docs = [
        _call(server, "j1_research_document", job_id=job, document_id=row["document_id"], page=1)
        for row in results["results"]
    ]
    linked = next(doc for doc in docs if doc["match_evidence_id"])
    assert not next(doc for doc in docs if doc != linked)["match_evidence_id"]
    recorded = _call(
        server,
        "j1_research_record_facts",
        job_id=job,
        expected_revision=status["revision"],
        facts=[_fact(linked, "Alpha")],
        idempotency_key="native-workflow-facts",
        finalize=True,
    )
    assert recorded["ok"] and recorded["analysis_status"] == "ready_partial"
    report = _call(server, "j1_research_report", job_id=job)
    assert report["finalized_revision"] == report["revision"]
    assert report["claims"][0]["evidence_current"]
    assert "engine" not in report["unknown_fields"] and "history" in report["unknown_fields"]
    if kind == "pdf":

        def ocr(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"ok": True, "pages": [{"page": 1, "text": f"VIN {VIN}; Engine Beta."}]}

        monkeypatch.setattr(worker, "ocr_pages", ocr)
        before_calls = list(calls)
        queued = _call(server, "j1_research_document", job_id=job, document_id=linked["document_id"], page=1, ocr=True)
        assert queued["ok"] and queued["ocr_status"] == "queued"
        duplicate = _call(
            server, "j1_research_document", job_id=job, document_id=linked["document_id"], page=1, ocr=True
        )
        assert duplicate["ok"] and duplicate["ocr_status"] == "pending"
        with store.connect(job) as conn:
            assert conn.execute("SELECT COUNT(*) FROM ocr_requests").fetchone()[0] == 1
        general.run_worker(once=True)
        assert calls == before_calls
        changed = _call(server, "j1_research_document", job_id=job, document_id=linked["document_id"], page=1)
        assert changed["document_revision"] == linked["document_revision"] + 1
        assert "Engine Beta." in changed["text"]
        report = _call(server, "j1_research_report", job_id=job)
        assert report["analysis_status"] == "draft" and not report["claims"][0]["evidence_current"]
        fixed = _call(
            server,
            "j1_research_record_facts",
            job_id=job,
            expected_revision=report["revision"],
            facts=[_fact(changed, "Beta")],
            idempotency_key="native-workflow-ocr-facts",
            finalize=True,
        )
        assert fixed["ok"] and fixed["analysis_status"] == "ready_partial"

    expanded = _call(server, "j1_research_add_queries", job_id=job, queries=["Synthetic supplemental engine data"])
    assert expanded["ok"] and expanded["added"] == 1
    expanded_status = _call(server, "j1_research_status", job_id=job)
    assert expanded_status["analysis_status"] == "draft"
    assert expanded_status["budget"]["queries"]["used"] == 4
    cancelled = _call(server, "j1_research_cancel", job_id=job)
    assert cancelled["collection_status"] == "cancelled" and not cancelled["network_inflight"]
    before_calls = list(calls)
    worker.run_job(job)
    assert calls == before_calls
    with general._db(readonly=True) as conn:
        durable = "\n".join(conn.iterdump())
    assert VIN not in durable and "Engine Alpha." not in durable and "Engine Beta." not in durable
    assert "mcp_tool_start" in trace.getvalue() and "mcp_tool_end" in trace.getvalue()
    assert VIN not in trace.getvalue() and exact_url not in trace.getvalue()
    with store.connect(job, transaction=True) as conn:
        state = store.metadata(conn)
        state["expires_at"] = time.time() - 1
        store.write_metadata(conn, state)
    expired = _call(server, "j1_research_status", job_id=job)
    assert not expired["ok"] and expired["error"]["code"] in {"vin_job_expired", "vin_ephemeral_state_lost"}
    assert not (root / job).exists()
    assert calls == before_calls
