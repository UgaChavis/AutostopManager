#!/usr/bin/env python3
"""One-time qualification of the legacy CRM stop and return; never a release or backup."""

from __future__ import annotations

import argparse
from contextlib import suppress
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid


def _module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


common = _module("legacy_transition_common", "crm_backup_coordinator.py")
gate = _module("legacy_transition_gate", "crm_legacy_gate.py")
ROOT = Path("/var/lib/autostop-manager/crm-first-transition")
OWNER_UID = 0
REVISION = "fe3c3c73d2800b6381ed2e1a489ec4be742d9e9c"
CONTAINER = "autostopcrm"
DATA = Path("/opt/autostopcrm/data")
DEPLOY_LOCK = Path("/opt/autostopcrm/.autostop-deploy.lock")
SCRIPT = Path(__file__).resolve()
BLOCKERS = (
    "autostop-manager-scheduler.service",
    "autostop-manager-mcp.service",
    "autostop-app-watchdog.timer",
    "autostop-app-watchdog.service",
    "autostopcrm-watchdog.timer",
    "autostopcrm-watchdog.service",
)
PRECOMMIT = {"prepared", "freeze_intent", "frozen", "gated"}


class Uncertain(RuntimeError):
    pass


def run(*args, timeout=30):
    return common.run(*args, timeout=timeout)


def load(root):
    return common.load(root / "attempt.json")


def save(root, record, **fields):
    record.update(fields)
    common.save(root / "attempt.json", record)
    return record


