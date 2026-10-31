#!/usr/bin/env python3
"""Recovery primitive for one CRM backup attempt; not installed or scheduled.

The caller must durably persist the exact initial identities and each confirmed
stop. An interrupted stop or start is deliberately not retried automatically.
"""

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

SCHEDULER = "autostop-manager-scheduler.service"
CRM_NAME = "autostopcrm"
ID = re.compile(r"^[0-9a-f]{64}$")


class Uncertain(RuntimeError):
    pass


def _run(*args):
    r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=30)
    if r.returncode:
        raise Uncertain("command failed or outcome unknown: " + args[0])
    return r.stdout.strip()


class System:
    def identity(self, kind):
        if kind == "crm":
            value = _run("docker", "inspect", "--format", "{{.Id}}", CRM_NAME)
            if not ID.fullmatch(value):
                raise Uncertain("invalid container identity")
            return value
        definition = _run("systemctl", "cat", SCHEDULER)
        return hashlib.sha256(definition.encode()).hexdigest()

    def state(self, kind):
        if kind == "crm":
            value = _run("docker", "inspect", "--format", "{{.State.Status}}", CRM_NAME)
            if value == "running":
                return "active"
            if value in ("exited", "created"):
                return "inactive"
            raise Uncertain("container in transition or unknown state")
        value = _run("systemctl", "show", SCHEDULER, "--property=ActiveState", "--value")
        if value not in ("active", "inactive", "failed"):
            raise Uncertain("unit in transition or unknown state")
        return "inactive" if value == "failed" else value

    def start(self, kind):
        if kind == "crm":
            _run("docker", "start", CRM_NAME)
        else:
            _run("systemctl", "start", SCHEDULER)

    def healthy(self, kind):
        if kind == "crm":
            return (
                _run("docker", "inspect", "--format", "{{if .State.Health}}{{.State.Health.Status}}{{end}}", CRM_NAME)
                == "healthy"
            )
        return self.state(kind) == "active"


def _save(path, record):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".recovery-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def recover(path, system=None):
    """Resume confirmed stops only, preserving the journal on every failure.

    Expected journal has exactly crm and scheduler entries, each with identity,
    initially_active (bool), and phase: untouched, stop_intent, stopped,
    start_intent or restored. Stops must have been confirmed by the caller.
    Recovery is serialized by an exclusive lock next to the private journal.
    """
    system = system or System()
    path = Path(path)
    lock = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with path.open() as f:
            record = json.load(f)
        if set(record) != {"crm", "scheduler"}:
            raise Uncertain("unknown resource set")
        for kind in ("crm", "scheduler"):
            item = record[kind]
            if set(item) != {"identity", "initially_active", "phase"} or type(item["initially_active"]) is not bool:
                raise Uncertain("invalid record")
            if not item["identity"] or item["phase"] not in (
                "untouched",
                "stop_intent",
                "stopped",
                "start_intent",
                "restored",
            ):
                raise Uncertain("invalid identity or phase")
            if system.identity(kind) != item["identity"]:
                raise Uncertain("resource identity changed")
            if item["phase"] in ("stop_intent", "start_intent"):
                raise Uncertain("previous operation outcome unknown")
            if item["phase"] == "stopped" and not item["initially_active"]:
                raise Uncertain("initially inactive resource was stopped")
        # CRM first: scheduler may issue requests as soon as it starts.
        for kind in ("crm", "scheduler"):
            item = record[kind]
            if item["phase"] != "stopped":
                continue
            if kind == "scheduler" and (system.state("crm") != "active" or not system.healthy("crm")):
                raise Uncertain("CRM not healthy; scheduler remains stopped")
            current = system.state(kind)
            if current == "active":
                # A reboot or another actor may have started it. Do not claim ownership.
                raise Uncertain("resource active without confirmed recovery")
            if system.identity(kind) != item["identity"]:
                raise Uncertain("resource identity changed")
            item["phase"] = "start_intent"
            _save(path, record)
            system.start(kind)
            if system.identity(kind) != item["identity"] or system.state(kind) != "active" or not system.healthy(kind):
                raise Uncertain("start not confirmed")
            item["phase"] = "restored"
            _save(path, record)
        return record
    finally:
        os.close(lock)
