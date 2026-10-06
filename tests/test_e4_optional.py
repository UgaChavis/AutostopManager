from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from autostop_manager import e4_optional as optional


SYNTHETIC_VIN = "JN1AS44D0" + "0" * 8


def fail_network(*args, **kwargs):
    raise AssertionError("decode must not use the network")


def assert_redacted(result, identifier=SYNTHETIC_VIN):
    assert identifier not in json.dumps(result)


def test_vininfo_missing_is_not_an_automatic_install(monkeypatch):
    monkeypatch.setattr(socket, "create_connection", fail_network)

    def missing():
        raise optional.OptionalDecoderError("vininfo_missing")

    monkeypatch.setattr(optional, "_load_vininfo", missing)
    result = optional.vininfo_decode(SYNTHETIC_VIN)
    assert result["outcome"] == "dependency_missing"
    assert result["vehicle_profile"] == {}
    assert_redacted(result)


def test_vininfo_keeps_model_engine_and_year_alternatives(monkeypatch):
    def vin(_identifier):
        return SimpleNamespace(
            manufacturer="Nissan",
            country="Japan",
            years=[1980, 2010],
            brand=SimpleNamespace(title="Nissan"),
            details=SimpleNamespace(
                model=SimpleNamespace(name=["Armada", "Titan", "Maxima"]),
                engine=SimpleNamespace(name=["VG30D", "VK56DE"]),
                body=SimpleNamespace(name=None, code="9"),
                transmission=SimpleNamespace(name=None, code="X"),
            ),
        )

    monkeypatch.setattr(optional, "_load_vininfo", lambda: SimpleNamespace(Vin=vin, VininfoException=ValueError))
    result = optional.vininfo_decode(SYNTHETIC_VIN)
    assert result["outcome"] == "partial"
    assert all(
        field not in result["vehicle_profile"]
        for field in ("model", "engine", "model_year", "transmission", "body_class")
    )
    assert {row["value"] for row in result["field_evidence"] if row["field"] == "model"} == {
        "Armada",
        "Titan",
        "Maxima",
    }
    assert {row["value"] for row in result["field_evidence"] if row["field"] == "model_year"} == {1980, 2010}
    assert all(row["strength"] == "candidate" for row in result["field_evidence"])
    assert_redacted(result)


@pytest.mark.parametrize(
    "identifier,outcome", [("ES1-0000000", "unsupported"), (True, "invalid_input"), ("Q" * 17, "invalid_input")]
)
def test_optional_tools_validate_before_dependency_or_source_calls(monkeypatch, identifier, outcome):
    monkeypatch.setattr(optional, "_load_vininfo", fail_network)
    monkeypatch.setattr(optional, "_node_binary", fail_network)
    assert optional.vininfo_decode(identifier)["outcome"] == outcome
    assert optional.corgi_decode(identifier)["outcome"] == outcome


def test_valid_european_z_is_unsupported_by_corgi_not_invalid(monkeypatch):
    monkeypatch.setattr(optional, "_node_binary", fail_network)
    result = optional.corgi_decode("WVWZZZ3CZEA" + "0" * 6)
    assert result["outcome"] == "unsupported"
    assert result["diagnostics"]["reason"] == "library_unsupported_format"


def ready_runtime(monkeypatch):
    monkeypatch.setattr(optional, "_node_binary", lambda: "unused-node")
    monkeypatch.setattr(optional, "_runtime_installed", lambda _runtime: True)


def test_corgi_missing_database_never_queries_or_prepares(monkeypatch, tmp_path):
    ready_runtime(monkeypatch)
    monkeypatch.setattr(optional, "_run_bounded", fail_network)
    result = optional.corgi_decode(SYNTHETIC_VIN, cache_dir=tmp_path)
    assert result["outcome"] == "database_missing"
    assert not list(tmp_path.iterdir())


def test_corgi_rejects_corrupt_snapshot_without_running_child(monkeypatch, tmp_path):
    ready_runtime(monkeypatch)
    monkeypatch.setattr(optional, "_run_bounded", fail_network)
    _runtime, database, manifest = optional._paths(tmp_path)
    database.parent.mkdir()
    database.write_bytes(b"corrupt database")
    manifest.write_text(
        json.dumps(
            {
                "package_version": optional.CORGI_VERSION,
                "compressed_sha256": optional.CORGI_COMPRESSED_SHA256,
                "upstream_database_sha256": optional.CORGI_DATABASE_SHA256,
                "database_sha256": "0" * 64,
            }
        )
    )
    result = optional.corgi_decode(SYNTHETIC_VIN, cache_dir=tmp_path)
    assert result["outcome"] == "database_invalid"
    assert result["diagnostics"]["reason"] == "snapshot_integrity_failed"
    assert_redacted(result)


def test_corgi_exact_binding_and_pattern_alternatives(monkeypatch):
    ready_runtime(monkeypatch)
    monkeypatch.setattr(optional, "_checked_snapshot", lambda *_args: {"database_sha256": "a" * 64, "data_date": None})
    payload = {
        "vin": SYNTHETIC_VIN,
        "valid": True,
        "errors": [],
        "components": {"wmi": {"make": "Nissan"}, "modelYear": {"year": 2010}},
        "patterns": [{"element": "Model", "value": "Model A"}, {"element": "Model", "value": "Model B"}],
    }
    monkeypatch.setattr(
        optional, "_run_bounded", lambda *_args, **_kwargs: (0, json.dumps({"ok": True, "result": payload}).encode())
    )
    result = optional.corgi_decode(SYNTHETIC_VIN)
    assert "model" not in result["vehicle_profile"]
    assert {row["value"] for row in result["field_evidence"] if row["field"] == "model"} == {"Model A", "Model B"}
    assert result["diagnostics"]["identifier_verified"] is True
    assert result["diagnostics"]["provider_clean"] is False
    assert_redacted(result)
    payload["vin"] = "WBA" + "0" * 14
    rejected = optional.corgi_decode(SYNTHETIC_VIN)
    assert rejected["outcome"] == "provider_failed"
    assert rejected["diagnostics"]["reason"] == "identifier_binding_mismatch"


