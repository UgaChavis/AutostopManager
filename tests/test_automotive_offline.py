from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from autostop_manager import automotive_offline as offline
from autostop_manager.automotive_contracts import binding
from autostop_manager.automotive_local_registry import PLATFORM_RULES, WMI_HINTS


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    files = {
        "python-packages/vininfo/__init__.py": b"# pinned test library\n",
        "corgi-browser.mjs": b"// pinned test core\n",
        "vpic.lite.db": b"test database",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    manifest = {
        "schema": offline.RUNTIME_SCHEMA,
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
        "vininfo": {"version": "1.11.0", "source_url": "https://github.com/idlesign/vininfo"},
        "corgi": {
            "version": "2.0.4",
            "database_version": "fixture",
            "source_url": "https://github.com/cardog-ai/corgi",
        },
        "node": {"executable": sys.executable, "version": "fixture", "sha256": offline._sha256(Path(sys.executable))},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setenv("AUTOSTOP_AUTOMOTIVE_OFFLINE_RUNTIME", str(tmp_path))
    return tmp_path, manifest


@pytest.fixture(autouse=True)
def no_network():
    with (
        patch("socket.socket", side_effect=AssertionError("network forbidden")),
        patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")),
        patch("urllib.request.urlopen", side_effect=AssertionError("HTTP forbidden")),
    ):
        yield


def test_wmi_is_assembler_hint_without_silent_model_or_market():
    row = offline.decode_wmi_local(" wau ")
    assert row["data"]["vehicle_profile"]["make"] == "Audi"
    assert "market" not in row["data"]["vehicle_profile"]
    assert "model" not in row["data"]["vehicle_profile"]
    assert row["execution"]["network_calls"] == 0
    assert row["evidence"][0]["scope"] == "family_hint"


@pytest.mark.parametrize("value", [None, 123, "W", "WAU0", "WQI"])
def test_wmi_requires_exact_valid_wmi(value):
    assert offline.decode_wmi_local(value)["outcome"] == "invalid_input"


def test_unknown_wmi_does_not_fallback_to_online_decoder():
    assert offline.decode_wmi_local("AAA")["outcome"] == "unsupported"


def test_local_registry_is_shared_with_compatible_decoder():
    from autostop_manager import vehicle_identity

    assert vehicle_identity.WMI_HINTS is WMI_HINTS
    assert vehicle_identity.PLATFORM_RULES is PLATFORM_RULES


def test_frame_supported_and_unsupported_preserve_binding_and_scope():
    row = offline.decode_frame_local("ES1-1234567")
    assert row["data"]["input_binding"] == binding("ES1-1234567", "frame_number")
    assert row["data"]["vehicle_profile"]["model_family"] == "Civic"
    assert "engine" not in row["data"]["vehicle_profile"]
    assert offline.decode_frame_local("ZZ99-1234567")["outcome"] == "unsupported"
    assert offline.decode_frame_local("1HGCM82633A123456")["outcome"] == "invalid_input"
    assert offline.decode_frame_local("ES1")["outcome"] == "invalid_input"


def test_brand_registry_is_limited_and_retains_context_conflict():
    row = offline.vin_brand_details("WAUZZZ4H0FN000001", {"make": "Toyota"})
    assert row["outcome"] == "partial"
    assert row["data"]["vehicle_profile"]["model_family"] == "A8"
    assert row["conflicts"][0]["field"] == "make"
    assert "engine" not in row["data"]["vehicle_profile"]
    assert offline.vin_brand_details("1HGCM82633A123456")["outcome"] == "unsupported"
    assert offline.vin_brand_details("WAUZZZ4H0FN000001", [])["outcome"] == "invalid_input"


@pytest.mark.parametrize("decoder", [offline.vininfo_decode, offline.corgi_decode])
def test_invalid_vin_returns_before_runtime_or_process(decoder):
    with patch.object(offline, "_runtime", side_effect=AssertionError("runtime must not load")):
        assert decoder("NOT-A-VIN")["outcome"] == "invalid_input"


def test_no_preparation_or_decoder_switching_when_runtime_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSTOP_AUTOMOTIVE_OFFLINE_RUNTIME", str(tmp_path / "missing"))
    with patch.object(offline.subprocess, "run", side_effect=AssertionError("no implicit install")):
        assert offline.vininfo_decode("1HGCM82633A123456")["outcome"] == "dependency_missing"
        assert offline.corgi_decode("1HGCM82633A123456")["outcome"] == "dependency_missing"


@pytest.mark.parametrize("decoder", [offline.vininfo_decode, offline.corgi_decode])
def test_invalid_runtime_resolution_is_configuration_failure_without_worker(tmp_path, monkeypatch, decoder):
    loop = tmp_path / "loop"
    loop.symlink_to(loop.name)
    monkeypatch.setenv("AUTOSTOP_AUTOMOTIVE_OFFLINE_RUNTIME", str(loop))
    with patch.object(offline.subprocess, "run", side_effect=AssertionError("invalid runtime must not run")):
        row = decoder("AAA00000000000000")
    assert row["outcome"] == "configuration_missing" and row["execution"]["network_calls"] == 0


@pytest.mark.parametrize("node", [None, [], "invalid", 1])
def test_malformed_node_metadata_is_configuration_failure_without_worker(runtime, node):
    root, manifest = runtime
    manifest["node"] = node
    (root / "manifest.json").write_text(json.dumps(manifest))
    with patch.object(offline.subprocess, "run", side_effect=AssertionError("invalid node must not run")):
        row = offline.corgi_decode("AAA00000000000000")
    assert row["outcome"] == "configuration_missing"


def test_unreadable_node_digest_is_structured_configuration_failure(runtime):
    original = offline._sha256

    def unreadable(path):
        if path == Path(sys.executable):
            raise OSError("synthetic unreadable node")
        return original(path)

    with (
        patch.object(offline, "_sha256", side_effect=unreadable),
        patch.object(offline.subprocess, "run", side_effect=AssertionError("unreadable node must not run")),
    ):
        assert offline.corgi_decode("AAA00000000000000")["outcome"] == "configuration_missing"


@pytest.mark.parametrize("decoder", [offline.vininfo_decode, offline.corgi_decode])
@pytest.mark.parametrize("reason", [True, {}, ["decoder failure"], 0])
def test_malformed_worker_error_returns_parse_error_instead_of_native_exception(runtime, decoder, reason):
    payload = {"vehicle_profile": {"make": "DEMO", "manufacturer": "DEMO"}, "error": reason}
    with patch.object(
        offline.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), "")
    ) as call:
        row = decoder("AAA00000000000000")
    assert row["outcome"] == "parse_error" and row["warnings"] == ["parse_error"]
    assert call.call_count == 1 and row["execution"]["network_calls"] == 0


