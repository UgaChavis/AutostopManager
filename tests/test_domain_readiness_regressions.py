"""Synthetic privacy/origin and corpus regressions from the source43 review."""

from contextlib import contextmanager
import copy
import hashlib
import json
import socket
import sqlite3
import time
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlsplit

import httpx
import pytest

from autostop_manager import bounded_http_read as bounded
from autostop_manager import catalog_clients as catalog
from autostop_manager import j1_research as j1
from autostop_manager import j1_vin_network as network
from autostop_manager import j1_vin_store as store
from autostop_manager import j1_vin_worker as worker
from autostop_manager import public_automotive_evidence as evidence
from autostop_manager import web_research_gateway as gateway


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*_args, **_kwargs):
        pytest.fail("External network is forbidden in domain regression tests")

    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


@pytest.mark.parametrize("private", ["X" * 17, "owner@example.org", "token=synthetic-secret-value"])
@pytest.mark.parametrize("field", ["make", "model"])
def test_public_evidence_rejects_private_context_before_fetch_or_echo(monkeypatch, private, field):
    monkeypatch.setattr(evidence, "_safe_get_bytes", lambda *_a, **_k: pytest.fail("private request"))
    context = {"make": "Mercedes-Benz", "model": "Synthetic", "model_year": 2020, field: private}
    for lookup in (
        evidence.lookup_nhtsa_recalls,
        evidence.lookup_nhtsa_tsb_metadata,
        evidence.lookup_public_automotive_evidence,
    ):
        result = lookup(**context)
        assert result["ok"] is False
        assert result["error"] == "public_automotive_private_input"
        assert private not in json.dumps(result)


@pytest.mark.parametrize("private", ["X" * 17, "owner@example.org", "token=synthetic-secret-value"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_public_catalogs_reject_private_input_before_planning_or_io(monkeypatch, private, dry_run):
    monkeypatch.setattr(catalog, "urlopen", lambda *_a, **_k: pytest.fail("private request"))
    lookups = [catalog.mann_filter_catalog_lookup, catalog.denso_aftermarket_catalog_lookup, catalog.exist_price_lookup]
    for lookup in lookups:
        result = lookup(part_number=private, dry_run=dry_run)
        assert result["ok"] is False
        assert result["error"] == "public_catalog_private_input"
        assert result["privacy"]["raw_identifier_is_sensitive"] is True
        assert private not in json.dumps(result)
    for build in (
        catalog.build_mann_filter_catalog_request,
        catalog.build_denso_aftermarket_search_request,
        catalog.build_exist_price_lookup_request,
    ):
        assert build(part_number=private)["ok"] is False
        assert build(part_number="90919-01275")["ok"] is True


@pytest.mark.parametrize("target", ["http://127.0.0.1/private", "https://foreign.invalid/report", "/same-origin"])
def test_nhtsa_transport_rejects_redirect_before_a_second_request(monkeypatch, target):
    requests = []
    client = httpx.AsyncClient

    def handle(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": target}, content=b"redirect")

    monkeypatch.setattr(bounded.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handle), **kw))
    with pytest.raises(HTTPError) as error:
        evidence._safe_get_bytes(evidence.NHTSA_RECALLS_API_URL, timeout=2)
    assert error.value.code == 302
    assert requests == [evidence.NHTSA_RECALLS_API_URL]


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1/Price/?pid=1",
        "https://other.invalid/Price/?pid=1",
        "http://www.exist.ru/Price/?pid=1",
        "//other.invalid/Price/?pid=1",
        "https://user@www.exist.ru/Price/?pid=1",
    ],
)
def test_exist_followup_stays_on_its_configured_https_origin(monkeypatch, target):
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, *_args):
            return self.payload

    def respond(request, **_kwargs):
        calls.append(request.full_url)
        return Response(
            b"[]" if "/Api/Parts/Search?" in request.full_url else f'<a href="{target}"><b>MANN</b> C2029</a>'.encode()
        )

    monkeypatch.setattr(catalog, "urlopen", respond)
    catalog.exist_price_lookup(part_number="C2029")
    assert len(calls) == 2
    assert all(urlsplit(url).hostname == "www.exist.ru" for url in calls)
    assert catalog._exist_absolute_url(target) is None
    assert catalog._exist_absolute_url("/Price/?pid=1") == "https://www.exist.ru/Price/?pid=1"
    with pytest.raises(ValueError, match="unsafe_exist_origin"):
        catalog._exist_read_text(target, office_id=905, timeout=1)


