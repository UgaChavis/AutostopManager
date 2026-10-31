import fcntl
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.request

import pytest

spec = importlib.util.spec_from_file_location("legacy_gate", Path(__file__).parents[1] / "scripts/crm_legacy_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

SERVER = r"""
import http.server, os, signal, sys, threading, time
from pathlib import Path
root = Path(sys.argv[3])
stop = [False]
def stopping(*_): stop[0] = True
signal.signal(signal.SIGTERM, stopping)
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/slow':
            (root/'handler-started').write_text('synthetic')
            while not (root/'release').exists(): time.sleep(.01)
        self.send_response(200)
        self.end_headers()
        try: self.wfile.write(b'synthetic')
        except OSError: pass
    def log_message(self,*args): pass
class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = 64
servers = [Server(('127.0.0.1',int(port)),Handler) for port in sys.argv[1:3]]
for server in servers:
    threading.Thread(target=server.serve_forever,daemon=True).start()
print('ready',flush=True)
while not stop[0]: time.sleep(.02)
for server in servers: server.shutdown(); server.server_close()
"""


def agent_state(root):
    root.mkdir(mode=0o700)
    for name, value in {
        "tasks.json": [],
        "schedules.json": [],
        "status.json": {"running": False, "current_task_id": None, "current_run_id": None},
    }.items():
        path = root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    (root / "agent.lock").touch(mode=0o600)


def test_agent_lock_busy_fails_immediately_and_does_not_expose_payload(tmp_path):
    root = tmp_path / "agent"
    agent_state(root)
    fd = os.open(root / "agent.lock", os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        start = time.monotonic()
        with pytest.raises(gate.Error, match="lock busy"):
            gate._agent_idle({"agent_dir": str(root), "uid": os.geteuid()})
        assert time.monotonic() - start < 0.5
    finally:
        os.close(fd)


@pytest.mark.parametrize(
    "name,value",
    [
        ("tasks.json", [{"status": "running", "task_text": "private-synthetic"}]),
        ("tasks.json", [{"status": "future-unknown", "task_text": "private-synthetic"}]),
        ("schedules.json", [{"active": True, "task_text": "private-synthetic"}]),
        ("status.json", {"running": False}),
    ],
)
def test_busy_or_unknown_agent_state_refuses_gate_without_payload(tmp_path, name, value):
    root = tmp_path / "agent"
    agent_state(root)
    (root / name).write_text(json.dumps(value))
    with pytest.raises(gate.Error) as error:
        gate._agent_idle({"agent_dir": str(root), "uid": os.geteuid()})
    assert "private-synthetic" not in str(error.value)


@pytest.fixture
def synthetic(tmp_path, monkeypatch):
    if os.geteuid() != 0:
        pytest.skip("pidfd signal qualification requires root")
    reservations = [socket.socket(), socket.socket()]
    for sock in reservations:
        sock.bind(("127.0.0.1", 0))
    ports = [sock.getsockname()[1] for sock in reservations]
    for sock in reservations:
        sock.close()
    root = tmp_path / "agent"
    agent_state(root)
    child = subprocess.Popen(
        [sys.executable, "-c", SERVER, *map(str, ports), str(tmp_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "MINIMAL_KANBAN_BOARD_CONTROL_ENABLED": "0"},
    )
    try:
        assert select.select([child.stdout], [], [], 5)[0]
        assert child.stdout.readline().strip() == b"ready"
        monkeypatch.setattr(gate, "PORTS", tuple(ports))
        monkeypatch.setattr(gate, "_cgroup_pids", lambda _: [child.pid])
        cgroup = Path(f"/proc/{child.pid}/cgroup").read_text()
        try:
            record = gate.capture(child.pid, root, cgroup, os.geteuid(), expected_pids=[child.pid])
        except PermissionError:
            pytest.skip("pidfd_getfd is restricted on this runner")
        yield child, record, ports, tmp_path
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGCONT)
            (tmp_path / "release").touch()
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        child.stdout.close()


def readback(ports):
    for port in ports:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
            assert response.status == 200 and response.read() == b"synthetic"


def test_exact_fixed_port_gate_restores_both_listeners_before_resume(synthetic):
    child, record, ports, _ = synthetic
    assert all(item["backlog"] == 64 for item in record["listeners"])
    gate.freeze(record)
    gate.gate(record)
    assert gate._frozen(record)
    gate.rollback(record)
    readback(ports)
    assert child.poll() is None


def test_rst_handler_remains_visible_and_gate_refuses_without_closing_listeners(synthetic):
    _, record, ports, root = synthetic
    request = socket.create_connection(("127.0.0.1", ports[0]))
    request.sendall(b"GET /slow HTTP/1.0\r\nHost: synthetic\r\n\r\n")
    deadline = time.monotonic() + 3
    while not (root / "handler-started").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert (root / "handler-started").exists()
    request.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    request.close()
    gate.freeze(record)
    with pytest.raises(gate.Error, match="accepted requests"):
        gate.gate(record)
    gate.rollback(record)
    (root / "release").touch()
    readback(ports)


def test_no_resume_until_stop_signal_is_pending(synthetic):
    child, record, _, _ = synthetic
    gate.freeze(record)
    gate.gate(record)
    with pytest.raises(gate.Error, match="stop signal"):
        gate.thaw_after_term(record, timeout=0.03)
    assert gate._frozen(record)
    child.send_signal(signal.SIGTERM)
    gate.thaw_after_term(record)
    assert child.wait(timeout=5) == 0


def test_changed_process_identity_refuses_before_signal(synthetic):
    _, record, ports, _ = synthetic
    record["start_ticks"] += 1
    with pytest.raises(gate.Error, match="process changed"):
        gate.freeze(record)
    readback(ports)
