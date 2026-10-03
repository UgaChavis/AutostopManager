from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts.disk_maintenance import ci


class FakeCI:
    def __init__(self, path: Path, count: int = 2):
        self.path = path
        self.workflows = {
            number: {
                "id": number,
                "path": f".github/workflows/ci-{number}.yml",
                "state": "active",
                "updated_at": "2026-10-03T00:00:00Z",
            }
            for number in range(1, count + 1)
        }
        self.workflows[9001] = {
            "id": 9001,
            "path": ".github/workflows/disabled.yml",
            "state": "disabled_manually",
            "updated_at": "2026-10-02T00:00:00Z",
        }
        self.runner = {"active_state": "active", "sub_state": "running", "main_pid": 123, "invocation_id": "a" * 32}
        self.calls: list[list[str]] = []
        self.runs: list[dict] = []
        self.fences = (False, False)
        self.fail_disable = None
        self.fail_enable = None
        self.fail_stop = False
        self.fail_start = False
        self.auth_failure = False
        self.stop_applies_before_error = False
        self.disable_applies_before_error = False
        self.clock = 0.0
        self.on_sleep = None
        self.after_disable = None
        self.timestamp = 1
        self.head = "a" * 40
        self.head_reads = 0
        self.fail_head_before = False
        self.fail_head_after = False

    def read(self):
        return json.loads(self.path.read_text())

    def sleep(self, duration):
        self.clock += duration
        if self.on_sleep:
            self.on_sleep()

    def result(self, argv, payload=None, code=0):
        output = json.dumps(payload) if payload is not None else ""
        return subprocess.CompletedProcess(argv, code, output, "synthetic-private-token-never-print")

    def run(self, argv, *, timeout=30, input_file=None):
        assert input_file is None
        assert 0 < timeout <= 45
        self.calls.append(list(argv))
        if argv[0] == "gh":
            return self.gh(argv)
        assert argv[:1] == ["systemctl"]
        assert ci.RUNNER_UNIT in argv
        if argv[1] == "show":
            values = {
                "Id": ci.RUNNER_UNIT,
                "ActiveState": self.runner["active_state"],
                "SubState": self.runner["sub_state"],
                "MainPID": self.runner["main_pid"],
                "InvocationID": self.runner["invocation_id"],
            }
            return subprocess.CompletedProcess(argv, 0, "\n".join(f"{k}={v}" for k, v in values.items()), "")
        if argv[1] == "stop":
            assert self.read()["runner_stop_intent"] is True
            assert not self.runs and not any(self.fences)
            if not self.fail_stop or self.stop_applies_before_error:
                self.runner.update(active_state="inactive", sub_state="dead", main_pid=0)
            return self.result(argv, code=int(self.fail_stop))
        if argv[1] == "start":
            assert self.read()["runner_start_intent"] is True
            if not self.fail_start:
                self.runner.update(active_state="active", sub_state="running", main_pid=456, invocation_id="b" * 32)
            return self.result(argv, code=int(self.fail_start))
        raise AssertionError("unexpected systemctl mutation")

    def gh(self, argv):
        method = argv[argv.index("--method") + 1]
        uri = urlsplit(argv[-1])
        if uri.path.endswith("/commits/main"):
            assert method == "GET"
            self.head_reads += 1
            failed = self.fail_head_before or (self.fail_head_after and self.head_reads > 1)
            return subprocess.CompletedProcess(argv, int(failed), self.head, "synthetic-private-token-never-print")
        endpoint = uri.path.split("/actions/", 1)[1]
        query = parse_qs(uri.query)
        if self.auth_failure:
            return self.result(argv, code=4)
        if endpoint in {"workflows", "runs"}:
            assert method == "GET"
            page = int(query["page"][0])
            if endpoint == "workflows":
                values = list(self.workflows.values())
                key = "workflows"
            else:
                values = [value for value in self.runs if value["status"] == query["status"][0]]
                key = "workflow_runs"
            return self.result(argv, {"total_count": len(values), key: values[(page - 1) * 100 : page * 100]})
        parts = endpoint.split("/")
        assert parts[0] == "workflows"
        workflow = self.workflows[int(parts[1])]
        if len(parts) == 2:
            assert method == "GET"
            return self.result(argv, workflow)
        assert method == "PUT" and parts[2] in {"disable", "enable"}
        saved = next(value for value in self.read()["workflows"] if value["id"] == workflow["id"])
        assert saved["original_state"] == "active" and saved["disable_intent"]
        assert workflow["id"] != 9001
        failed = workflow["id"] == (self.fail_disable if parts[2] == "disable" else self.fail_enable)
        if not failed or (parts[2] == "disable" and self.disable_applies_before_error):
            workflow["state"] = "disabled_manually" if parts[2] == "disable" else "active"
            workflow["updated_at"] = f"2026-10-03T00:{self.timestamp // 60:02d}:{self.timestamp % 60:02d}Z"
            self.timestamp += 1
        if self.after_disable and parts[2] == "disable":
            self.after_disable()
        return self.result(argv, code=4 if failed else 0)

    def mutations(self, suffix):
        return [call for call in self.calls if call[0] == "gh" and call[-1].endswith("/" + suffix)]


