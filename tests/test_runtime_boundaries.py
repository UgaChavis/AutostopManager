"""Synthetic failure checks for the retained CLI, voice and MCP boundaries."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from autostop_manager import cli, mcp_probe as probe, telegram_monitor_voice as voice
from autostop_manager.mcp_contract import validate_manager_mcp_surface


@pytest.mark.parametrize(
    "stdout,code,error",
    [
        ("not-json", 0, "monitor_voice_transcription_invalid"),
        ("[]", 0, "monitor_voice_transcription_invalid"),
        ("x" * 65537, 0, "monitor_voice_transcription_invalid"),
        (json.dumps({"ok": False, "error": "PRIVATE message"}), 1, "monitor_voice_transcription_failed"),
        (json.dumps({"ok": True, "text": "", "language": "ru"}), 0, "monitor_voice_transcription_invalid"),
        (
            json.dumps({"ok": True, "text": "synthetic", "language": "invalid-language"}),
            0,
            "monitor_voice_transcription_invalid",
        ),
        (json.dumps({"ok": True, "text": "synthetic", "language": "ru"}), 0, "monitor_voice_cleanup_failed"),
    ],
)
def test_voice_runner_rejects_invalid_or_unclean_output(monkeypatch, stdout, code, error):
    monkeypatch.setattr(voice, "_media_runner_path", lambda: Path("/synthetic/runner"))
    monkeypatch.setattr(voice.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=stdout, returncode=code))
    with pytest.raises(voice.MonitorVoiceError, match=error):
        voice._run_transcriber(Path("synthetic.ogg"), language="ru")


def test_voice_runner_requires_cleanup_and_passes_bounded_command(monkeypatch):
    captured = []
    monkeypatch.setattr(voice, "_media_runner_path", lambda: Path("/synthetic/runner"))

    def run(args, **kwargs):
        captured.append((args, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"ok": True, "text": "synthetic", "language": "ru", "cleanup_verified": True}),
        )

    monkeypatch.setattr(voice.subprocess, "run", run)
    assert voice._run_transcriber(Path("synthetic.ogg"), language="ru")["cleanup_verified"]
    assert "--delete-after" in captured[0][0]
    assert captured[0][1]["timeout"] == 180


@pytest.mark.parametrize("failure", [OSError("offline"), voice.subprocess.TimeoutExpired("synthetic", 1)])
def test_voice_runner_unavailable_is_safe(monkeypatch, failure):
    monkeypatch.setattr(voice, "_media_runner_path", lambda: Path("/synthetic/runner"))

    def run(*a, **kw):
        raise failure

    monkeypatch.setattr(voice.subprocess, "run", run)
    with pytest.raises(voice.MonitorVoiceError, match="monitor_voice_transcription_unavailable"):
        voice._run_transcriber(Path("synthetic.ogg"), language="ru")


@pytest.mark.parametrize("payload", [{"ok": False, "error": "PRIVATE text"}, None])
def test_voice_bridge_failure_does_not_expose_raw_details(monkeypatch, payload):
    def request(*a, **kw):
        if payload is None:
            raise voice.BridgeError("bridge_unavailable")
        return payload

    monkeypatch.setattr(voice, "send_local_request", request)
    with pytest.raises(voice.MonitorVoiceError) as error:
        voice._request({"operation": "monitor_voice_stage"})
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize(
    "identifier,language,error",
    [("invalid", "ru", "inbound_event_invalid"), ("inbound-1", "../", "monitor_voice_language_invalid")],
)
def test_voice_validates_event_and_language_before_read(monkeypatch, identifier, language, error):
    monkeypatch.setattr(voice.os, "geteuid", lambda: 0)
    monkeypatch.setattr(voice, "_request", lambda *a: pytest.fail("must not read"))
    with pytest.raises(voice.MonitorVoiceError, match=error):
        voice.transcribe_monitored_voice(identifier, language=language)


def test_voice_runner_path_and_staged_path_checks(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    runner = root / "scripts/run-work-telegram-media.sh"
    runner.parent.mkdir(parents=True)
    monkeypatch.setattr(voice, "__file__", str(root / "autostop_manager/telegram_monitor_voice.py"))
    with pytest.raises(voice.MonitorVoiceError, match="runner_unavailable"):
        voice._media_runner_path()
    runner.write_text("synthetic")
    runner.chmod(0o755)
    assert voice._media_runner_path() == runner
    monkeypatch.setattr(voice, "account_inbox_dir", lambda *a: tmp_path)
    assert voice._staged_voice_path("a" * 32, ".ogg").parent == tmp_path
    with pytest.raises(voice.MonitorVoiceError, match="stage_invalid"):
        voice._staged_voice_path("../", ".ogg")


def test_cli_dispatches_store_operations_and_probe_without_live_effects(monkeypatch, capsys):
    seen = []

    class Store:
        def store_quote_conductor_release_readiness(self):
            seen.append("gate")
            return {"ok": True}

        def get_store_checkpoint(self, stream):
            seen.append(stream)
            return {"ok": True}

        def reset_store_checkpoint_for_rebaseline(self, **kwargs):
            seen.append(kwargs)
            return {"ok": True}

    monkeypatch.setattr(cli, "StoreState", Store)
    monkeypatch.setattr(cli, "probe_manager_mcp", lambda *a, **kw: {"ok": True})
    for args in (
        ["store-conductor-release-gate"],
        ["store-checkpoint-status", "--stream", "store_digest"],
        [
            "store-checkpoint-reset",
            "--stream",
            "store_digest",
            "--expected-state-version",
            "1",
            "--reason",
            "operator_verified_rebaseline",
            "--confirm-rebaseline",
        ],
        ["mcp-probe"],
    ):
        assert cli.main(args) == 0
        assert json.loads(capsys.readouterr().out)["ok"]
    assert seen[:2] == ["gate", "store_digest"]
    assert seen[2]["expected_state_version"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"format": "wrong"},
        {"format": "mcp_surface_manifest_v1", "expected_tool_count": 1, "expected_tool_names": []},
    ],
)
def test_mcp_rejects_malformed_manifest(tmp_path, payload):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload))
    assert not validate_manager_mcp_surface({}, manifest_path=path)["ok"]


def test_probe_unwraps_only_object_payloads_and_handles_pagination():
    assert probe._payload_from_tool_result(
        SimpleNamespace(
            content=[SimpleNamespace(text="bad"), SimpleNamespace(text="[]"), SimpleNamespace(text='{"ok": true}')]
        )
    ) == {"ok": True}
    assert probe._payload_from_tool_result(SimpleNamespace(content=[SimpleNamespace(text=None)])) is None
    assert probe._safe_url("http://user:secret@127.0.0.1/path?secret=hidden") == "http://127.0.0.1/path"
    assert probe._safe_url("http://[") == "invalid_url"

    class Session:
        async def list_tools(self, cursor=None):
            return SimpleNamespace(tools=[cursor], nextCursor="second" if cursor is None else None)

    assert asyncio.run(probe._list_all_tools(Session())) == [None, "second"]
    assert not probe.probe_manager_mcp(timeout=0)["ok"]


@pytest.mark.parametrize(
    "status,expected",
    [(401, "transport_auth_failure"), (403, "transport_auth_failure"), (404, "transport_route_unavailable")],
)
def test_probe_classifies_http_failure_without_payload(status, expected):
    error = httpx.HTTPStatusError(
        "PRIVATE", request=httpx.Request("GET", "http://example.test"), response=httpx.Response(status)
    )
    assert probe.classify_transport_exception(error) == expected


@pytest.mark.parametrize(
    "failure",
    [
        "registry",
        "duplicate",
        "status",
        "category",
        "resolver",
        "resolver_private",
        "provider",
        "provider_private",
        "transport",
        "none",
    ],
)
def test_probe_failure_stages_with_synthetic_transport(monkeypatch, failure):
    @asynccontextmanager
    async def transport(*a, **kw):
        if failure == "transport":
            raise httpx.ConnectError("PRIVATE connection detail")
        yield None, None, None

    class Session:
        def __init__(self, *a):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def initialize(self):
            return SimpleNamespace(serverInfo=SimpleNamespace(name="synthetic"))

        async def send_ping(self):
            pass

        async def list_tools(self, **kw):
            tool = SimpleNamespace(name="synthetic", inputSchema={})
            return SimpleNamespace(tools=[tool, tool] if failure == "duplicate" else [tool], nextCursor=None)

    async def call(session, name, arguments, **kw):
        if name == "catalog_provider_status":
            return False, {"ok": failure != "status"}
        if name == "search_partsapi_category_index":
            return False, {"ok": failure != "category"}
        if name == "resolve_vin_oem_parts":
            if failure == "resolver":
                return False, {"status": "broken", "readiness": {"needs_partsapi_category_mapping": True}}
            return False, {
                "status": "needs_identity_confirmation",
                "readiness": {"needs_partsapi_category_mapping": False},
                "calls": [{"operation": "parts_by_vin", "dry_run": True}],
                "oem_candidates": [],
                "live_call_count": 0,
                "extra": probe.SYNTHETIC_IDENTIFIER if failure == "resolver_private" else "",
            }
        return False, {
            "ok": False,
            "failure_class": "timeout",
            "requires_fallback": failure != "provider",
            "extra": probe.SYNTHETIC_IDENTIFIER if failure == "provider_private" else "",
        }

    monkeypatch.setattr(probe, "streamable_http_client", transport)
    monkeypatch.setattr(probe, "ClientSession", Session)
    monkeypatch.setattr(probe, "validate_manager_mcp_surface", lambda _: {"ok": failure != "registry"})
    monkeypatch.setattr(probe, "_call", call)
    result = probe.probe_manager_mcp(provider_failure_check=True)
    assert result["ok"] is (failure == "none")
    assert probe.SYNTHETIC_IDENTIFIER not in json.dumps(result)
    assert "PRIVATE" not in json.dumps(result)
