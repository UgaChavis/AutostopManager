from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from autostop_manager import cli, diagnostics, instruction_bundle as bundles, telegram_wake as wake
from tests.instruction_bundle_fakes import create_instruction_bundle


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(bundles, "_OWNER_UID", os.getuid())
    release = create_instruction_bundle(tmp_path / "releases" / "current")
    stale = create_instruction_bundle(tmp_path / "checkout")
    for name in ("manage-autostop-client", "manage-owner-telegram"):
        (stale / f".agents/skills/{name}/SKILL.md").write_text("Old project policy.\n")
    bundle = bundles.load_instruction_bundle(release, "a" * 40)
    config = SimpleNamespace(
        thread_id="private-thread-id",
        project_dir=str(stale),
        instruction_root=str(release),
        instruction_revision="a" * 40,
    )
    state = SimpleNamespace(
        release=release,
        stale=stale,
        bundle=bundle,
        config=config,
        calls=[],
        status={
            "ok": True,
            "instruction_root": str(release),
            "instruction_revision": "a" * 40,
            "instruction_bundle_sha256": bundle.sha256,
            "instruction_protocol_sha256": "c" * 64,
            "instruction_delivery_state": "acknowledged",
            "instruction_model_context_verified": True,
            "private": "private-status-text",
        },
        effective={"skills": {"config": [{"name": "private-off", "enabled": False}]}, "secret": "private-key"},
        extra_skills=[],
        turns=[],
        fail_method=None,
        empty_discovery=False,
    )

    class App:
        def __init__(self, selected):
            assert selected is config

        async def connect(self):
            state.calls.append(("connect", {}))

        async def request(self, method, params):
            state.calls.append((method, params))
            if method == state.fail_method:
                raise RuntimeError("private-exception-and-customer-text")
            if method == "thread/read":
                assert params == {"threadId": config.thread_id, "includeTurns": False}
                return {"thread": {"id": config.thread_id, "cwd": str(stale), "turns": state.turns}}
            if method == "skills/list":
                assert params == {"cwds": [str(stale), state.status["instruction_root"]], "forceReload": False}
                if state.empty_discovery:
                    return {"data": []}
                return {
                    "data": [
                        {
                            "cwd": root,
                            "skills": [
                                {
                                    "name": name,
                                    "path": str(Path(root) / f".agents/skills/{name}/SKILL.md"),
                                    "enabled": True,
                                }
                                for name in bundles.PROJECT_SKILLS
                            ]
                            + state.extra_skills,
                        }
                        for root in (str(stale), state.status["instruction_root"])
                    ]
                }
            assert method == "config/read"
            assert params == {"cwd": str(stale), "includeLayers": False}
            return {"config": state.effective}

        async def close(self):
            state.calls.append(("close", {}))

    async def status(request):
        assert request == {"operation": "status"}
        state.calls.append(("wake/status", request))
        return state.status

    monkeypatch.setattr(wake.WakeConfig, "load", classmethod(lambda cls: config))
    monkeypatch.setattr(wake, "AppServer", App)
    monkeypatch.setattr(wake, "local_request", status)
    monkeypatch.setattr(wake, "INSTRUCTION_RELEASE_ROOTS", (release.parent,))
    return state


def test_source_check_reads_only_metadata_and_preserves_raw_discovery_limits(runtime):
    effective = deepcopy(runtime.effective)
    result = diagnostics.audit_instruction_sources()

    assert result["ok"] is True
    assert result["bundle"]["sha256"] == runtime.bundle.sha256
    assert result["raw_project_skill_differences"] == 2
    assert result["prepared_profile"] == {
        "project_doc_max_bytes": 0,
        "automatic_skill_catalog_included": False,
        "skills_config_overridden": False,
        "applied_to_thread_verified": False,
    }
    assert result["metadata"] == {"scope": "configured_bootstrap", "cwd_matches_config": True, "turns_read": 0}
    assert result["instruction_model_context_verified"] is False
    assert result["delivery"]["model_context_verified"] is False
    assert runtime.effective == effective
    assert [method for method, _ in runtime.calls] == [
        "wake/status",
        "connect",
        "thread/read",
        "skills/list",
        "config/read",
        "close",
    ]
    rendered = json.dumps(result)
    assert all(
        token not in rendered for token in ("private-thread-id", "private-status-text", "private-key", "private-off")
    )


def test_idle_unprepared_bundle_is_explicitly_unverified_without_false_failure(runtime):
    runtime.status.update(instruction_bundle_sha256=None, instruction_delivery_state="not_prepared")
    result = diagnostics.audit_instruction_sources()
    assert result["ok"] is True
    assert result["delivery"]["bundle_hash_matches"] is None
    assert result["delivery"]["state"] == "not_prepared"
    assert result["instruction_model_context_verified"] is False


