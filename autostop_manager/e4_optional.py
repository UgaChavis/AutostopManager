"""Optional local E4 adapters: decoding never installs packages or downloads data."""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
import gzip
import hashlib
from importlib import import_module, metadata
from importlib.resources import files
import json
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
import shutil
import sqlite3
import subprocess
import threading
import time
from typing import Any, BinaryIO

from .e4_identity import make_observation
from .vehicle_identity_inputs import validate_identity_input
from .vin_lookup import MAX_VPIC_RESPONSE_BYTES, classify_identifier

VININFO_VERSION = "1.11.0"
CORGI_VERSION = "2.0.4"
CORGI_COMPRESSED_SHA256 = "f20009d5fce3231647420f6d7e0ccdcac79eb4f4c90addb9c57961199b8039d7"
CORGI_DATABASE_SHA256 = "72f1470302a2a78a6824ae966c877b83ef947e983a7d407bc19a096bc17c0b2b"
CORGI_DATABASE_BYTES = 80355328
MAX_CHILD_RESPONSE_BYTES = MAX_VPIC_RESPONSE_BYTES
MAX_CHILD_STDERR_BYTES = 64 * 1024


class OptionalDecoderError(Exception):
    """Safe error code; upstream exception messages and identifiers are not retained."""


def _asset(name: str) -> Any:
    return files("autostop_manager").joinpath("resources", "e4", "corgi", name)


def _cache_directory(cache_dir: str | Path | None = None) -> Path:
    return Path(cache_dir or os.environ.get("AUTOSTOP_E4_CACHE_DIR") or ".local/e4").resolve()


def _paths(cache_dir: str | Path | None = None) -> tuple[Path, Path, Path]:
    root = _cache_directory(cache_dir) / f"corgi-{CORGI_VERSION}"
    return root / "runtime", root / "vpic.lite.db", root / "manifest.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_file(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise OptionalDecoderError("metadata_too_large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise OptionalDecoderError("metadata_invalid")
    return value


def _node_binary() -> str:
    binary = os.environ.get("AUTOSTOP_E4_NODE") or shutil.which("node")
    if not binary:
        raise OptionalDecoderError("node_missing")
    try:
        result = subprocess.run([binary, "--version"], capture_output=True, timeout=5, check=False)
        parts = result.stdout.decode("ascii").strip().lstrip("v").split(".")
        version = tuple(int(part) for part in parts)
        if result.returncode or len(version) != 3 or version[0] != 24 or version < (24, 21, 0):
            raise OptionalDecoderError("node_version_unsupported")
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise OptionalDecoderError("node_unavailable") from error
    return binary


def _runtime_installed(runtime: Path) -> bool:
    package = runtime / "node_modules" / "@cardog" / "corgi" / "package.json"
    try:
        return _json_file(package).get("version") == CORGI_VERSION
    except (OSError, ValueError, OptionalDecoderError):
        return False


def _checked_vin(identifier: str, model_year: int | None) -> tuple[str, int | None, str | None]:
    checked = validate_identity_input(identifier, model_year=model_year)
    if not checked["ok"]:
        return "", None, "invalid_input"
    classification = classify_identifier(checked["identifier"])
    if classification.kind != "vin":
        return classification.normalized, None, "invalid_input" if classification.kind == "unknown" else "unsupported"
    return classification.normalized, checked["context"].get("model_year"), None


def _put_candidates(fields: dict[str, Any], candidates: dict[str, list[Any]], field: str, value: Any) -> None:
    values = value if isinstance(value, (list, tuple)) else [value]
    filtered = []
    for item in values:
        if item is None or isinstance(item, bool):
            continue
        if isinstance(item, str):
            item = item.strip()
            if not item or item.casefold() in {"unknown", "unsupportedbrand", "not applicable", "n/a"}:
                continue
        if isinstance(item, (str, int, float)) and item not in filtered:
            filtered.append(item)
    if len(filtered) == 1:
        fields[field] = filtered[0]
    elif filtered:
        candidates[field] = filtered


def _load_vininfo() -> Any:
    try:
        if metadata.version("vininfo") != VININFO_VERSION:
            raise OptionalDecoderError("vininfo_version_unsupported")
        return import_module("vininfo")
    except (metadata.PackageNotFoundError, ImportError) as error:
        raise OptionalDecoderError("vininfo_missing") from error