@pytest.fixture
def fake(tmp_path, monkeypatch):
    value = FakeCI(tmp_path / "ci-state.json")
    monkeypatch.setattr(ci, "run", value.run)
    monkeypatch.setattr(ci, "_process_fences", lambda: value.fences)
    monkeypatch.setattr(ci.time, "monotonic", lambda: value.clock)
    monkeypatch.setattr(ci.time, "sleep", value.sleep)

    def save(path, state):
        path.write_text(json.dumps(state))
        path.chmod(0o600)

    monkeypatch.setattr(ci, "atomic_json", save)
    monkeypatch.setattr(ci, "read_json", lambda path, private=False: json.loads(path.read_text()))
    return value


def assert_original_workflows(fake):
    assert all(
        value["state"] == ("disabled_manually" if key == 9001 else "active") for key, value in fake.workflows.items()
    )


def test_ci_pause_before_mutation_receipt_and_idempotent_resume(fake):
    state = ci.pause({}, fake.path)
    assert state["phase"] == "paused" and state["runner_stopped"]
    assert fake.runner["active_state"] == "inactive"
    assert len(fake.mutations("disable")) == 2
    assert ci.resume({}, fake.path)["restored"]
    calls = len(fake.calls)
    assert ci.resume({}, fake.path)["restored"]
    assert len(fake.calls) == calls
    assert_original_workflows(fake)
    assert fake.runner["active_state"] == "active"
    assert len(fake.mutations("enable")) == 2


@pytest.mark.parametrize("applied_before_error", [False, True])
def test_ci_partial_disable_failure_restores_every_own_change(fake, applied_before_error):
    fake.fail_disable, fake.disable_applies_before_error = 2, applied_before_error
    with pytest.raises(ci.MaintenanceError, match="ci_api_auth_failed"):
        ci.pause({}, fake.path)
    assert_original_workflows(fake)
    assert fake.read()["phase"] == "restored"
    assert not any(call[:2] == ["systemctl", "stop"] for call in fake.calls)


@pytest.mark.parametrize("status", ci.BUSY_STATUSES)
def test_ci_drain_timeout_restores_and_never_cancels(fake, status):
    fake.runs = [{"id": 42, "status": status}]
    with pytest.raises(ci.MaintenanceError, match="ci_drain_timeout"):
        ci.pause({"ci": {"poll_seconds": 1}}, fake.path, wait_seconds=2)
    assert_original_workflows(fake)
    assert fake.clock == 2
    assert fake.read()["phase"] == "restored"
    assert not any("cancel" in arg or "dispatch" in arg for call in fake.calls for arg in call)
    assert not any(call[:2] == ["systemctl", "stop"] for call in fake.calls)


