from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from autostop_manager import cli, diagnostics, instruction_bundle as bundles, telegram_wake as wake
from tests.instruction_bundle_fakes import create_instruction_bundle, seal_instruction_bundle


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(bundles, "_OWNER_UID", os.getuid())
    release = create_instruction_bundle(tmp_path / "releases" / "current")
    stale = create_instruction_bundle(tmp_path / "checkout")
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
        missing_skills=[],
        duplicate_skills=[],
        disabled_skills=[],
        physical_paths=False,
        path_overrides={},
        duplicate_scope=False,
        omit_worker_scope=False,
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
                rows = [
                    {
                        "cwd": root,
                        "skills": [
                            {
                                "name": name,
                                "path": state.path_overrides.get(name)
                                or str(
                                    (Path(root) / f".agents/skills/{name}/SKILL.md").resolve()
                                    if state.physical_paths
                                    else Path(root) / f".agents/skills/{name}/SKILL.md"
                                ),
                                "enabled": name not in state.disabled_skills,
                            }
                            for name in (*bundles.PROJECT_SKILLS, *state.duplicate_skills)
                            if name not in state.missing_skills
                        ]
                        + state.extra_skills,
                    }
                    for root in (str(stale), state.status["instruction_root"])
                    if not state.omit_worker_scope or root != str(stale)
                ]
                if state.duplicate_scope:
                    rows.append(deepcopy(rows[0]))
                return {"data": rows}
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
    for name in ("manage-autostop-client", "manage-owner-telegram"):
        (runtime.stale / f".agents/skills/{name}/SKILL.md").write_text("Old project policy.\n")
    effective = deepcopy(runtime.effective)
    result = diagnostics.audit_instruction_sources()

    assert result["ok"] is False
    assert result["bundle"]["sha256"] == runtime.bundle.sha256
    assert result["raw_project_skill_differences"] == 2
    assert result["raw_project_skill_unverified"] == 0
    assert "instruction_raw_skill_drift" in result["warnings"]
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
    assert result["raw_project_skill_differences"] == result["raw_project_skill_unverified"] == 0


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
    assert result["ok"] is False
    assert result["raw_project_skill_unverified"] == 2
    assert any(skill["path"] is None for skill in result["raw_skill_discovery"])
    assert str(forbidden) not in json.dumps(result)
    assert "private-customer-body" not in json.dumps(result)


@pytest.mark.parametrize("physical_paths", [False, True], ids=["lexical-alias", "canonical-physical"])
def test_canonical_workspace_skill_aliases_are_verified_without_rpc_path_reads(runtime, monkeypatch, physical_paths):
    shutil.rmtree(runtime.stale / ".agents")
    (runtime.stale / ".agents").symlink_to(runtime.release / ".agents", target_is_directory=True)
    runtime.physical_paths = physical_paths
    read = diagnostics.read_instruction_bytes
    allowed = {runtime.release / f".agents/skills/{name}/SKILL.md" for name in bundles.PROJECT_SKILLS}

    def guarded(path):
        assert path in allowed
        return read(path)

    monkeypatch.setattr(diagnostics, "read_instruction_bytes", guarded)
    result = diagnostics.audit_instruction_sources()

    assert result["ok"] is True
    assert result["raw_project_skill_differences"] == result["raw_project_skill_unverified"] == 0
    assert all(item["matches_selected_bundle"] is True for item in result["raw_skill_discovery"])
    assert result["instruction_model_context_verified"] is False


