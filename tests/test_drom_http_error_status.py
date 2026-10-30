"""Known Webbee HTTP errors survive diagnostic bodies and preserve write uncertainty."""

import asyncio
from io import BytesIO
import json
import socket
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import drom_listings as drom, mcp_drom_executor, mcp_telemetry, mcp_tools
from autostop_manager.listing_executor import BoundedListingExecutor


class ErrorResponse(HTTPError):
    def __init__(self, status, *, read_error=ConnectionResetError, close_error=None):
        super().__init__("https://synthetic.invalid/", status, "private diagnostic", {}, BytesIO())
        self.read_calls = self.close_calls = 0
        self.read_error = read_error
        self.close_error = close_error

    def read(self, *_args):
        self.read_calls += 1
        if self.read_error:
            raise self.read_error("private body failure")
        return b"private diagnostic"

    def close(self):
        self.close_calls += 1
        super().close()
        if self.close_error:
            raise self.close_error("private cleanup failure")


class Response(BytesIO):
    def __init__(self, payload, *, status=200, broken=False):
        super().__init__(json.dumps(payload).encode())
        self.status, self.broken = status, broken

    def read(self, *args):
        if self.broken:
            raise ConnectionResetError("private successful response failure")
        return super().read(*args)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("synthetic Drom review must not acquire real data")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(drom, "load_runtime_env", denied)
    monkeypatch.setattr(drom, "drom_listings_enabled", lambda: True)
    monkeypatch.setattr(drom, "_load_api_config", lambda: ("synthetic-token", "synthetic-robot"))
    monkeypatch.setattr(drom, "uuid4", lambda: SimpleNamespace(hex="synthetic-review"))


@pytest.fixture(scope="module")
def native_tools():
    class Inert:
        def __init__(self, *_args, **_kwargs):
            pass

    loop = asyncio.new_event_loop()
    pools = [BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=3) for _ in range(2)]
    try:
        with pytest.MonkeyPatch.context() as patch:
            for name in (
                "StoreIntegration",
                "StoreApiClient",
                "StoreOwnerApiClient",
                "StoreQuoteConductor",
                "StoreQuoteOwnerApi",
            ):
                patch.setattr(mcp_tools, name, Inert)
            for name in (
                "get_store_api_url",
                "get_store_read_token",
                "get_store_manage_token",
                "get_store_quote_token",
                "get_store_owner_token",
            ):
                patch.setattr(mcp_tools, name, lambda: "")
            patch.setattr(mcp_telemetry.LOGGER, "disabled", True)
            patch.setattr(mcp_drom_executor, "_START_EXECUTOR", pools[0])
            patch.setattr(mcp_drom_executor, "_GET_EXECUTOR", pools[1])
            server = FastMCP("synthetic-drom-http-errors")
            mcp_tools.register_manager_tools(
                server,
                store=Inert(),
                store_client=Inert(),
                include_tools={"drom_start_parts_search", "drom_get_parts_search"},
            )
            yield server, loop
    finally:
        for pool in pools:
            pool.close()
        loop.close()


@pytest.fixture(params=["direct", "native"])
def call(request, native_tools, monkeypatch):
    def invoke(name, responses):
        sent = []

        class Opener:
            def open(self, http_request, *, timeout):
                assert timeout == drom.REQUEST_TIMEOUT_SECONDS
                sent.append(http_request)
                assert len(sent) <= len(responses), "unexpected extra provider request"
                response = responses[len(sent) - 1]
                if isinstance(response, Exception):
                    raise response
                return response

        monkeypatch.setattr(drom, "build_opener", lambda *_args: Opener())
        arguments = (
            {"query": "DEMO123"} if name == "drom_start_parts_search" else {"task_id": 123, "uid": "synthetic-run"}
        )
        if request.param == "direct":
            result = getattr(drom, name)(**arguments)
        else:
            server, loop = native_tools
            content, result = loop.run_until_complete(
                server._tool_manager.get_tool(name).run(arguments, convert_result=True)
            )
            assert json.loads(content[0].text) == result
        assert "private" not in repr(result)
        assert "synthetic-token" not in repr(result)
        for response in responses:
            if isinstance(response, ErrorResponse):
                assert response.read_calls == 0
                assert response.close_calls == 1
        return result, sent

    return invoke