@pytest.mark.parametrize("fences", [(True, False), (False, True)])
def test_ci_worker_or_remote_deploy_blocks_stop(fake, fences):
    fake.fences = fences
    with pytest.raises(ci.MaintenanceError, match="ci_drain_timeout"):
        ci.pause({}, fake.path, wait_seconds=0)
    assert_original_workflows(fake)
    assert not any(call[:2] == ["systemctl", "stop"] for call in fake.calls)


def test_ci_waits_for_completion_without_forcing_jobs(fake):
    fake.runs = [{"id": 42, "status": "in_progress"}]
    fake.on_sleep = fake.runs.clear
    assert ci.pause({}, fake.path)["phase"] == "paused"
    assert fake.clock == 5
    ci.resume({}, fake.path)
    assert_original_workflows(fake)


@pytest.mark.parametrize("applied_before_error", [False, True])
def test_ci_runner_stop_failure_recovers_request_with_unknown_outcome(fake, applied_before_error):
    fake.fail_stop, fake.stop_applies_before_error = True, applied_before_error
    with pytest.raises(ci.MaintenanceError, match="ci_command_failed"):
        ci.pause({}, fake.path)
    assert_original_workflows(fake)
    assert fake.runner["active_state"] == "active"
    assert fake.read()["phase"] == "restored"


def test_ci_originally_inactive_runner_remains_inactive(fake):
    fake.runner.update(active_state="inactive", sub_state="dead", main_pid=0, invocation_id="")
    assert ci.pause({}, fake.path)["phase"] == "paused"
    ci.resume({}, fake.path)
    assert_original_workflows(fake)
    assert not any(call[:2] in [["systemctl", "stop"], ["systemctl", "start"]] for call in fake.calls)


def test_ci_initial_auth_failure_has_no_mutations_or_private_payload(fake):
    fake.auth_failure = True
    with pytest.raises(ci.MaintenanceError, match="ci_api_auth_failed") as caught:
        ci.pause({}, fake.path)
    assert not fake.path.exists()
    assert "synthetic-private-token" not in str(caught.value)
    assert not fake.mutations("disable")


def test_ci_crash_after_unconfirmed_disable_recovered_from_durable_intent(fake):
    ci.pause({}, fake.path)
    state = fake.read()
    state.update(phase="pausing", runner_stop_intent=False, runner_stopped=False)
    for entry in state["workflows"]:
        entry.update(disabled=False, disabled_updated_at=None)
    fake.runner.update(active_state="active", sub_state="running", main_pid=123, invocation_id="a" * 32)
    fake.path.write_text(json.dumps(state))
    ci.resume({}, fake.path)
    assert_original_workflows(fake)


def test_ci_resume_attempts_remaining_workflows_after_one_failure_then_can_retry(fake):
    ci.pause({}, fake.path)
    fake.fail_enable = 1
    with pytest.raises(ci.MaintenanceError, match="ci_resume_incomplete"):
        ci.resume({}, fake.path)
    assert fake.runner["active_state"] == "active"
    assert fake.workflows[1]["state"] == "disabled_manually"
    assert fake.workflows[2]["state"] == "active"
    assert fake.read()["phase"] == "recovery_required"
    fake.fail_enable = None
    ci.resume({}, fake.path)
    assert_original_workflows(fake)


@pytest.mark.parametrize("change", ["path", "state", "timestamp"])
def test_ci_resume_identity_or_external_lifecycle_change_fails_closed(fake, change):
    ci.pause({}, fake.path)
    if change == "path":
        fake.workflows[1]["path"] = ".github/workflows/replaced.yml"
    elif change == "state":
        fake.workflows[1]["state"] = "disabled_inactivity"
    else:
        fake.workflows[1]["updated_at"] = "2026-10-04T00:00:00Z"
    with pytest.raises(ci.MaintenanceError, match="ci_resume_incomplete"):
        ci.resume({}, fake.path)
    assert not any(call[-1].endswith("/workflows/1/enable") for call in fake.calls)
    assert fake.workflows[2]["state"] == "active"