def test_declared_hash_mismatch_is_not_accepted(runtime):
    runtime.status["instruction_bundle_sha256"] = "b" * 64
    result = diagnostics.audit_instruction_sources()
    assert result["ok"] is False
    assert "instruction_declared_bundle_mismatch" in result["warnings"]


@pytest.mark.parametrize("delivery", ["rejected", "unknown"])
def test_unsettled_delivery_is_not_accepted(runtime, delivery):
    runtime.status["instruction_delivery_state"] = delivery
    result = diagnostics.audit_instruction_sources()
    assert result["ok"] is False
    assert "instruction_delivery_unverified" in result["warnings"]


def test_status_selects_existing_job_bundle_instead_of_new_config_bundle(runtime):
    previous = create_instruction_bundle(runtime.release.parent / "previous", revision="b" * 40)
    runtime.status.update(instruction_root=str(previous), instruction_revision="b" * 40)
    result = diagnostics.audit_instruction_sources()
    assert result["ok"] is True
    assert result["bundle"]["root"] == str(previous)
    assert result["bundle"]["revision"] == "b" * 40
    assert result["bundle"]["source"] == "worker_status"


def test_corrupted_bundle_stops_before_any_codex_connection(runtime):
    (runtime.release / "docs/agent/modules/F4.md").write_text("Changed policy with old manifest.\n")
    result = diagnostics.audit_instruction_sources()
    assert result["ok"] is False
    assert result["warnings"] == ["instruction_bundle_mismatch"]
    assert [method for method, _ in runtime.calls] == ["wake/status"]


def test_discovery_never_opens_an_untrusted_rpc_path(runtime, tmp_path, monkeypatch):
    forbidden = tmp_path / "private-data"
    forbidden.write_text("private-customer-body")
    runtime.extra_skills = [{"name": "manage-autostop-client", "path": str(forbidden), "enabled": True}]
    read = diagnostics.read_instruction_bytes

    def guarded(path):
        assert path != forbidden
        return read(path)

    monkeypatch.setattr(diagnostics, "read_instruction_bytes", guarded)
    result = diagnostics.audit_instruction_sources()
    assert result["ok"] is True
    assert any(skill["path"] is None for skill in result["raw_skill_discovery"])
    assert str(forbidden) not in json.dumps(result)
    assert "private-customer-body" not in json.dumps(result)


@pytest.mark.parametrize("failure", ["thread/read", "skills/list", "config/read"])
def test_runtime_failures_are_sanitized_and_connection_is_closed(runtime, failure):
    runtime.fail_method = failure
    result = diagnostics.audit_instruction_sources()
    assert result["ok"] is False
    assert result["warnings"] == ["instruction_sources_unavailable"]
    assert runtime.calls[-1][0] == "close"
    assert "private-exception-and-customer-text" not in json.dumps(result)


def test_unexpected_history_is_not_exposed_or_used(runtime):
    runtime.turns = [{"private": "private-customer-body"}]
    result = diagnostics.audit_instruction_sources()
    assert result["ok"] is False
    assert "private-customer-body" not in json.dumps(result)
    assert "skills/list" not in [method for method, _ in runtime.calls]


def test_empty_discovery_is_not_accepted(runtime):
    runtime.empty_discovery = True
    assert diagnostics.audit_instruction_sources()["ok"] is False


def test_doctor_host_sources_are_explicit_and_project_only_is_portable(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "diagnose", lambda **options: calls.append(options) or {"ok": True})
    assert cli.main(["doctor", "--project-only"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert calls == [{"integrations": False, "full": False, "check_external_links": False}]
    assert cli.main(["doctor", "--instruction-sources"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert calls[-1] == {"integrations": False, "full": False, "instruction_sources": True}


def test_project_only_and_host_sources_are_rejected_before_diagnostics(monkeypatch):
    def forbidden(**options):
        raise AssertionError("host diagnostic must not run")

    monkeypatch.setattr(cli, "diagnose", forbidden)
    monkeypatch.setattr(diagnostics, "audit_instruction_sources", forbidden)
    with pytest.raises(SystemExit) as failure:
        cli.main(["doctor", "--project-only", "--instruction-sources"])
    assert failure.value.code == 2
    result = diagnostics.diagnose(check_external_links=False, instruction_sources=True)
    assert result["ok"] is False
    assert result["checks"]["scope"]["warnings"] == ["instruction_sources_requires_host_scope"]