def test_bounded_child_timeout_and_output_limits():
    with pytest.raises(optional.OptionalDecoderError, match="decoder_timeout"):
        optional._run_bounded([sys.executable, "-c", "import time; time.sleep(5)"], timeout_seconds=0.05)
    with pytest.raises(optional.OptionalDecoderError, match="response_too_large"):
        optional._run_bounded([sys.executable, "-c", "print('x' * 4096)"], max_response_bytes=1024)
    with pytest.raises(optional.OptionalDecoderError, match="stderr_too_large"):
        optional._run_bounded([sys.executable, "-c", "import sys; sys.stderr.write('x' * 70000)"])


def test_bounded_child_cancellation_terminates_promptly():
    event = threading.Event()
    timer = threading.Timer(0.05, event.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(optional.OptionalDecoderError, match="decoder_cancelled"):
            optional._run_bounded([sys.executable, "-c", "import time; time.sleep(5)"], cancel_event=event)
        assert time.monotonic() - started < 1
    finally:
        timer.cancel()


@pytest.mark.parametrize(
    "reason,outcome",
    [
        ("decoder_timeout", "decoder_timeout"),
        ("response_too_large", "response_too_large"),
        ("decoder_cancelled", "decoder_cancelled"),
    ],
)
def test_corgi_child_failures_have_distinct_outcomes(monkeypatch, reason, outcome):
    ready_runtime(monkeypatch)
    monkeypatch.setattr(optional, "_checked_snapshot", lambda *_args: {"database_sha256": "a" * 64})

    def fail(*_args, **_kwargs):
        raise optional.OptionalDecoderError(reason)

    monkeypatch.setattr(optional, "_run_bounded", fail)
    result = optional.corgi_decode(SYNTHETIC_VIN)
    assert result["outcome"] == outcome
    assert result["diagnostics"]["reason"] == reason
    assert_redacted(result)


def test_snapshot_prepare_rejects_upstream_before_replacing_existing(tmp_path):
    compressed = tmp_path / "bad.gz"
    compressed.write_bytes(b"untrusted")
    database = tmp_path / "vpic.lite.db"
    database.write_bytes(b"previous snapshot")
    with pytest.raises(optional.OptionalDecoderError, match="upstream_integrity_failed"):
        optional._prepare_snapshot(compressed, database, tmp_path / "manifest.json")
    assert database.read_bytes() == b"previous snapshot"


@pytest.mark.skipif(
    os.environ.get("AUTOSTOP_E4_RUN_OPTIONAL_INTEGRATION") != "1",
    reason="explicit local optional dependency preparation required",
)
def test_real_optional_adapters_offline_and_readonly(monkeypatch, tmp_path):
    monkeypatch.setattr(socket, "create_connection", fail_network)
    guard = tmp_path / "offline-node.mjs"
    guard.write_text(
        """
import net from 'node:net';
import tls from 'node:tls';
import http from 'node:http';
import https from 'node:https';
import http2 from 'node:http2';
import dgram from 'node:dgram';
import { syncBuiltinESMExports } from 'node:module';
const blocked = () => { throw new Error('offline-test-network-blocked'); };
net.connect = net.createConnection = net.Socket.prototype.connect = blocked;
tls.connect = http.request = http.get = https.request = https.get = blocked;
http2.connect = dgram.createSocket = blocked;
globalThis.fetch = blocked;
syncBuiltinESMExports();
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("NODE_OPTIONS", "--import=" + guard.as_uri())
    code, probe = optional._run_bounded(
        [
            optional._node_binary(),
            "--input-type=module",
            "-e",
            "import net from 'node:net'; try {net.connect({port:9}); process.exitCode=1;} catch {console.log('blocked');}",
        ]
    )
    assert code == 0 and probe.strip() == b"blocked"
    runtime, database, manifest_path = optional._paths()
    assert optional._runtime_installed(runtime)
    before = sha256(database.read_bytes()).hexdigest()
    manifest_before = manifest_path.read_bytes()
    assert optional.optional_status()["vininfo"]["status"] == "ready"
    # Public upstream README example; no private vehicle data is used.
    public_vin = "KM8K2CAB4PU001140"
    decoded = optional.corgi_decode(public_vin)
    assert decoded["outcome"] == "success"
    assert decoded["vehicle_profile"]["model"] == "Kona"
    assert decoded["vehicle_profile"]["model_year"] == 2023
    assert decoded["vehicle_profile"]["engine"] == "MPI Nu PE"
    assert_redacted(decoded, public_vin)
    renault = optional.vininfo_decode("VF1LM1B0H" + "0" * 8)
    assert renault["vehicle_profile"]["make"] == "Renault"
    nissan = optional.vininfo_decode(SYNTHETIC_VIN)
    assert len([row for row in nissan["field_evidence"] if row["field"] == "model"]) == 2
    assert "model" not in nissan["vehicle_profile"]
    assert sha256(database.read_bytes()).hexdigest() == before
    assert manifest_path.read_bytes() == manifest_before
    assert not Path(str(database) + "-wal").exists()
    assert not Path(str(database) + "-shm").exists()