def private_root(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if root.is_symlink() or not root.is_dir() or info.st_uid != OWNER_UID or info.st_mode & 0o077:
        raise Uncertain("transition state is not private")


def unit(attempt, kind):
    return f"autostop-crm-first-transition-{attempt}-{kind}.service"


def source_digest():
    digest = hashlib.sha256()
    for path in (SCRIPT, Path(common.__file__), Path(gate.__file__)):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def same_generation(before, actual):
    return all(before[k] == actual[k] for k in ("id", "started", "restarts", "revision"))


class System:
    container = CONTAINER
    data = DATA

    def boot(self):
        return common.System().boot()

    def daemon(self):
        return common.System().daemon()

    def inspection(self):
        return json.loads(run("docker", "inspect", self.container))[0]

    def crm(self):
        value = self.inspection()
        state = value["State"]
        return {
            "id": value["Id"],
            "state": state["Status"],
            "started": state["StartedAt"],
            "finished": state["FinishedAt"],
            "exit": str(state["ExitCode"]),
            "oom": str(state["OOMKilled"]).lower(),
            "restarts": str(value["RestartCount"]),
            "health": state.get("Health", {}).get("Status", ""),
            "revision": value["Config"].get("Labels", {}).get("org.opencontainers.image.revision", ""),
        }

    def preconditions(self):
        states = {}
        for name in BLOCKERS:
            raw = run("systemctl", "show", name, "-p", "LoadState,ActiveState,MainPID,ControlPID,ControlGroup")
            value = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
            if value.get("LoadState") == "not-found":
                states[name] = "not-found"
                continue
            state = value.get("ActiveState", "unknown")
            if state == "inactive":
                if value.get("MainPID", "0") != "0" or value.get("ControlPID", "0") != "0":
                    state = "processes_remaining"
                group = value.get("ControlGroup", "")
                if group:
                    path = Path("/sys/fs/cgroup") / group.lstrip("/") / "cgroup.procs"
                    if path.exists() and path.read_text().strip():
                        state = "processes_remaining"
            states[name] = state
        try:
            store = json.loads(run("docker", "inspect", "autostop-app"))[0]
            states["autostop-app"] = "stopped" if store["State"]["Status"] == "exited" else store["State"]["Status"]
        except Exception as exc:
            raise Uncertain("Store maintenance state is unavailable") from exc
        return states

    def validate(self):
        info = self.inspection()
        if info["State"].get("Paused") or not info["HostConfig"].get("Init"):
            raise Uncertain("CRM process wrapper is not qualified")
        if info["Config"].get("StopSignal", "") not in ("", "SIGTERM", "15"):
            raise Uncertain("CRM stop signal is not qualified")
        if info["HostConfig"]["RestartPolicy"]["Name"] != "always":
            raise Uncertain("CRM restart policy differs")
        if (
            info["Config"].get("WorkingDir") != "/app"
            or info["Config"].get("Cmd") != ["python", "scripts/container_entrypoint.py"]
            or info["Config"].get("Entrypoint")
        ):
            raise Uncertain("CRM startup command differs")
        mounts = info["Mounts"]
        for mount in mounts:
            destination = mount["Destination"].rstrip("/")
            if (
                destination == "/app/src/minimal_kanban/web_app_assets/source/automotive_tool_catalog.json"
                and not mount["RW"]
            ):
                continue
            if not destination or destination == "/app" or destination.startswith("/app/"):
                raise Uncertain("CRM source is overlaid")
            if destination == "/opt/AutostopManager" and mount["RW"]:
                raise Uncertain("Manager source is writable")
            if destination.startswith("/opt/AutostopManager/") and destination != "/opt/AutostopManager/data":
                raise Uncertain("Manager source is overlaid")
        if not any(
            m["Source"] == str(self.data) and m["Destination"] == "/home/autostop/.minimal-kanban" for m in mounts
        ):
            raise Uncertain("CRM data binding differs")
        versions = run(
            "docker",
            "exec",
            info["Id"],
            "python",
            "-c",
            'from importlib.metadata import version; print(" ".join(version(x) for x in ("mcp","anyio","uvicorn","starlette")))',
        )
        if versions != "1.28.1 4.15.1 0.54.0 1.10.0":
            raise Uncertain("CRM runtime dependencies differ")
        common.check_host_writers()

    def capture(self, initial):
        info = self.inspection()
        if info["Id"] != initial["id"]:
            raise Uncertain("CRM identity changed before capture")
        rows = run("docker", "top", initial["id"], "-eo", "pid,ppid,comm").splitlines()[1:]
        parts = [row.split() for row in rows]
        pids = sorted(int(row[0]) for row in parts)
        children = [int(row[0]) for row in parts if row[1] == str(info["State"]["Pid"]) and row[2] == "python"]
        if len(children) != 1 or len(pids) != 2:
            raise Uncertain("CRM process topology differs")
        pid = children[0]
        return gate.capture(pid, self.data / "agent", Path(f"/proc/{pid}/cgroup").read_text(), expected_pids=pids)

    def freeze(self, value):
        gate.freeze(value)

    def gate(self, value):
        gate.gate(value)

    def rollback(self, value):
        gate.rollback(value)

    def thaw_after_term(self, value):
        gate.thaw_after_term(value)

    def launch_stop(self, identity):
        return subprocess.Popen(
            ["docker", "stop", "--signal", "SIGTERM", "--timeout", "-1", identity],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def launch_worker(self, attempt):
        run("systemctl", "start", "--no-block", unit(attempt, "worker"))

    def wait_worker(self, root, attempt):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            record = load(root)
            if record["attempt"] != attempt:
                raise Uncertain("transition attempt changed")
            state = run("systemctl", "show", unit(attempt, "worker"), "-p", "ActiveState", "--value")
            if state in {"inactive", "failed"} and record["phase"] in {
                "complete",
                "precommit_failed",
                "stop_unknown",
                "unknown",
            }:
                return
            if state == "failed":
                raise Uncertain("transition worker failed")
            time.sleep(0.1)
        raise Uncertain("transition worker is still active")

    def start(self, identity):
        run("docker", "start", identity, timeout=None)

    def healthy(self, identity):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            actual = self.crm()
            if actual["id"] != identity:
                raise Uncertain("CRM container changed during health check")
            if actual["state"] == "running" and actual["health"] == "healthy":
                return
            time.sleep(0.5)
        raise Uncertain("CRM health is not confirmed")


def plan(system=None):
    system = system or System()
    states = system.preconditions()
    initial = system.crm()
    system.validate()
    if initial["revision"] != REVISION or initial["state"] != "running" or initial["health"] != "healthy":
        raise Uncertain("legacy CRM revision or health differs")
    return {
        "initial": initial,
        "boot": system.boot(),
        "daemon": system.daemon(),
        "source_digest": source_digest(),
        "prerequisites": states,
        "ready": all(value in ("inactive", "not-found", "stopped") for value in states.values()),
    }


def _prepare_gate(root, record, system):
    value = None
    try:
        value = system.capture(record["initial"])
        save(root, record, gate=value, phase="freeze_intent")
        system.freeze(value)
        save(root, record, phase="frozen")
        system.gate(value)
        save(root, record, phase="gated")
        if not same_generation(record["initial"], system.crm()):
            raise Uncertain("CRM changed during gate")
        save(root, record, phase="stop_intent")
        return value
    except BaseException:
        # No Docker request has been sent. The live owner may safely undo its
        # own gate even if the last write/fsync failed after rename.
        if value is not None:
            system.rollback(value)
            system.healthy(record["initial"]["id"])
        with suppress(Exception):
            save(root, record, phase="complete", restore="ok", result="aborted_before_stop")
        raise


def worker(root, attempt, system=None):
    system = system or System()
    with common.Lock(DEPLOY_LOCK, wait=False), common.Lock(root / "operation.lock"):
        record = load(root)
        if record["attempt"] != attempt or record["phase"] != "prepared":
            return
        if record["source_digest"] != source_digest():
            raise Uncertain("transition source changed")
        current = plan(system)
        if not current["ready"] or not same_generation(record["initial"], current["initial"]):
            save(root, record, phase="precommit_failed", result="preconditions_changed")
            return
        if current["boot"] != record["boot"] or current["daemon"] != record["daemon"]:
            save(root, record, phase="precommit_failed", result="runtime_changed")
            return
        value = _prepare_gate(root, record, system)
        process = None
        stopped = None
        journal_errors = []

        def note(**fields):
            # Once the request exists, storage errors must not strand the
            # frozen process. This worker outlives the controller and finishes
            # the already-owned request using its in-memory identity proof.
            try:
                save(root, record, **fields)
            except Exception as exc:  # noqa: BLE001 - defer until CRM returned.
                journal_errors.append(exc)

        try:
            try:
                process = system.launch_stop(record["initial"]["id"])
            except OSError:
                # Popen failed before a child was created: no Docker RPC exists.
                system.rollback(value)
                system.healthy(record["initial"]["id"])
                note(phase="complete", restore="ok", result="stop_not_sent")
                raise
            note(phase="stop_running")
            deadline = time.monotonic() + 15
            while True:
                try:
                    system.thaw_after_term(value)
                    break
                except (Uncertain, gate.Error):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise Uncertain("Docker stop TERM delivery is unknown") from None
                    time.sleep(0.05)
            if process.wait() != 0:
                raise Uncertain("Docker stop failed")
            observed = system.crm()
            if not common.same_stopped(record["initial"], observed):
                raise Uncertain("CRM did not stop cleanly")
            if system.boot() != record["boot"] or system.daemon() != record["daemon"]:
                raise Uncertain("runtime changed during stop")
            stopped = observed
            note(phase="stopped", stop_receipt=stopped, result="stopped")
        except BaseException:
            if process is not None and stopped is None:
                note(phase="stop_unknown", restore="unknown", result="stop_unknown")
            raise
        finally:
            if stopped is not None:
                # A healthy return is mandatory even when the controller died
                # or the durable stop receipt could not be written.
                if system.crm() != stopped:
                    raise Uncertain("owned stopped CRM changed before return")
                note(phase="start_intent")
                system.start(stopped["id"])
                system.healthy(stopped["id"])
                after = system.crm()
                if after["id"] != stopped["id"] or after["started"] == record["initial"]["started"]:
                    raise Uncertain("CRM did not enter a new generation")
                note(
                    phase="complete",
                    restore="ok",
                    result="returned_with_journal_error" if journal_errors else "stop_and_return_verified",
                )
        if journal_errors:
            raise Uncertain("CRM returned; transition journal durability failed")


def recover(root, attempt, system=None):
    system = system or System()
    with common.Lock(DEPLOY_LOCK, wait=False), common.Lock(root / "operation.lock"):
        record = load(root)
        if record["attempt"] != attempt:
            raise Uncertain("transition attempt changed")
        if record["phase"] == "complete":
            return record
        if record["source_digest"] != source_digest():
            raise Uncertain("transition source changed")
        actual = system.crm()
        if actual["id"] != record["initial"]["id"] or actual["revision"] != record["initial"]["revision"]:
            raise Uncertain("CRM identity changed during recovery")
        changed = system.boot() != record["boot"] or system.daemon() != record["daemon"]
        phase = record["phase"]
        if changed:
            # A reboot/daemon restart invalidates old pidfds and requests. Only
            # observe a new, already healthy generation; never replay stop.
            if actual["state"] != "running" or actual["started"] == record["initial"]["started"]:
                raise Uncertain("restarted runtime requires reconciliation")
            system.healthy(actual["id"])
            return save(root, record, phase="complete", restore="ok", result="interrupted_runtime_changed")
        if phase in PRECOMMIT:
            if not same_generation(record["initial"], actual) or actual["state"] != "running":
                raise Uncertain("precommit CRM generation changed")
            if phase != "prepared":
                system.rollback(record["gate"])
            system.healthy(actual["id"])
            return save(root, record, phase="complete", restore="ok", result="aborted_before_stop")
        if phase == "precommit_failed":
            system.healthy(actual["id"])
            return save(root, record, phase="complete", restore="ok")
        if phase == "start_intent":
            if actual["state"] == "running" and actual["started"] != record["initial"]["started"]:
                system.healthy(actual["id"])
                return save(root, record, phase="complete", restore="ok", result="stop_and_return_verified")
            raise Uncertain("start outcome unknown; not repeated")
        if phase != "stopped":
            save(root, record, restore="unknown")
            raise Uncertain("in-flight stop outcome unknown; not repeated")
        if actual != record.get("stop_receipt") or not common.same_stopped(record["initial"], actual):
            raise Uncertain("stopped CRM receipt changed")
        journal_error = None
        try:
            save(root, record, phase="start_intent")
        except Exception as exc:  # noqa: BLE001 - exact owned stop is already confirmed.
            journal_error = exc
        system.start(actual["id"])
        system.healthy(actual["id"])
        after = system.crm()
        if after["id"] != actual["id"] or after["started"] == record["initial"]["started"]:
            raise Uncertain("CRM did not enter a new generation")
        result = "returned_with_journal_error" if journal_error else "stop_and_return_verified"
        try:
            save(root, record, phase="complete", restore="ok", result=result)
        except OSError:
            raise Uncertain("CRM returned; recovery journal durability failed") from None
        if journal_error:
            raise Uncertain("CRM returned; recovery journal durability failed")
        return record


def controller(root, attempt, system=None):
    system = system or System()
    with common.Lock(root / "controller.lock", wait=False):
        record = load(root)
        if record["attempt"] != attempt or record["phase"] != "prepared":
            raise Uncertain("transition is not prepared")
        try:
            system.launch_worker(attempt)
            system.wait_worker(root, attempt)
        finally:
            recover(root, attempt, system)
        result = load(root)
        if result["restore"] != "ok" or result["result"] != "stop_and_return_verified":
            raise Uncertain("stop qualification was not successful")
        return result


def _install_units(attempt):
    if any(c.isspace() for c in str(SCRIPT)):
        raise Uncertain("unsafe script path")
    directory = Path("/run/systemd/system")
    created = []
    for kind, action in (("main", "controller"), ("worker", "worker"), ("recovery", "recover")):
        extra = (
            f"ExecStopPost=/usr/bin/systemctl start --no-block {unit(attempt, 'recovery')}\n"
            if kind == "main"
            else "Restart=on-failure\nRestartSec=5s\n"
            if kind == "recovery"
            else ""
        )
        text = (
            "[Unit]\nDescription=One-time legacy CRM stop qualification\nAfter=docker.service\n"
            + ("StartLimitIntervalSec=0\n" if kind == "recovery" else "")
            + "[Service]\nType=oneshot\nUser=root\nUMask=0077\nKillMode=control-group\n"
            "TimeoutStartSec=infinity\n"
            f"ExecStart=/usr/bin/python3 {SCRIPT} {action} {attempt}\n{extra}"
        )
        path = directory / unit(attempt, kind)
        if path.exists():
            raise Uncertain("transition unit already exists")
        common.durable(path, text.encode())
        created.append(path)
    run("systemctl", "daemon-reload")
    return created


def require_supervision(action, attempt):
    kind = {"controller": "main", "worker": "worker", "recover": "recovery"}[action]
    raw = run("systemctl", "show", unit(attempt, kind), "-p", "MainPID,InvocationID")
    values = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    if values.get("MainPID") != str(os.getpid()) or values.get("InvocationID") != os.environ.get("INVOCATION_ID"):
        raise Uncertain("internal operation is not its supervised service")


def launch(system=None):
    system = system or System()
    private_root(ROOT)
    with common.Lock(ROOT / "launch.lock", wait=False), common.Lock(DEPLOY_LOCK, wait=False):
        if (ROOT / "attempt.json").exists() and load(ROOT)["phase"] != "complete":
            raise Uncertain("previous transition requires recovery")
        value = plan(system)
        if not value["ready"]:
            raise Uncertain("maintenance prerequisites are not satisfied")
        attempt = uuid.uuid4().hex
        record = {**value, "attempt": attempt, "phase": "prepared", "restore": "pending", "result": "pending"}
        common.save(ROOT / "attempt.json", record)
        created = _install_units(attempt)
    # The main service, stop worker and recovery have separate cgroups. The
    # invoking terminal may disappear without cancelling any of them.
    try:
        run("systemctl", "start", unit(attempt, "main"), timeout=None)
    finally:
        record = load(ROOT)
        if record["phase"] == "complete":
            for kind in ("main", "worker", "recovery"):
                with suppress(Exception):
                    run("systemctl", "stop", unit(attempt, kind))
            for path in created:
                path.unlink(missing_ok=True)
            run("systemctl", "daemon-reload")
    if record["restore"] != "ok" or record["result"] != "stop_and_return_verified":
        raise Uncertain("transition did not complete")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "run", "controller", "worker", "recover"))
    parser.add_argument("attempt", nargs="?")
    args = parser.parse_args()
    try:
        if args.action == "plan":
            value = plan()
            print(
                json.dumps(
                    {
                        "revision": value["initial"]["revision"],
                        "ready": value["ready"],
                        "prerequisites": value["prerequisites"],
                    }
                )
            )
        elif args.action == "run":
            value = launch()
            print(json.dumps({k: value[k] for k in ("attempt", "phase", "restore", "result")}))
        else:
            if (
                not args.attempt
                or not re.fullmatch(r"[0-9a-f]{32}", args.attempt)
                or not os.environ.get("INVOCATION_ID")
            ):
                raise Uncertain("internal operation requires its systemd service")
            require_supervision(args.action, args.attempt)
            private_root(ROOT)
            {"controller": controller, "worker": worker, "recover": recover}[args.action](ROOT, args.attempt)
    except Exception as exc:  # noqa: BLE001 - never log environment or business data.
        reason = str(exc) if isinstance(exc, Uncertain) else type(exc).__name__
        print("Legacy CRM transition incomplete:", reason, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
