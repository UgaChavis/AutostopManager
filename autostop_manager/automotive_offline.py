"""Independent local automotive decoders; preparation never occurs during decode."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from .automotive_contracts import binding, invalid, result
from .automotive_local_registry import PLATFORM_RULES, PRIMARY_LINEAGE, REGISTRY_VERSION, WMI_HINTS

VIN_PATTERN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
WMI_PATTERN = re.compile(r"^[A-HJ-NPR-Z0-9]{3}$")
RUNTIME_SCHEMA = "autostop.automotive-offline-runtime.v1"
RUNTIME_DEFAULT = "/var/lib/autostop-automotive-offline/current"
MAX_MANIFEST_BYTES = 200_000
MAX_RESULT_BYTES = 100_000


def _normalize(value: Any, pattern: re.Pattern[str]) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    return normalized if pattern.fullmatch(normalized) else None


def _local_evidence(method: str) -> dict[str, Any]:
    return {
        "provider": "autostop_local",
        "primary_lineage": PRIMARY_LINEAGE,
        "method": method,
        "version": REGISTRY_VERSION,
        "scope": "family_hint",
        "locator": "autostop_manager/automotive_local_registry.py",
    }


def decode_wmi_local(wmi: str) -> dict[str, Any]:
    tool_id = "decode_wmi_local"
    normalized = _normalize(wmi, WMI_PATTERN)
    if normalized is None:
        return invalid(tool_id, "wmi")
    hints = WMI_HINTS.get(normalized)
    if hints is None:
        return result(
            tool_id,
            "unsupported",
            {"wmi": normalized, "hints": {}, "vehicle_profile": {}},
            missing_fields=["manufacturer", "model", "engine", "transmission", "market"],
        )
    # A WMI describes an assembler. Market/type ranges remain hints, never VIN facts.
    profile = {key: hints[key] for key in ("make", "manufacturer", "country") if key in hints}
    return result(
        tool_id,
        "partial",
        {
            "wmi": normalized,
            "hints": dict(hints),
            "vehicle_profile": profile,
            "field_provenance": dict.fromkeys(profile, "local_wmi_hint"),
        },
        evidence=[_local_evidence(tool_id)],
        missing_fields=["model", "engine", "transmission", "production_date", "market"],
        warnings=["wmi_does_not_identify_model_or_factory_configuration"],
    )


def _rule_data(identifier: str, kind: str, *, vag_only: bool = False) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for rule in PLATFORM_RULES:
        if rule.kind != kind or not rule.matches(identifier):
            continue
        if vag_only and rule.fields.get("make") not in {"Audi", "Volkswagen", "Skoda"}:
            continue
        fields = dict(rule.fields)
        if "model" in fields:
            fields["model_family"] = fields.pop("model")
        # Existing engine/market assertions remain candidate hints, not exact fields.
        family = {key: fields[key] for key in ("make", "model_family", "platform") if key in fields}
        candidates.append(
            {
                "rule_id": rule.rule_id,
                "vehicle_profile": family,
                "hints": fields,
                "scope": "family_hint",
                "evidence_note": rule.evidence,
            }
        )
    profile: dict[str, Any] = dict(candidates[0]["vehicle_profile"]) if len(candidates) == 1 else {}
    return {
        "input_binding": binding(identifier, "frame_number" if kind == "jdm_frame" else "vin"),
        "vehicle_profile": profile,
        "candidates": candidates,
        "registry_version": REGISTRY_VERSION,
        "field_provenance": dict.fromkeys(profile, "local_family_rule"),
    }


def decode_frame_local(identifier: str) -> dict[str, Any]:
    tool_id = "decode_frame_local"
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Z0-9]{2,10}-?[0-9]{5,8}", identifier.strip().upper()):
        return invalid(tool_id, "identifier")
    normalized = identifier.strip().upper()
    if VIN_PATTERN.fullmatch(normalized):
        return invalid(tool_id, "identifier_kind_frame")
    data = _rule_data(normalized, "jdm_frame")
    return result(
        tool_id,
        "partial" if data["candidates"] else "unsupported",
        data,
        evidence=[_local_evidence(tool_id)] if data["candidates"] else [],
        missing_fields=["modification", "engine", "transmission", "production_date"],
        warnings=["limited_frame_registry_no_factory_options"],
    )


def vin_brand_details(identifier: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    tool_id = "vin_brand_details"
    normalized = _normalize(identifier, VIN_PATTERN)
    if normalized is None or (context is not None and not isinstance(context, dict)):
        return invalid(tool_id, "identifier" if normalized is None else "context")
    data = _rule_data(normalized, "vin_prefix", vag_only=True)
    conflicts = []
    if context:
        from .vehicle_identity import identity_values_agree

        for field in ("make", "model", "platform"):
            candidate = data["vehicle_profile"].get("model_family" if field == "model" else field)
            if context.get(field) and candidate and not identity_values_agree(field, context[field], candidate):
                conflicts.append({"field": field, "code": "local_family_context_conflict"})
    return result(
        tool_id,
        "partial" if data["candidates"] else "unsupported",
        data,
        evidence=[_local_evidence(tool_id)] if data["candidates"] else [],
        conflicts=conflicts,
        missing_fields=["modification", "engine", "transmission", "production_date", "market"],
        warnings=["limited_vag_family_rules_not_factory_build_sheet"],
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1_048_576), b""):
            digest.update(block)
    return digest.hexdigest()


def _runtime(provider: str) -> tuple[Path | None, dict[str, Any], str | None]:
    configured = os.environ.get("AUTOSTOP_AUTOMOTIVE_OFFLINE_RUNTIME", RUNTIME_DEFAULT)
    try:
        root = Path(configured).resolve()
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            return None, {}, "dependency_missing"
    except (OSError, RuntimeError, ValueError):
        return None, {}, "configuration_missing"
    try:
        if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            return None, {}, "configuration_missing"
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("schema") != RUNTIME_SCHEMA or not isinstance(manifest.get("files"), dict):
            return None, {}, "configuration_missing"
        metadata = manifest.get(provider)
        if not isinstance(metadata, dict) or not isinstance(metadata.get("version"), str):
            return None, {}, "configuration_missing"
        files = manifest["files"]
        if provider == "vininfo":
            files = {name: digest for name, digest in files.items() if name.startswith("python-packages/")}
            if "python-packages/vininfo/__init__.py" not in files:
                return None, {}, "dependency_missing"
        else:
            files = {name: digest for name, digest in files.items() if name in {"corgi-browser.mjs", "vpic.lite.db"}}
            if "vpic.lite.db" not in files:
                return None, {}, "database_missing"
            if "corgi-browser.mjs" not in files:
                return None, {}, "dependency_missing"
        for relative, expected in files.items():
            if not isinstance(relative, str) or not isinstance(expected, str):
                return None, {}, "configuration_missing"
            path = root / relative
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                return None, {}, "configuration_missing"
            if not path.is_file():
                outcome = "database_missing" if relative == "vpic.lite.db" else "dependency_missing"
                return None, {}, outcome
            if _sha256(path) != expected:
                return None, {}, "configuration_missing"
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        return None, {}, "configuration_missing"
    return root, manifest, None


def _worker(command: list[str], payload: dict[str, Any], timeout: float) -> tuple[dict[str, Any], str | None]:
    # No shell, inherited provider credentials, online fallback, or decoder switching.
    env = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1", "LOG_LEVEL": "fatal", "NODE_NO_WARNINGS": "1"}
    try:
        completed = subprocess.run(
            command, input=json.dumps(payload), capture_output=True, text=True, timeout=timeout, env=env, check=False
        )
    except subprocess.TimeoutExpired:
        return {}, "decoder_timeout"
    except OSError:
        return {}, "dependency_missing"
    if len(completed.stdout) > MAX_RESULT_BYTES:
        return {}, "parse_error"
    try:
        decoded = json.loads(completed.stdout)
    except (ValueError, TypeError):
        return {}, "parse_error"
    if not isinstance(decoded, dict):
        return {}, "parse_error"
    reported_error = decoded.get("error")
    if reported_error is not None and not isinstance(reported_error, str):
        return {}, "parse_error"
    if completed.returncode != 0 or reported_error:
        return {}, reported_error or "parse_error"
    if not isinstance(decoded.get("vehicle_profile"), dict):
        return {}, "parse_error"
    return decoded, None


def _decoder_error(tool_id: str, reason: str) -> dict[str, Any]:
    outcome = reason if reason in {"dependency_missing", "database_missing", "configuration_missing"} else "parse_error"
    return result(tool_id, outcome, {}, warnings=[reason])


def _offline_evidence(provider: str, manifest: dict[str, Any], identifier: str) -> dict[str, Any]:
    metadata = manifest.get(provider, {})
    return {
        "provider": provider,
        "primary_lineage": "nhtsa_vpic" if provider == "corgi" else "vininfo_local_rules",
        "method": "offline_decode",
        "version": metadata.get("version"),
        "locator": metadata.get("source_url"),
        "scope": "decoded_fields",
        "input_binding": binding(identifier, "vin"),
        "database_sha256": manifest.get("files", {}).get("vpic.lite.db") if provider == "corgi" else None,
    }


def vininfo_decode(identifier: str) -> dict[str, Any]:
    tool_id = "vininfo_decode"
    normalized = _normalize(identifier, VIN_PATTERN)
    if normalized is None:
        return invalid(tool_id, "identifier")
    root, manifest, error = _runtime("vininfo")
    if error or root is None:
        return result(tool_id, error or "dependency_missing", {}, missing_fields=["prepared_offline_runtime"])
    worker = Path(__file__).resolve().parent.parent / "scripts" / "automotive-vininfo-worker.py"
    decoded, error = _worker([sys.executable, "-I", "-B", str(worker), str(root)], {"identifier": normalized}, 10)
    if error:
        return _decoder_error(tool_id, error)
    profile = decoded.get("vehicle_profile", {})
    outcome = "partial" if profile.get("manufacturer") or profile.get("model") else "unsupported"
    data = {
        **decoded,
        "input_binding": binding(normalized, "vin"),
        "field_provenance": dict.fromkeys(profile, "vininfo_local_rules"),
    }
    return result(
        tool_id,
        outcome,
        data,
        evidence=[_offline_evidence("vininfo", manifest, normalized)],
        missing_fields=[
            key
            for key in ("model", "model_year", "engine", "transmission", "production_date", "market")
            if profile.get(key) in (None, "")
        ],
        warnings=["local_decode_not_factory_configuration"],
    )


def corgi_decode(identifier: str, model_year: int | None = None, timeout_seconds: float = 10) -> dict[str, Any]:
    tool_id = "corgi_decode"
    normalized = _normalize(identifier, VIN_PATTERN)
    if normalized is None:
        return invalid(tool_id, "identifier")
    if model_year is not None and (
        isinstance(model_year, bool) or not isinstance(model_year, int) or not 1980 <= model_year <= 2100
    ):
        return invalid(tool_id, "model_year")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 < timeout_seconds <= 30
    ):
        return invalid(tool_id, "timeout_seconds")
    root, manifest, error = _runtime("corgi")
    if error or root is None:
        return result(tool_id, error or "dependency_missing", {}, missing_fields=["prepared_offline_runtime"])
    if not (root / "vpic.lite.db").is_file():
        return result(tool_id, "database_missing", {}, missing_fields=["vpic_database"])
    node_metadata = manifest.get("node", {})
    if not isinstance(node_metadata, dict):
        return result(tool_id, "configuration_missing", {}, warnings=["invalid_node_runtime_metadata"])
    node = node_metadata.get("executable")
    try:
        if not isinstance(node, str) or not Path(node).is_absolute() or not Path(node).is_file():
            return result(tool_id, "dependency_missing", {}, missing_fields=["node_runtime"])
        node_digest = _sha256(Path(node))
    except (OSError, RuntimeError, ValueError):
        return result(tool_id, "configuration_missing", {}, warnings=["node_runtime_unreadable"])
    if node_digest != node_metadata.get("sha256"):
        return result(tool_id, "configuration_missing", {}, warnings=["node_runtime_hash_mismatch"])
    worker = Path(__file__).resolve().parent.parent / "scripts" / "automotive-corgi-worker.mjs"
    decoded, error = _worker(
        [
            node,
            "--jitless",
            "--permission",
            f"--allow-fs-read={root}",
            f"--allow-fs-read={worker}",
            str(worker),
            str(root),
        ],
        {"identifier": normalized, "model_year": model_year},
        timeout_seconds,
    )
    if error:
        return _decoder_error(tool_id, error)
    profile = decoded.get("vehicle_profile", {})
    data = {
        **decoded,
        "input_binding": binding(normalized, "vin"),
        "field_provenance": dict.fromkeys(profile, "nhtsa_vpic_via_corgi"),
        "database": {
            "version": manifest.get("corgi", {}).get("database_version"),
            "sha256": manifest["files"].get("vpic.lite.db"),
        },
    }
    if model_year is not None and "model_year" in profile:
        data["field_provenance"]["model_year"] = "explicit_model_year"
    return result(
        tool_id,
        "partial" if profile.get("make") or profile.get("manufacturer") else "unsupported",
        data,
        evidence=[_offline_evidence("corgi", manifest, normalized)],
        missing_fields=[
            key
            for key in ("model", "engine", "transmission", "production_date", "market")
            if profile.get(key) in (None, "")
        ],
        warnings=["corgi_and_vpic_share_primary_lineage", "local_decode_not_factory_configuration"],
    )