def test_ci_new_unsaved_workflow_during_disable_aborts_without_touching_it(fake):
    def add_workflow():
        fake.workflows[3] = {
            "id": 3,
            "path": ".github/workflows/new.yml",
            "state": "active",
            "updated_at": "2026-10-03T00:00:00Z",
        }

    fake.after_disable = add_workflow
    with pytest.raises(ci.MaintenanceError, match="ci_catalog_changed"):
        ci.pause({}, fake.path)
    assert_original_workflows(fake)
    assert not any(call[-1].endswith("/workflows/3/disable") for call in fake.calls)


def test_ci_workflow_and_runs_discovery_paginates_without_saved_ids(fake):
    for number in range(3, 103):
        fake.workflows[number] = {
            "id": number,
            "path": f".github/workflows/ci-{number}.yml",
            "state": "disabled_inactivity",
            "updated_at": "2026-10-03T00:00:00Z",
        }
    fake.runs = [{"id": number, "status": "queued"} for number in range(1, 102)]
    assert len(ci._workflows(ci._settings({}))) == 103
    assert ci.active({}) is True
    assert any("workflows?per_page=100&page=2" in call[-1] for call in fake.calls)
    assert any("runs?status=queued&per_page=100&page=2" in call[-1] for call in fake.calls)


def test_ci_invalid_recovery_state_cannot_enable_previously_disabled_workflow(fake):
    ci.pause({}, fake.path)
    state = fake.read()
    entry = next(value for value in state["workflows"] if value["id"] == 9001)
    entry["disable_intent"] = True
    fake.path.write_text(json.dumps(state))
    with pytest.raises(ci.MaintenanceError, match="ci_state_invalid"):
        ci.resume({}, fake.path)
    assert not fake.mutations("enable")


def test_ci_runner_identity_change_before_stop_restores_workflows_without_stop(fake):
    def change_runner():
        fake.runner["invocation_id"] = "c" * 32

    fake.after_disable = change_runner
    with pytest.raises(ci.MaintenanceError, match="ci_runner_changed"):
        ci.pause({}, fake.path)
    assert_original_workflows(fake)
    assert not any(call[:2] == ["systemctl", "stop"] for call in fake.calls)


def test_ci_pause_recovers_a_stale_owned_window_before_starting_a_new_one(fake):
    first = ci.pause({}, fake.path)
    second = ci.pause({}, fake.path)
    assert first["operation_id"] != second["operation_id"]
    assert len(fake.mutations("enable")) == 2
    assert len(fake.mutations("disable")) == 4
    ci.resume({}, fake.path)
    assert_original_workflows(fake)


def test_ci_active_checks_local_fences_even_without_api_auth(fake):
    fake.fences, fake.auth_failure = (True, False), True
    assert ci.active({}) is True
    assert not any(call[0] == "gh" for call in fake.calls)


def test_ci_state_write_failure_prevents_any_disable(fake, monkeypatch):
    def fail_save(_path, _state):
        raise OSError("synthetic-private-token")

    monkeypatch.setattr(ci, "atomic_json", fail_save)
    with pytest.raises(ci.MaintenanceError, match="ci_state_write_failed"):
        ci.pause({}, fake.path)
    assert not fake.mutations("disable")


def test_ci_repo_main_change_during_pause_warns_after_restoration_without_dispatch(fake):
    saved = ci.pause({}, fake.path)
    assert saved["repository_head_before"] == "a" * 40 and saved["admission_gap_possible"]
    fake.head = "b" * 40
    result = ci.resume({}, fake.path)
    assert result["restored"] and result["warnings"] == ["ci_repository_head_changed"]
    assert result["repository_head_after"] == "b" * 40
    assert fake.read()["repository_head_after"] == "b" * 40
    assert_original_workflows(fake)
    assert not any("dispatch" in arg for call in fake.calls for arg in call)


def test_ci_repo_head_metadata_failure_before_pause_has_no_changes(fake):
    fake.fail_head_before = True
    with pytest.raises(ci.MaintenanceError, match="ci_command_failed"):
        ci.pause({}, fake.path)
    assert not fake.path.exists()
    assert not fake.mutations("disable")