def test_provider_runtime_dependencies_are_independent(runtime):
    root, _ = runtime
    (root / "vpic.lite.db").unlink()
    assert offline._runtime("vininfo")[2] is None
    assert offline._runtime("corgi")[2] == "database_missing"


def test_modified_database_fails_closed_without_running_process(runtime):
    root, _ = runtime
    (root / "vpic.lite.db").write_bytes(b"modified")
    with patch.object(offline.subprocess, "run", side_effect=AssertionError("tampered database")):
        assert offline.corgi_decode("1HGCM82633A123456")["outcome"] == "configuration_missing"
    assert offline._runtime("vininfo")[2] is None


def test_manifest_path_escape_or_symlink_rejected(runtime, tmp_path):
    root, manifest = runtime
    manifest["files"]["python-packages/../../escape"] = "0" * 64
    (root / "manifest.json").write_text(json.dumps(manifest))
    assert offline._runtime("vininfo")[2] == "configuration_missing"
    manifest["files"].pop("python-packages/../../escape")
    (root / "manifest.json").write_text(json.dumps(manifest))
    path = root / "python-packages/vininfo/__init__.py"
    path.unlink()
    path.symlink_to(root / "corgi-browser.mjs")
    assert offline._runtime("vininfo")[2] == "configuration_missing"