@pytest.mark.parametrize("stage", ["status", "result"])
@pytest.mark.parametrize(
    "status,code,uncertain",
    [
        (302, "unavailable", False),
        (401, "webbee_auth_failed", False),
        (403, "webbee_access_denied", False),
        (404, "not_found", False),
        (423, "webbee_robot_blocked", False),
        (429, "webbee_quota_or_rate_limited", False),
        (503, "unavailable", True),
    ],
)
@pytest.mark.parametrize("close_error", [None, OSError, RuntimeError])
def test_get_preserves_known_status_without_reading_error_body(call, stage, status, code, uncertain, close_error):
    error = ErrorResponse(status, close_error=close_error)
    responses = [error] if stage == "status" else [Response({"completedAt": "2026-10-30"}), error]
    result, sent = call("drom_get_parts_search", responses)
    if code == "unavailable":
        code = f"webbee_{stage}_unavailable"
    elif code == "not_found":
        code = "webbee_task_not_found" if stage == "status" else "webbee_result_not_found"
    assert result["error"] == code
    assert result["outcome_uncertain"] is uncertain
    assert result["status"] == "failed"
    assert result["task_id"] == 123 and result["uid"] == "synthetic-run"
    assert [row.get_method() for row in sent] == ["GET"] * (1 if stage == "status" else 2)
    if stage == "result":
        assert result["provider_status"] == "complete"


@pytest.mark.parametrize(
    "status,code,uncertain",
    [
        (302, "webbee_task_creation_failed", False),
        (401, "webbee_auth_failed", False),
        (403, "webbee_access_denied", False),
        (409, "webbee_task_limit_or_conflict", False),
        (423, "webbee_robot_blocked", False),
        (429, "webbee_quota_or_rate_limited", True),
    ],
)
def test_create_rejection_retains_distinct_auth_and_429_uncertainty(call, status, code, uncertain):
    result, sent = call("drom_start_parts_search", [ErrorResponse(status, close_error=OSError)])
    assert result["error"] == code
    assert result["outcome_uncertain"] is uncertain
    assert result["status"] == ("uncertain" if uncertain else "failed")
    assert [row.get_method() for row in sent] == ["POST"]
    assert result["task_id"] is None and result["uid"] == "autostop-synthetic-review"


@pytest.mark.parametrize("status", [408, 503])
def test_uncertain_create_error_still_reconciles_once_without_repeating_post(call, status):
    result, sent = call(
        "drom_start_parts_search", [ErrorResponse(status, close_error=RuntimeError), Response({"items": []})]
    )
    assert result["error"] == "webbee_task_creation_uncertain"
    assert result["status"] == "uncertain" and result["outcome_uncertain"] is True
    assert [row.get_method() for row in sent] == ["POST", "GET"]
    assert len({urlsplit(row.full_url).path for row in sent}) == 1


@pytest.mark.parametrize(
    "status,code,uncertain",
    [
        (401, "webbee_start_outcome_uncertain", True),
        (429, "webbee_start_outcome_uncertain", True),
        (503, "webbee_start_outcome_uncertain", True),
        (423, "webbee_robot_blocked", False),
        (404, "webbee_task_not_found", False),
    ],
)
def test_start_patch_keeps_existing_uid_reconciliation_and_outcome(call, status, code, uncertain):
    result, sent = call(
        "drom_start_parts_search",
        [Response({"id": 123}, status=201), ErrorResponse(status, close_error=OSError), Response({})],
    )
    assert result["error"] == code and result["outcome_uncertain"] is uncertain
    assert [row.get_method() for row in sent] == ["POST", "PATCH", "GET"]
    assert (
        parse_qs(urlsplit(sent[1].full_url).query)
        == parse_qs(urlsplit(sent[2].full_url).query)
        == {"uid": [result["uid"]]}
    )
    assert result["task_id"] == 123


def test_successful_create_with_lost_body_still_has_unknown_outcome(call):
    result, sent = call("drom_start_parts_search", [Response({}, status=201, broken=True), Response({"items": []})])
    assert result["error"] == "webbee_task_creation_uncertain"
    assert result["status"] == "uncertain" and result["outcome_uncertain"] is True
    assert [row.get_method() for row in sent] == ["POST", "GET"]


def test_successful_get_response_still_parses_its_payload(call):
    result, sent = call("drom_get_parts_search", [Response({"completedAt": "2026-10-30"}), Response([])])
    assert result["ok"] is True and result["status"] == "complete"
    assert result["listings"] == [] and len(sent) == 2


@pytest.mark.parametrize("close_error", [OSError, RuntimeError])
def test_close_failure_alone_does_not_replace_known_auth_rejection(call, close_error):
    result, sent = call("drom_start_parts_search", [ErrorResponse(401, read_error=None, close_error=close_error)])
    assert result["error"] == "webbee_auth_failed" and result["outcome_uncertain"] is False
    assert [row.get_method() for row in sent] == ["POST"]


@pytest.mark.parametrize("broken", [False, True])
def test_successful_get_unreadable_or_oversized_body_remains_uncertain(call, monkeypatch, broken):
    monkeypatch.setattr(drom, "MAX_RESPONSE_BYTES", 64)
    result, sent = call("drom_get_parts_search", [Response("x" * 65, broken=broken)])
    assert result["error"] == "webbee_status_unavailable" and result["outcome_uncertain"] is True
    assert [row.get_method() for row in sent] == ["GET"]