def test_ci_repo_head_failure_after_restore_is_warning_not_ci_left_off(fake):
    ci.pause({}, fake.path)
    fake.fail_head_after = True
    result = ci.resume({}, fake.path)
    assert result["restored"] and result["warnings"] == ["ci_repository_head_readback_unavailable"]
    assert result["repository_head_after"] is None
    assert fake.read()["phase"] == "restored"
    assert_original_workflows(fake)
    assert fake.runner["active_state"] == "active"
    assert ci.resume({}, fake.path)["warnings"] == result["warnings"]


def test_ci_late_worker_after_durable_stop_intent_blocks_signal(fake, monkeypatch):
    save = ci.atomic_json

    def late_worker(path, state):
        save(path, state)
        if state["runner_stop_intent"]:
            fake.fences = (True, False)

    monkeypatch.setattr(ci, "atomic_json", late_worker)
    with pytest.raises(ci.MaintenanceError, match="ci_drain_race"):
        ci.pause({}, fake.path)
    assert_original_workflows(fake)
    assert not any(call[:2] == ["systemctl", "stop"] for call in fake.calls)


def test_ci_drain_api_calls_receive_remaining_deadline_budget(fake, monkeypatch):
    original = ci.run

    def timed_api(argv, **kwargs):
        result = original(argv, **kwargs)
        if "runs?status=" in argv[-1]:
            assert kwargs["timeout"] <= 2
            fake.clock += 0.5
        return result

    monkeypatch.setattr(ci, "run", timed_api)
    with pytest.raises(ci.MaintenanceError, match="ci_drain_timeout"):
        ci.pause({}, fake.path, wait_seconds=2)
    assert_original_workflows(fake)
    assert fake.clock <= 2
    assert not any(call[:2] == ["systemctl", "stop"] for call in fake.calls)


@pytest.mark.parametrize(
    "bad",
    [
        ("phase", []),
        ("runner_original", {"active_state": [], "sub_state": "running"}),
        ("runner_stop_intent", "yes"),
    ],
)
def test_ci_invalid_private_state_fields_fail_without_mutations(fake, bad):
    ci.pause({}, fake.path)
    state = fake.read()
    state[bad[0]] = bad[1]
    fake.path.write_text(json.dumps(state))
    count = len(fake.calls)
    with pytest.raises(ci.MaintenanceError, match="ci_state_invalid"):
        ci.resume({}, fake.path)
    assert len(fake.calls) == count


def test_ci_native_process_probe_detects_worker_and_orphan_deploy_without_payload(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    for pid, comm, argv in [
        ("101", "Runner.Worker", b"Runner.Worker\0"),
        ("102", "bash", b"bash\0-c\0STORE_DEPLOY_ROOT=/opt/autostop-app synthetic-private-token\0"),
    ]:
        directory = proc / pid
        directory.mkdir(parents=True)
        (directory / "comm").write_text(comm)
        (directory / "cmdline").write_bytes(argv)
    real_path = Path
    monkeypatch.setattr(ci, "Path", lambda value: proc if value == "/proc" else real_path(value))
    assert ci._process_fences() == (True, True)


def test_ci_runner_start_failure_still_restores_all_own_workflows(fake):
    ci.pause({}, fake.path)
    fake.fail_start = True
    with pytest.raises(ci.MaintenanceError, match="ci_resume_incomplete"):
        ci.resume({}, fake.path)
    assert_original_workflows(fake)
    assert fake.read()["phase"] == "recovery_required"
    fake.fail_start = False
    ci.resume({}, fake.path)
    assert fake.runner["active_state"] == "active"


@pytest.mark.parametrize(
    "policy",
    [
        {"ci": {"repository": "other/repo"}},
        {"ci": {"runner_unit": "other.service"}},
        {"ci": {"runner_root": "/tmp/runner"}},
        {"ci": {"poll_seconds": 0}},
    ],
)
def test_ci_target_and_limits_fail_closed_before_commands(fake, policy):
    with pytest.raises(ci.MaintenanceError):
        ci.pause(policy, fake.path)
    assert fake.calls == []