@pytest.mark.parametrize("change", [None, "revision", "skill_bytes"])
def test_stable_workspace_alias_verifies_only_matching_installed_skill_bytes(runtime, tmp_path, monkeypatch, change):
    releases = tmp_path / "manager-releases"
    alternate = create_instruction_bundle(releases / "installed", revision=("b" if change == "revision" else "a") * 40)
    if change == "skill_bytes":
        (alternate / ".agents/skills/manage-autostop-client/SKILL.md").write_text("Changed project policy.\n")
        seal_instruction_bundle(alternate)
    (releases / "current").symlink_to(alternate, target_is_directory=True)
    shutil.rmtree(runtime.stale / ".agents")
    (runtime.stale / ".agents").symlink_to(releases / "current/.agents", target_is_directory=True)
    runtime.physical_paths = True
    monkeypatch.setattr(diagnostics, "_INSTALLED_MANAGER_RELEASES", releases)
    monkeypatch.setattr(diagnostics, "_INSTALLED_MANAGER_WORKSPACE", runtime.stale)

    result = diagnostics.audit_instruction_sources()

    assert result["ok"] is (change is None)
    assert result["raw_project_skill_unverified"] == (len(bundles.PROJECT_SKILLS) if change == "revision" else 0)
    assert result["raw_project_skill_differences"] == (1 if change == "skill_bytes" else 0)


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_workspace_skill_alias_cannot_escape_to_another_target(runtime, tmp_path, monkeypatch, kind):
    directory = runtime.stale / ".agents/skills/manage-autostop-client"
    foreign = tmp_path / "private-source" / "SKILL.md"
    foreign.parent.mkdir()
    foreign.write_text("private-foreign-policy")
    if kind == "file":
        (directory / "SKILL.md").unlink()
        (directory / "SKILL.md").symlink_to(foreign)
    else:
        shutil.rmtree(directory)
        directory.symlink_to(foreign.parent, target_is_directory=True)
    read = diagnostics.read_instruction_bytes

    def guarded(path):
        assert path.resolve() != foreign
        return read(path)

    monkeypatch.setattr(diagnostics, "read_instruction_bytes", guarded)
    result = diagnostics.audit_instruction_sources()

    assert result["ok"] is False and result["raw_project_skill_unverified"] == 1
    assert "instruction_raw_skills_unverified" in result["warnings"]
    assert "private-foreign-policy" not in json.dumps(result)
    assert str(foreign) not in json.dumps(result)


def test_unique_arbitrary_rpc_skill_path_is_never_opened(runtime, tmp_path, monkeypatch):
    foreign = tmp_path / "private-source"
    foreign.write_text("private-policy-bytes")
    runtime.path_overrides["manage-autostop-client"] = str(foreign)
    read = diagnostics.read_instruction_bytes

    def guarded(path):
        assert path != foreign
        return read(path)

    monkeypatch.setattr(diagnostics, "read_instruction_bytes", guarded)
    result = diagnostics.audit_instruction_sources()

    assert result["ok"] is False and result["raw_project_skill_unverified"] == 2
    assert str(foreign) not in json.dumps(result)
    assert "private-policy-bytes" not in json.dumps(result)


def test_missing_worker_skill_file_is_unverified(runtime):
    (runtime.stale / ".agents/skills/manage-autostop-client/SKILL.md").unlink()
    result = diagnostics.audit_instruction_sources()
    assert result["ok"] is False and result["raw_project_skill_unverified"] == 1


@pytest.mark.parametrize("problem", ["missing", "duplicate"])
def test_missing_or_duplicate_skill_identity_cannot_make_sources_ready(runtime, problem):
    getattr(runtime, "missing_skills" if problem == "missing" else "duplicate_skills").append("manage-autostop-client")
    result = diagnostics.audit_instruction_sources()
    assert result["ok"] is False and result["raw_project_skill_unverified"] == 2
    ambiguous = [item for item in result["raw_skill_discovery"] if item["name"] == "manage-autostop-client"]
    assert all(item["discovery_state"] == problem for item in ambiguous)
    assert all(item["sha256"] is None and item["path"] is None for item in ambiguous)


@pytest.mark.parametrize("problem", ["missing", "duplicate"])
def test_missing_or_duplicate_scope_is_reported_as_unverified(runtime, problem):
    runtime.omit_worker_scope = problem == "missing"
    runtime.duplicate_scope = problem == "duplicate"
    result = diagnostics.audit_instruction_sources()
    assert result["ok"] is False and result["raw_project_skill_unverified"] == len(bundles.PROJECT_SKILLS)


def test_disabled_skill_does_not_change_its_verified_bytes_or_user_selection(runtime):
    runtime.disabled_skills.append("manage-autostop-client")
    result = diagnostics.audit_instruction_sources()
    assert result["ok"] is True
    disabled = [item for item in result["raw_skill_discovery"] if item["name"] == "manage-autostop-client"]
    assert all(item["enabled_in_raw_discovery"] is False for item in disabled)
    assert all(item["matches_selected_bundle"] is True for item in disabled)


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