def vininfo_decode(identifier: str, *, model_year: int | None = None) -> dict[str, Any]:
    """Decode using only the pinned optional Python library, retaining alternatives."""
    normalized, year, problem = _checked_vin(identifier, model_year)
    if problem:
        return make_observation("vininfo_decode", identifier, outcome=problem, version=VININFO_VERSION)
    try:
        module = _load_vininfo()
    except OptionalDecoderError as error:
        return make_observation(
            "vininfo_decode",
            normalized,
            outcome="dependency_missing",
            diagnostics={"reason": str(error)},
            version=VININFO_VERSION,
        )
    try:
        vin = module.Vin(normalized)
        fields: dict[str, Any] = {}
        candidates: dict[str, list[Any]] = {}
        _put_candidates(fields, candidates, "manufacturer", vin.manufacturer)
        _put_candidates(fields, candidates, "manufacturer_country", vin.country)
        _put_candidates(fields, candidates, "model_year", vin.years)
        if vin.brand.title != "UnsupportedBrand":
            _put_candidates(fields, candidates, "make", vin.brand.title)
        if vin.details is not None:
            for name, field in {
                "model": "model",
                "engine": "engine",
                "body": "body_class",
                "transmission": "transmission",
            }.items():
                # A code without a mapped name is not a decoded characteristic.
                _put_candidates(fields, candidates, field, getattr(vin.details, name).name)
        outcome = "partial" if fields or candidates else "not_found"
        return make_observation(
            "vininfo_decode",
            normalized,
            fields=fields,
            field_candidates=candidates,
            outcome=outcome,
            diagnostics={"offline": True, "details_supported": vin.details is not None},
            warnings=["local_rules_are_candidates", "model_year_is_not_production_year"],
            model_year=year,
            version=VININFO_VERSION,
        )
    except (module.VininfoException, TypeError, ValueError, AttributeError, IndexError):
        return make_observation(
            "vininfo_decode",
            normalized,
            outcome="provider_failed",
            diagnostics={"reason": "vininfo_decode_failed"},
            version=VININFO_VERSION,
        )


def _stream_reader(stream: BinaryIO, label: str, queue: Queue[tuple[str, bytes]], stop: threading.Event) -> None:
    """Read pipe chunks into a bounded queue, including on Windows."""
    while not stop.is_set():
        chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
        while not stop.is_set():
            try:
                queue.put((label, chunk), timeout=0.1)
                break
            except Full:
                continue
        if not chunk:
            return


