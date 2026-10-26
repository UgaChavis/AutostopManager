"""Thread-owned artifact roots are contextual; execution-root drift still blocks work."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace

import pytest
from telegram_wake_fakes import THREAD, MockServer, connected_app

from autostop_manager import telegram_wake as wake

OTHER_THREAD = "00000000-0000-4000-8000-000000000099"


@pytest.fixture
def artifact_base(tmp_path, monkeypatch):
    base = tmp_path / "visualizations"
    base.mkdir(mode=0o700)
    monkeypatch.setattr(wake, "VISUALIZATION_ROOT", base)
    return base


def artifact(base, thread=THREAD, date=("2026", "10", "26")):
    path = base.joinpath(*date, thread)
    path.mkdir(parents=True, mode=0o700)
    return path


def normalized(roots, thread=THREAD, session=THREAD):
    return wake._execution_workspace_roots(roots, thread_id=thread, session_id=session)


def test_only_this_sessions_owned_artifact_root_is_contextual(artifact_base):
    own = artifact(artifact_base)
    unrelated = artifact(artifact_base, OTHER_THREAD)
    roots = [wake.PROJECT_DIR, str(own), str(unrelated), "/another/workspace"]
    assert normalized(roots) == [wake.PROJECT_DIR, str(unrelated), "/another/workspace"]
    assert normalized([wake.PROJECT_DIR, str(own)], session=OTHER_THREAD) == [wake.PROJECT_DIR, str(own)]
    assert normalized([wake.PROJECT_DIR, str(own)], session=None) == [wake.PROJECT_DIR, str(own)]


@pytest.mark.parametrize("date", [("2026", "13", "01"), ("2026", "2", "01"), ("2026", "02", "30")])
def test_noncanonical_or_invalid_date_is_not_a_trusted_artifact_root(artifact_base, date):
    own = artifact(artifact_base, date=date)
    roots = [wake.PROJECT_DIR, str(own)]
    assert normalized(roots) == roots


@pytest.mark.parametrize("part", ["base", "year", "leaf"])
def test_writable_artifact_ancestor_or_leaf_is_not_excluded(artifact_base, part):
    own = artifact(artifact_base)
    target = {"base": artifact_base, "year": artifact_base / "2026", "leaf": own}[part]
    target.chmod(0o770)
    roots = [wake.PROJECT_DIR, str(own)]
    assert normalized(roots) == roots


def test_different_owner_is_not_excluded(artifact_base, monkeypatch):
    own = artifact(artifact_base)
    uid = os.geteuid()
    monkeypatch.setattr(wake.os, "geteuid", lambda: uid + 1)
    roots = [wake.PROJECT_DIR, str(own)]
    assert normalized(roots) == roots


def test_symlink_or_missing_directory_is_not_excluded(artifact_base, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = artifact_base / "2026" / "10" / "26"
    parent.mkdir(parents=True)
    own = parent / THREAD
    own.symlink_to(outside, target_is_directory=True)
    roots = [wake.PROJECT_DIR, str(own)]
    assert normalized(roots) == roots
    own.unlink()
    assert normalized(roots) == roots


def test_multiple_matching_roots_and_missing_schema_are_not_normalized(artifact_base):
    own = artifact(artifact_base)
    roots = [wake.PROJECT_DIR, str(own), str(own)]
    assert normalized(roots) == roots
    assert normalized(None) is None
    assert normalized([]) == []


class ArtifactServer(MockServer):
    def __init__(self, base, *, extra_root=None, wrong_model=False):
        super().__init__(peer_threads=True)
        self.base = base
        self.extra_root = extra_root
        self.wrong_model = wrong_model

    def _thread_response(self, thread_id):
        result = super()._thread_response(thread_id)
        result["thread"]["sessionId"] = thread_id
        own = self.base / "2026" / "10" / "26" / thread_id
        own.mkdir(parents=True, exist_ok=True, mode=0o700)
        result["runtimeWorkspaceRoots"] = [*result["runtimeWorkspaceRoots"], str(own)]
        if thread_id != THREAD:
            if self.extra_root is not None:
                result["runtimeWorkspaceRoots"].append(self.extra_root)
            if self.wrong_model:
                result["model"] = "unexpected-model"
        return result


@pytest.mark.parametrize("drift", [None, "workspace", "model"])
def test_resume_handles_own_artifacts_but_rejects_execution_setting_drift(tmp_path, artifact_base, drift):
    async def scenario():
        server = ArtifactServer(
            artifact_base,
            extra_root="/unrelated/project" if drift == "workspace" else None,
            wrong_model=drift == "model",
        )
        async with connected_app(tmp_path, server) as bootstrap:
            await bootstrap.resume()
            params = bootstrap.peer_start_params()
            assert params["runtimeWorkspaceRoots"] == [wake.PROJECT_DIR]
            created = await bootstrap.request("thread/start", params)
            peer = wake.AppServer(replace(bootstrap.config, thread_id=created["thread"]["id"]))
            try:
                await peer.connect()
                peer.resume_overrides = dict(params)
                peer.resume_overrides.pop("ephemeral")
                peer.expected_resume_settings = dict(bootstrap.resumed_settings)
                if drift:
                    with pytest.raises(wake.WakeError, match=r"^wake_worker_settings_mismatch$"):
                        await peer.resume()
                else:
                    assert await peer.resume() == "idle"
                    assert peer.resumed_settings == bootstrap.resumed_settings
                assert server.counter == 0
            finally:
                await peer.close()

    asyncio.run(scenario())