def test_malformed_manifest_is_configuration_failure(runtime):
    root, _ = runtime
    (root / "manifest.json").write_text("[]")
    assert offline._runtime("corgi")[2] == "configuration_missing"


def test_vininfo_retains_year_alternatives_binding_and_field_provenance(runtime):
    payload = {"vehicle_profile": {"manufacturer": "AvtoVAZ", "model": "Vesta"}, "model_year_candidates": [2018, 1988]}
    with patch.object(
        offline.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), "")
    ) as call:
        row = offline.vininfo_decode("XTAGFK330JY144213")
    assert row["data"]["model_year_candidates"] == [2018, 1988]
    assert "model_year" in row["missing_fields"]
    assert row["data"]["input_binding"] == binding("XTAGFK330JY144213", "vin")
    command = call.call_args.args[0]
    assert "XTAGFK330JY144213" not in command  # VIN travels on stdin, not process argv.
    assert call.call_args.kwargs["env"].get("AUTOSTOP_PARTSAPI_KEY") is None
    assert "-I" in command and "-B" in command


def test_corgi_preserves_primary_lineage_and_bounded_explicit_model_year(runtime):
    payload = {"vehicle_profile": {"make": "Honda", "model": "Accord", "model_year": 2003}, "decoder_errors": []}
    with patch.object(
        offline.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), "")
    ) as call:
        row = offline.corgi_decode("1HGCM82633A123456", model_year=2003, timeout_seconds=3)
    assert row["evidence"][0]["primary_lineage"] == "nhtsa_vpic"
    assert row["data"]["field_provenance"]["model_year"] == "explicit_model_year"
    assert row["execution"]["network_calls"] == 0
    assert call.call_args.kwargs["timeout"] == 3
    assert json.loads(call.call_args.kwargs["input"])["model_year"] == 2003
    assert "--permission" in call.call_args.args[0]
    assert "--allow-child-process" not in call.call_args.args[0]


@pytest.mark.parametrize(
    "keyword,value",
    [
        ("model_year", True),
        ("model_year", "2003"),
        ("model_year", 1900),
        ("timeout_seconds", 0),
        ("timeout_seconds", float("nan")),
        ("timeout_seconds", 31),
    ],
)
def test_corgi_invalid_options_do_not_load_runtime(keyword, value):
    with patch.object(offline, "_runtime", side_effect=AssertionError("invalid options")):
        assert offline.corgi_decode("1HGCM82633A123456", **{keyword: value})["outcome"] == "invalid_input"


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired([], 1), OSError("missing")])
def test_worker_failure_is_bounded_and_no_fallback(runtime, failure):
    with patch.object(offline.subprocess, "run", side_effect=failure) as call:
        row = offline.corgi_decode("1HGCM82633A123456")
    assert row["ok"] is False
    assert call.call_count == 1


@pytest.mark.parametrize(
    "output", ["", "[]", "invalid JSON", "x" * (offline.MAX_RESULT_BYTES + 1), '{"error":"database_missing"}']
)
def test_worker_output_errors_do_not_forge_success(runtime, output):
    with patch.object(offline.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output, "")):
        assert offline.vininfo_decode("XTAGFK330JY144213")["ok"] is False


def test_worker_corrupt_node_is_configuration_failure(runtime):
    root, manifest = runtime
    manifest["node"]["sha256"] = "0" * 64
    (root / "manifest.json").write_text(json.dumps(manifest))
    assert offline.corgi_decode("1HGCM82633A123456")["outcome"] == "configuration_missing"