def _collect_output(
    queue: Queue[tuple[str, bytes]],
    *,
    deadline: float,
    max_response_bytes: int,
    cancel_event: threading.Event | None,
) -> bytes:
    output = bytearray()
    stderr_bytes = 0
    active = {"stdout", "stderr"}
    while active:
        if cancel_event is not None and cancel_event.is_set():
            raise OptionalDecoderError("decoder_cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OptionalDecoderError("decoder_timeout")
        try:
            label, chunk = queue.get(timeout=min(remaining, 0.2))
        except Empty:
            continue
        if not chunk:
            active.discard(label)
        elif label == "stdout":
            output.extend(chunk)
            if len(output) > max_response_bytes:
                raise OptionalDecoderError("response_too_large")
        else:
            stderr_bytes += len(chunk)
            if stderr_bytes > MAX_CHILD_STDERR_BYTES:
                raise OptionalDecoderError("stderr_too_large")
    return bytes(output)


def _run_bounded(
    command: list[str],
    *,
    payload: dict[str, Any] | None = None,
    timeout_seconds: float = 30,
    cwd: Path | None = None,
    max_response_bytes: int = MAX_CHILD_RESPONSE_BYTES,
    cancel_event: threading.Event | None = None,
) -> tuple[int, bytes]:
    """Bound a subprocess while consuming stdout and stderr concurrently."""
    if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 600:
        raise OptionalDecoderError("timeout_invalid")
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)
    # Readers add at most eight 64 KiB chunks while the consumer enforces its cap.
    queue: Queue[tuple[str, bytes]] = Queue(maxsize=8)
    stop = threading.Event()
    readers: list[threading.Thread] = []
    deadline = time.monotonic() + timeout_seconds
    try:
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        for label, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            reader = threading.Thread(target=_stream_reader, args=(stream, label, queue, stop), daemon=True)
            readers.append(reader)
            reader.start()
        if payload is not None:
            process.stdin.write(json.dumps(payload, ensure_ascii=True).encode("utf-8"))
        process.stdin.close()
        output = _collect_output(
            queue, deadline=deadline, max_response_bytes=max_response_bytes, cancel_event=cancel_event
        )
        if cancel_event is not None and cancel_event.is_set():
            raise OptionalDecoderError("decoder_cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OptionalDecoderError("decoder_timeout")
        return process.wait(timeout=remaining), output
    except subprocess.TimeoutExpired as error:
        raise OptionalDecoderError("decoder_timeout") from error
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        stop.set()
        for reader in readers:
            reader.join(timeout=1)
        for child_stream in (process.stdin, process.stdout, process.stderr):
            if child_stream is not None:
                child_stream.close()


def _checked_snapshot(database: Path, manifest_path: Path) -> dict[str, Any]:
    if not database.is_file() or not manifest_path.is_file():
        raise OptionalDecoderError("snapshot_missing")
    try:
        manifest = _json_file(manifest_path)
        if (
            manifest.get("package_version") != CORGI_VERSION
            or manifest.get("compressed_sha256") != CORGI_COMPRESSED_SHA256
            or manifest.get("upstream_database_sha256") != CORGI_DATABASE_SHA256
            or _sha256(database) != manifest.get("database_sha256")
        ):
            raise OptionalDecoderError("snapshot_integrity_failed")
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise OptionalDecoderError("snapshot_invalid")
            tables = {
                row[0].casefold() for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not {"wmi", "pattern", "element", "wmi_vinschema", "vinschema"} <= tables:
                raise OptionalDecoderError("snapshot_schema_invalid")
        return manifest
    except (OSError, ValueError, sqlite3.Error) as error:
        raise OptionalDecoderError("snapshot_invalid") from error


def _corgi_candidates(result: dict[str, Any], model_year: int | None) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    """Preserve distinct pattern values rather than accepting an arbitrary winner."""
    fields: dict[str, Any] = {}
    candidates: dict[str, list[Any]] = {}
    values: dict[str, list[Any]] = {}
    names = {
        "Make": "make",
        "Model": "model",
        "Series": "series",
        "Series2": "series2",
        "Trim": "trim",
        "Engine Model": "engine",
        "Engine Number of Cylinders": "engine_cylinders",
        "Cylinders": "engine_cylinders",
        "Displacement (L)": "engine_displacement_l",
        "Engine Brake (hp) From": "engine_power_hp",
        "Engine Power (kW)": "engine_power_kw",
        "Fuel Type - Primary": "fuel_type",
        "Fuel Type": "fuel_type",
        "Transmission Style": "transmission",
        "Body Class": "body_class",
        "Drive Type": "drivetrain",
        "Plant Country": "plant_country",
        "Plant City": "plant_city",
        "Manufacturer Name": "manufacturer",
    }
    patterns = result.get("patterns") or []
    if not isinstance(patterns, list):
        raise OptionalDecoderError("corgi_payload_invalid")
    for pattern in patterns:
        if not isinstance(pattern, dict):
            raise OptionalDecoderError("corgi_payload_invalid")
        element = pattern.get("element")
        field = names.get(element) if isinstance(element, str) else None
        value = pattern.get("value")
        if field and value not in (None, ""):
            values.setdefault(field, []).append(value)
    components = result.get("components") or {}
    if not isinstance(components, dict):
        raise OptionalDecoderError("corgi_payload_invalid")
    wmi = components.get("wmi") or {}
    if isinstance(wmi, dict):
        for upstream, field in {
            "make": "make",
            "manufacturer": "manufacturer",
            "country": "manufacturer_country",
        }.items():
            if wmi.get(upstream) and field not in values:
                values[field] = [wmi[upstream]]
    year = components.get("modelYear") or {}
    if isinstance(year, dict) and year.get("year"):
        values["model_year"] = [year["year"]]
    for field, options in values.items():
        _put_candidates(fields, candidates, field, options)
    return fields, candidates


def corgi_decode(
    identifier: str,
    *,
    model_year: int | None = None,
    cache_dir: str | Path | None = None,
    database_path: str | Path | None = None,
    timeout_seconds: float = 30,
    _cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Call only the checked local Corgi snapshot; no download or decoder fallback."""
    normalized, year, problem = _checked_vin(identifier, model_year)
    if problem:
        return make_observation("corgi_decode", identifier, outcome=problem, version=CORGI_VERSION)
    if normalized[8] not in "0123456789X":
        return make_observation(
            "corgi_decode",
            normalized,
            outcome="unsupported",
            diagnostics={"reason": "library_unsupported_format"},
            version=CORGI_VERSION,
        )
    runtime, database, manifest_path = _paths(cache_dir)
    if database_path is not None:
        database = Path(database_path).resolve()
        manifest_path = database.with_name("manifest.json")
    try:
        node = _node_binary()
        if not _runtime_installed(runtime):
            raise OptionalDecoderError("corgi_missing")
        manifest = _checked_snapshot(database, manifest_path)
    except OptionalDecoderError as error:
        reason = str(error)
        outcome = (
            "database_missing"
            if reason == "snapshot_missing"
            else "database_invalid"
            if reason.startswith("snapshot_")
            else "dependency_missing"
        )
        return make_observation(
            "corgi_decode", normalized, outcome=outcome, diagnostics={"reason": reason}, version=CORGI_VERSION
        )
    try:
        payload = {
            "identifier": normalized,
            "model_year": year,
            "database_path": str(database),
            "runtime_dir": str(runtime),
        }
        # The helper is a packaged resource; the package itself lives in the explicit cache.
        helper = _asset("decode.mjs")
        returncode, raw = _run_bounded(
            [node, str(helper)], payload=payload, timeout_seconds=timeout_seconds, cancel_event=_cancel_event
        )
        response = json.loads(raw)
        if returncode or not isinstance(response, dict) or response.get("ok") is not True:
            raise OptionalDecoderError("corgi_decode_failed")
        result = response.get("result")
        if not isinstance(result, dict) or result.get("vin") != normalized:
            raise OptionalDecoderError("identifier_binding_mismatch")
        fields, candidates = _corgi_candidates(result, year)
        errors = result.get("errors") or []
        if not isinstance(errors, list) or any(not isinstance(item, dict) for item in errors):
            raise OptionalDecoderError("corgi_payload_invalid")
        blocking = any(item.get("severity") in {"error", "fatal"} for item in errors)
        clean = not errors and result.get("valid") is True
        warnings = ["local_vpic_snapshot", "model_year_is_not_production_year"]
        if normalized[0] not in "12345" and year is None:
            warnings.append("model_year_uses_north_american_heuristic")
            clean = False
        outcome = (
            "partial"
            if errors and (fields or candidates)
            else "not_found"
            if not fields and not candidates
            else "success"
        )
        if blocking:
            # Never promote fields when the library itself reports a blocking failure.
            outcome, clean = "partial" if fields or candidates else "not_found", False
        return make_observation(
            "corgi_decode",
            normalized,
            fields=fields,
            field_candidates=candidates,
            outcome=outcome,
            diagnostics={
                "offline": True,
                "provider_clean": clean,
                "identifier_verified": True,
                "provider_error_codes": [str(item.get("code", "")) for item in errors],
                "database_sha256": manifest["database_sha256"],
                "data_date": manifest.get("data_date"),
            },
            warnings=warnings,
            model_year=year,
            version=f"{CORGI_VERSION}:{manifest['database_sha256'][:12]}",
        )
    except (OptionalDecoderError, OSError, ValueError, TypeError) as error:
        reason = str(error) if isinstance(error, OptionalDecoderError) else "corgi_payload_invalid"
        outcome = (
            reason
            if reason in {"decoder_timeout", "response_too_large", "decoder_cancelled"}
            else "response_too_large"
            if reason == "stderr_too_large"
            else "provider_failed"
        )
        return make_observation(
            "corgi_decode", normalized, outcome=outcome, diagnostics={"reason": reason}, version=CORGI_VERSION
        )


def optional_status(*, cache_dir: str | Path | None = None) -> dict[str, Any]:
    """Content-free local availability; does not install, prepare or query a source."""
    try:
        _load_vininfo()
        vininfo = {"status": "ready", "version": VININFO_VERSION}
    except OptionalDecoderError as error:
        vininfo = {"status": "dependency_missing", "reason": str(error), "version": VININFO_VERSION}
    runtime, database, manifest_path = _paths(cache_dir)
    corgi: dict[str, Any] = {"version": CORGI_VERSION}
    try:
        _node_binary()
        if not _runtime_installed(runtime):
            raise OptionalDecoderError("corgi_missing")
        manifest = _checked_snapshot(database, manifest_path)
        corgi.update(status="ready", database_sha256=manifest["database_sha256"], data_date=manifest.get("data_date"))
    except OptionalDecoderError as error:
        reason = str(error)
        corgi.update(
            status="database_missing"
            if reason == "snapshot_missing"
            else "database_invalid"
            if reason.startswith("snapshot_")
            else "dependency_missing",
            reason=reason,
        )
    return {"ok": True, "vininfo": vininfo, "corgi": corgi, "network_used": False}


def _prepare_snapshot(compressed: Path, database: Path, manifest_path: Path) -> dict[str, Any]:
    """Prepare a pinned database outside Git, including its WAL-to-DELETE conversion."""
    if _sha256(compressed) != CORGI_COMPRESSED_SHA256:
        raise OptionalDecoderError("upstream_integrity_failed")
    temporary = database.with_suffix(".db.preparing")
    try:
        with gzip.open(compressed, "rb") as source, temporary.open("wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        if temporary.stat().st_size != CORGI_DATABASE_BYTES or _sha256(temporary) != CORGI_DATABASE_SHA256:
            raise OptionalDecoderError("upstream_integrity_failed")
        with closing(sqlite3.connect(temporary)) as connection:
            if connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise OptionalDecoderError("database_prepare_failed")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise OptionalDecoderError("database_corrupt")
        manifest = {
            "schema_version": 1,
            "package_version": CORGI_VERSION,
            "data_origin_id": "nhtsa_vpic",
            "source_url": "https://registry.npmjs.org/@cardog/corgi/-/corgi-2.0.4.tgz",
            "code_license": "ISC",
            "data_date": None,
            "compressed_sha256": CORGI_COMPRESSED_SHA256,
            "upstream_database_sha256": CORGI_DATABASE_SHA256,
            "database_sha256": _sha256(temporary),
            "database_bytes": temporary.stat().st_size,
            "prepared_at": datetime.now(UTC).isoformat(),
        }
        temporary.replace(database)
        manifest_tmp = manifest_path.with_suffix(".json.preparing")
        manifest_tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        manifest_tmp.replace(manifest_path)
        return manifest
    finally:
        temporary.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(temporary) + suffix).unlink(missing_ok=True)


def prepare_corgi(*, cache_dir: str | Path | None = None, timeout_seconds: float = 180) -> dict[str, Any]:
    """Explicit, network-capable preparation; no production services are altered."""
    try:
        _node_binary()
        npm = shutil.which("npm")
        if not npm:
            raise OptionalDecoderError("npm_missing")
        runtime, database, manifest_path = _paths(cache_dir)
        runtime.mkdir(parents=True, exist_ok=True)
        for name in ("package.json", "package-lock.json"):
            (runtime / name).write_bytes(_asset(name).read_bytes())
        returncode, _output = _run_bounded(
            [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=runtime, timeout_seconds=timeout_seconds
        )
        if returncode or not _runtime_installed(runtime):
            raise OptionalDecoderError("package_install_failed")
        compressed = runtime / "node_modules" / "@cardog" / "corgi" / "dist" / "db" / "vpic.lite.db.gz"
        manifest = _prepare_snapshot(compressed, database, manifest_path)
        return {
            "ok": True,
            "status": "ready",
            "version": CORGI_VERSION,
            "database_sha256": manifest["database_sha256"],
            "data_date": manifest["data_date"],
            "network_used": True,
        }
    except (OptionalDecoderError, OSError, ValueError, sqlite3.Error) as error:
        reason = str(error) if isinstance(error, OptionalDecoderError) else "database_prepare_failed"
        return {"ok": False, "status": "prepare_failed", "reason": reason, "network_used": True}