@pytest.mark.parametrize("entrypoint", ["search_web_multi", "research_part_public_evidence"])
@pytest.mark.parametrize("formatting", ["plain", "3-6-8", "4-13", "5-12", "triple-percent"])
def test_gateway_canonicalizes_vin_before_capability_dispatch(entrypoint, formatting):
    vin = "WBAFR123456789012" if formatting == "5-12" else "WDB12345678901234"
    token = {
        "plain": vin,
        "3-6-8": f"{vin[:3]} {vin[3:9]} {vin[9:]}",
        "4-13": f"{vin[:4]} {vin[4:]}",
        "5-12": f"{vin[:5]} {vin[5:]}",
        "triple-percent": quote(quote("".join(f"%{ord(c):02X}" for c in vin), safe=""), safe=""),
    }[formatting]
    calls = []

    def invoke(_capability, arguments):
        calls.append(arguments)
        return {"ok": True, "results": []}

    previous = gateway._DEFAULT_GATEWAY
    gateway.install_web_research_gateway(gateway.CapabilityWebResearchGatewayAdapter(invoke))
    try:
        result = getattr(gateway, entrypoint)(query="масляный фильтр " + token)
    finally:
        gateway.install_web_research_gateway(previous)
    assert len(calls) == 1
    sent = calls[0]["query"]
    for _ in range(8):
        sent = unquote(sent)
    assert vin not in sent.replace(" ", "").replace("-", "")
    assert sent == "масляный фильтр"
    assert result["vin_redacted"] is True


@pytest.mark.parametrize("status", [401, 403, 429, None])
def test_shared_searx_rejection_blocks_both_aliases_across_queries(monkeypatch, status):
    state = {
        "vin": "X" * 17,
        "job_id": "0" * 32,
        "expires_at": time.time() + 60,
        "used_seconds": 0.0,
        "cancel_requested": False,
        "inflight": True,
        "operation_started_at": time.time(),
        "collection_status": "running",
        "stop_reason": "",
        "blocked_engines": [],
        "blocked_hosts": [],
    }
    recipients = []

    class Connection:
        def execute(self, *_args):
            return None

    @contextmanager
    def connect(*_args, **_kwargs):
        yield Connection()

    def write(_conn, current):
        state.clear()
        state.update(copy.deepcopy(current))

    def rejected(_query, _scope, engine):
        recipients.append(engine)
        raise network._http_error(status) if status else network._ProviderError("engine_unavailable")

    monkeypatch.setattr(store, "connect", connect)
    monkeypatch.setattr(store, "metadata", lambda _conn: copy.deepcopy(state))
    monkeypatch.setattr(store, "write_metadata", write)
    monkeypatch.setattr(store, "enabled", lambda: True)
    monkeypatch.setattr(worker, "_discover", lambda *_args: None)
    monkeypatch.setattr(network, "_searxng_search", rejected)
    monkeypatch.setattr(network, "_duckduckgo_search", lambda *_args: [])
    for index in range(2):
        worker._collect_search(
            state["job_id"], {"id": index, "query": state["vin"] + " synthetic"}, copy.deepcopy(state)
        )
    assert recipients == (["bing"] if status else ["bing", "yahoo", "bing", "yahoo"])
    assert state["blocked_engines"] == (["bing", "yahoo"] if status else [])


def test_collector_retains_different_majority_tails_after_common_long_prefix(monkeypatch):
    prefix = " ".join(f"intro{n:04d}" for n in range(2048)) + "\n"
    bodies = {
        "https://example.org/first": prefix + "brake caliper pressure fluid hose seal inspection. " * 400,
        "https://example.net/second": prefix + "injector electric circuit voltage wiring connector diagnosis. " * 350,
    }
    assert max(map(len, bodies.values())) <= j1.MAX_DOCUMENT_CHARS
    assert len({hashlib.sha256(body.encode()).digest() for body in bodies.values()}) == 2
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    j1._init_schema(connection)
    now, job_id = j1._utcnow(), "a" * 32
    connection.execute(
        "INSERT INTO jobs(id,objective,max_pages,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        (job_id, "Synthetic technical corpus", 2, "running", now, now),
    )
    for doc_id, url in zip(("b" * 32, "c" * 32), bodies, strict=True):
        connection.execute(
            "INSERT INTO documents(id,job_id,url,created_at) VALUES(?,?,?,?)", (doc_id, job_id, url, now)
        )
    connection.commit()

    @contextmanager
    def fake_db(**_kwargs):
        yield connection

    monkeypatch.setattr(j1, "_db", fake_db)
    monkeypatch.setattr(j1, "_vin_api", lambda _job: None)
    monkeypatch.setattr(j1, "_cache_size", lambda: 0)
    monkeypatch.setattr(j1, "_STOP", False)
    monkeypatch.setattr(
        j1,
        "fetch_document",
        lambda url, **_kw: {
            "ok": True,
            "url": url,
            "title": "Synthetic technical chapter",
            "kind": "html",
            "text": bodies[url],
        },
    )
    try:
        j1._work_job(job_id)
        assert [r[0] for r in connection.execute("SELECT status FROM documents")] == ["fetched", "fetched"]
        assert j1.research_results(job_id, query="injector")["total"] == 1
        assert "injector" in j1.research_document(job_id, "c" * 32, offset=len(prefix))["text"]
        assert j1.research_status(job_id)["status"] == "completed"
    finally:
        connection.close()
