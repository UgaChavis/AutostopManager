from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "linux", reason="PR_SET_MDWE is Linux-specific")
def test_corgi_worker_decodes_readonly_sqlite_with_executable_memory_denied(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("actual Node runtime not installed")
    version = subprocess.check_output([node, "--version"], text=True, timeout=10).strip()
    if tuple(int(part) for part in version.removeprefix("v").split(".")) < (22, 13, 0):
        pytest.skip("actual worker requires Node>=22.13 with node:sqlite")
    node_path = Path(node).resolve()
    database = tmp_path / "vpic.lite.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE Wmi (make TEXT, model TEXT)")
        connection.execute("INSERT INTO Wmi VALUES ('DEMO', 'MDWE fixture')")
    core = tmp_path / "corgi-browser.mjs"
    core.write_text(
        "export async function decodeVIN(identifier, db) {\n"
        "  let networkDenied = false;\n"
        "  try { await fetch('data:text/plain,synthetic-fixture'); }\n"
        "  catch (error) { networkDenied = error.message === 'offline_network_denied'; }\n"
        "  if (!networkDenied) throw new Error('worker_network_guard_missing');\n"
        "  const [result] = await db.exec('SELECT make, model FROM Wmi');\n"
        "  return { valid: true, components: { vehicle: Object.fromEntries(\n"
        "    result.columns.map((key, i) => [key, result.values[0][i]])) }};\n"
        "}\n"
    )
    manifest = {
        "schema": "autostop.automotive-offline-runtime.v1",
        "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (core, database)},
        "corgi": {"version": "2.0.4", "database_version": "synthetic MDWE fixture"},
        "node": {
            "executable": str(node_path),
            "version": version,
            "sha256": hashlib.sha256(node_path.read_bytes()).hexdigest(),
        },
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    child = """
import ctypes, errno, json, mmap, os, resource, sys
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(65, 1, 0, 0, 0) != 0:
    if ctypes.get_errno() == 22:  # Kernel does not implement PR_SET_MDWE.
        sys.exit(77)
    raise RuntimeError('PR_SET_MDWE_failed')
assert libc.prctl(66, 0, 0, 0, 0) == 1
memory = mmap.mmap(-1, mmap.PAGESIZE, prot=mmap.PROT_READ | mmap.PROT_WRITE)
address = ctypes.addressof(ctypes.c_char.from_buffer(memory))
assert libc.mprotect(ctypes.c_void_p(address), mmap.PAGESIZE, mmap.PROT_READ | mmap.PROT_EXEC) == -1
assert ctypes.get_errno() in (errno.EPERM, errno.EACCES)
sys.path.insert(0, sys.argv[1])
os.environ['AUTOSTOP_AUTOMOTIVE_OFFLINE_RUNTIME'] = sys.argv[2]
from autostop_manager import automotive_offline as offline
actual_run = offline.subprocess.run
worker_diagnostics = []
def trace_worker(*args, **kwargs):
    result = actual_run(*args, **kwargs)
    worker_diagnostics.append({'returncode': result.returncode, 'stderr': result.stderr[:2000]})
    return result
offline.subprocess.run = trace_worker
positive = offline.corgi_decode('AAA00000000000000')
negative = offline.corgi_decode('INVALID')
print(json.dumps({'positive': positive, 'negative': negative, 'MDWE': libc.prctl(66, 0, 0, 0, 0),
                  'worker_diagnostics': worker_diagnostics}))
"""
    response = subprocess.run(
        [sys.executable, "-I", "-B", "-c", child, str(Path(__file__).resolve().parents[1]), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if response.returncode == 77:
        pytest.skip("kernel does not implement PR_SET_MDWE")
    assert response.returncode == 0, response.stderr
    result = json.loads(response.stdout)
    assert result["MDWE"] == 1
    assert result["positive"]["outcome"] == "partial", {
        "node_version": version,
        "worker_diagnostics": result["worker_diagnostics"],
    }
    assert result["positive"]["data"]["vehicle_profile"] == {"make": "DEMO", "model": "MDWE fixture"}
    assert result["positive"]["execution"]["network_calls"] == 0
    assert result["negative"]["outcome"] == "invalid_input"
    assert hashlib.sha256(database.read_bytes()).hexdigest() == manifest["files"][database.name]
