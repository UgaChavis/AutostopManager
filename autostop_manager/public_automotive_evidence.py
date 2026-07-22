from __future__ import annotations

import csv
from datetime import UTC, datetime
from io import BytesIO, StringIO
import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile

from .source_catalog import load_open_dataset_endpoints
from .vin_lookup import normalize_vin


NHTSA_RECALLS_API_URL = "https://api.nhtsa.gov/recalls/recallsByVehicle"
NHTSA_DATASETS_URL = "https://www.nhtsa.gov/nhtsa-datasets-and-apis"
NHTSA_RECALLS_URL = "https://www.nhtsa.gov/recalls"
MERCEDES_OPERATING_FLUIDS_URL = "https://operatingfluids.mercedes-benz.com/"
ZF_LUBRICANTS_URL = "https://aftermarket.zf.com/lubricants/"

_ALLOWED_HOSTS = frozenset({"api.nhtsa.gov", "static.nhtsa.gov"})
_MAX_HTTP_BYTES = 24 * 1024 * 1024
_MAX_TSB_TEXT_BYTES = 72 * 1024 * 1024
_MAX_SUMMARY_CHARS = 1_200


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _compact(value: Any, *, limit: int = _MAX_SUMMARY_CHARS) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _normalise(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _redact_identifier(value: str | None) -> str | None:
    normalized = normalize_vin(str(value or ""))
    if not normalized:
        return None
    if len(normalized) <= 6:
        return f"{normalized[:2]}***"
    return f"{normalized[:3]}***{normalized[-3:]}"


def _normalise_limit(value: int) -> int:
    return max(1, min(int(value or 10), 20))


def _parse_model_year(value: int | str | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    if 1886 <= year <= datetime.now(UTC).year + 1:
        return year
    return None


def _safe_get_bytes(url: str, *, timeout: float, max_bytes: int = _MAX_HTTP_BYTES) -> bytes:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
        raise ValueError("public automotive evidence URL is outside the allowlist")
    request = Request(url, headers={"User-Agent": "AutostopManager/1.0", "Accept": "application/json,text/csv,*/*"})
    with urlopen(request, timeout=timeout) as response:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(min(64 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("public automotive evidence response exceeds the size limit")
            chunks.append(chunk)
    return b"".join(chunks)


def _nhtsa_recall_record(row: dict[str, Any]) -> dict[str, str]:
    return {
        "campaign_number": _compact(row.get("NHTSACampaignNumber"), limit=40),
        "manufacturer": _compact(row.get("Manufacturer"), limit=240),
        "report_received_date": _compact(row.get("ReportReceivedDate"), limit=32),
        "component": _compact(row.get("Component"), limit=320),
        "summary": _compact(row.get("Summary")),
        "consequence": _compact(row.get("Consequence")),
        "remedy": _compact(row.get("Remedy")),
    }


def lookup_nhtsa_recalls(
    *,
    make: str | None,
    model: str | None,
    model_year: int | str | None,
    limit: int = 10,
    timeout: float = 12.0,
) -> dict[str, Any]:
    """Return public NHTSA model-level recall evidence without VIN lookup."""
    year = _parse_model_year(model_year)
    make_text = _compact(make, limit=128)
    model_text = _compact(model, limit=128)
    if not (make_text and model_text and year):
        return {
            "ok": True,
            "source_id": "nhtsa_datasets_apis",
            "source_url": NHTSA_DATASETS_URL,
            "scope": "US model-level",
            "records": [],
            "missing_context": ["make", "model", "model_year"],
            "warnings": [
                "NHTSA recall lookup needs make, model, and model year; it is not a VIN campaign-status check."
            ],
        }

    request_url = f"{NHTSA_RECALLS_API_URL}?{urlencode({'make': make_text, 'model': model_text, 'modelYear': year})}"
    checked_at = _utc_now()
    try:
        payload = json.loads(_safe_get_bytes(request_url, timeout=timeout).decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "source_id": "nhtsa_datasets_apis",
            "source_url": NHTSA_DATASETS_URL,
            "request_url": request_url,
            "scope": "US model-level",
            "records": [],
            "checked_at": checked_at,
            "error": str(exc),
        }

    source_rows = payload.get("results") if isinstance(payload, dict) else []
    if not isinstance(source_rows, list):
        source_rows = []
    records = [_nhtsa_recall_record(row) for row in source_rows if isinstance(row, dict)][: _normalise_limit(limit)]
    return {
        "ok": True,
        "source_id": "nhtsa_datasets_apis",
        "source_url": NHTSA_DATASETS_URL,
        "request_url": request_url,
        "scope": "US model-level",
        "records": records,
        "record_count": int(payload.get("Count") or len(source_rows))
        if isinstance(payload, dict)
        else len(source_rows),
        "checked_at": checked_at,
        "warnings": [
            "NHTSA results describe U.S. model-level campaigns and do not confirm whether a specific VIN has an open recall."
        ],
        "recall_status_url": NHTSA_RECALLS_URL,
    }


def _nhtsa_tsb_dataset_url() -> str | None:
    endpoints = load_open_dataset_endpoints().get("endpoints", [])
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        source_id = str(endpoint.get("source_id") or endpoint.get("id") or "")
        url = str(endpoint.get("url") or "")
        if source_id.startswith("nhtsa_tsbs_") and url.endswith(".zip"):
            return url
    return None


def _tsb_value(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return _compact(value)
    return ""


def _matches_tsb_row(row: dict[str, Any], *, make: str, model: str, model_year: int) -> bool:
    row_make = _normalise(_tsb_value(row, "Make", "MAKETXT"))
    row_model = _normalise(_tsb_value(row, "Model", "MODELTXT"))
    row_year = _normalise(_tsb_value(row, "Model Year", "YEARTXT"))
    return row_make == _normalise(make) and row_model == _normalise(model) and row_year == str(model_year)


def _read_tsb_rows(payload: bytes) -> list[dict[str, str]]:
    with ZipFile(BytesIO(payload)) as archive:
        candidates = [member for member in archive.infolist() if member.filename.casefold().endswith(".csv")]
        if not candidates:
            candidates = [member for member in archive.infolist() if member.filename.casefold().endswith(".txt")]
        if not candidates:
            raise ValueError("NHTSA manufacturer-communications ZIP contains no CSV or TXT table")
        member = min(candidates, key=lambda item: (item.file_size, item.filename.casefold()))
        if member.file_size > _MAX_TSB_TEXT_BYTES:
            raise ValueError("NHTSA manufacturer-communications table exceeds the uncompressed size limit")
        with archive.open(member) as source:
            text_bytes = source.read(_MAX_TSB_TEXT_BYTES + 1)
    if len(text_bytes) > _MAX_TSB_TEXT_BYTES:
        raise ValueError("NHTSA manufacturer-communications table exceeds the read limit")
    text = text_bytes.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
    return [dict(row) for row in csv.DictReader(StringIO(text), delimiter=delimiter) if isinstance(row, dict)]


def _nhtsa_tsb_record(row: dict[str, Any]) -> dict[str, str]:
    return {
        "nhtsa_id": _tsb_value(row, "NHTSA ID Number", "ID"),
        "document_id": _tsb_value(row, "TSB/Document ID", "BULNO"),
        "communication_type": _tsb_value(row, "Communication Type"),
        "communication_date": _tsb_value(row, "Mfr Communication Date", "BULDTE"),
        "component": _tsb_value(row, "NHTSA Components", "COMPNAME"),
        "summary": _tsb_value(row, "Summary", "SUMMARY")[:_MAX_SUMMARY_CHARS],
    }


def lookup_nhtsa_tsb_metadata(
    *,
    make: str | None,
    model: str | None,
    model_year: int | str | None,
    limit: int = 10,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Return compact metadata from NHTSA's public manufacturer-communications ZIP."""
    year = _parse_model_year(model_year)
    make_text = _compact(make, limit=128)
    model_text = _compact(model, limit=128)
    source_url = _nhtsa_tsb_dataset_url() or NHTSA_DATASETS_URL
    if not (make_text and model_text and year):
        return {
            "ok": True,
            "source_id": "nhtsa_tsbs_recent_zips",
            "source_url": source_url,
            "scope": "US model-level metadata",
            "records": [],
            "missing_context": ["make", "model", "model_year"],
            "warnings": ["NHTSA TSB metadata lookup needs make, model, and model year."],
        }
    if not source_url.endswith(".zip"):
        return {
            "ok": False,
            "source_id": "nhtsa_tsbs_recent_zips",
            "source_url": source_url,
            "scope": "US model-level metadata",
            "records": [],
            "error": "No current NHTSA manufacturer-communications ZIP is configured.",
        }

    checked_at = _utc_now()
    try:
        rows = _read_tsb_rows(_safe_get_bytes(source_url, timeout=timeout))
    except (HTTPError, URLError, TimeoutError, ValueError, UnicodeError, BadZipFile, OSError) as exc:
        return {
            "ok": False,
            "source_id": "nhtsa_tsbs_recent_zips",
            "source_url": source_url,
            "scope": "US model-level metadata",
            "records": [],
            "checked_at": checked_at,
            "error": str(exc),
        }

    records: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not _matches_tsb_row(row, make=make_text, model=model_text, model_year=year):
            continue
        record = _nhtsa_tsb_record(row)
        key = (record["nhtsa_id"], record["document_id"])
        if key in seen:
            continue
        seen.add(key)
        records.append(record)
        if len(records) >= _normalise_limit(limit):
            break
    return {
        "ok": True,
        "source_id": "nhtsa_tsbs_recent_zips",
        "source_url": source_url,
        "scope": "US model-level metadata",
        "records": records,
        "checked_at": checked_at,
        "warnings": [
            "Manufacturer-communications records are navigation evidence, not a VIN-specific repair instruction or proof of an open campaign."
        ],
    }


def official_fluid_reference_routes(*, make: str | None, system: str | None = None) -> list[dict[str, str]]:
    make_key = _normalise(make)
    system_key = _normalise(system)
    routes: list[dict[str, str]] = []
    if any(token in make_key for token in ("mercedes", "maybach", "smart")):
        routes.append(
            {
                "source_id": "mercedes_operating_fluids",
                "name": "Mercedes-Benz Operating Fluids",
                "source_url": MERCEDES_OPERATING_FLUIDS_URL,
                "scope": "official approval and product-list reference",
                "applicability_limit": "Confirm the exact vehicle, engine or unit and market before treating an approval as applicable.",
            }
        )
    if any(
        token in system_key for token in ("transmission", "gearbox", "automatic", "differential", "axle", "steering")
    ):
        routes.append(
            {
                "source_id": "ZF_TE_ML_11",
                "name": "ZF Aftermarket Lubricants / TE-ML",
                "source_url": ZF_LUBRICANTS_URL,
                "scope": "official ZF-unit lubricant and interval reference",
                "applicability_limit": "Use only after the exact ZF unit or transmission code is confirmed; a vehicle make alone is insufficient.",
            }
        )
    return routes


def _normalise_topics(value: str | list[str] | None) -> set[str]:
    raw_values = [value] if isinstance(value, str) else list(value or [])
    aliases = {
        "recall": "recalls",
        "recalls": "recalls",
        "отзыв": "recalls",
        "отзывы": "recalls",
        "tsb": "tsb",
        "technicalservicebulletin": "tsb",
        "technicalservicebulletins": "tsb",
        "manufacturercommunications": "tsb",
        "бюллетень": "tsb",
        "бюллетени": "tsb",
        "fluids": "fluids",
        "fluid": "fluids",
        "oil": "fluids",
        "масло": "fluids",
        "жидкости": "fluids",
    }
    topics = {aliases.get(_normalise(item), _normalise(item)) for item in raw_values if _normalise(item)}
    return topics or {"recalls", "fluids"}


def lookup_public_automotive_evidence(
    *,
    vin: str | None = None,
    make: str | None = None,
    model: str | None = None,
    model_year: int | str | None = None,
    topics: str | list[str] | None = None,
    system: str | None = None,
    include_tsb: bool = False,
    limit: int = 10,
    timeout: float = 12.0,
) -> dict[str, Any]:
    """Collect legal public technical evidence without writing or persisting raw exports."""
    bounded_limit = _normalise_limit(limit)
    selected_topics = _normalise_topics(topics)
    evidence: list[dict[str, Any]] = []
    warnings: list[str] = []
    missing_context: list[str] = []

    if "recalls" in selected_topics:
        recall = lookup_nhtsa_recalls(
            make=make,
            model=model,
            model_year=model_year,
            limit=bounded_limit,
            timeout=timeout,
        )
        evidence.append(recall)
        warnings.extend(str(item) for item in recall.get("warnings", []) if item)
        missing_context.extend(str(item) for item in recall.get("missing_context", []) if item)

    if include_tsb or "tsb" in selected_topics:
        tsb = lookup_nhtsa_tsb_metadata(
            make=make,
            model=model,
            model_year=model_year,
            limit=bounded_limit,
            timeout=max(timeout, 20.0),
        )
        evidence.append(tsb)
        warnings.extend(str(item) for item in tsb.get("warnings", []) if item)
        missing_context.extend(str(item) for item in tsb.get("missing_context", []) if item)

    fluid_routes = official_fluid_reference_routes(make=make, system=system) if "fluids" in selected_topics else []
    if fluid_routes:
        evidence.append(
            {
                "source_id": "official_fluid_reference_routes",
                "scope": "official public reference routes",
                "records": fluid_routes,
                "checked_at": _utc_now(),
                "warnings": [
                    "Official approval lists do not replace VIN/unit-specific capacity, fill procedure, level temperature, or repair documentation."
                ],
            }
        )
        warnings.extend(evidence[-1]["warnings"])

    if not evidence:
        warnings.append(
            "No supported public-evidence topic was selected; use source routing or public web research for the exact question."
        )

    failed = [item for item in evidence if item.get("ok") is False]
    return {
        "ok": not failed,
        "input_context": {
            "vin": _redact_identifier(vin),
            "make": _compact(make, limit=128) or None,
            "model": _compact(model, limit=128) or None,
            "model_year": _parse_model_year(model_year),
            "system": _compact(system, limit=128) or None,
            "topics": sorted(selected_topics),
        },
        "evidence": evidence,
        "warnings": list(dict.fromkeys(warnings)),
        "missing_context": list(dict.fromkeys(missing_context)),
        "confidence": "medium" if evidence and not failed else "low",
        "rules": [
            "Public recalls and manufacturer communications are source-backed signals, not a VIN-specific repair authorization.",
            "Do not infer exact torque, timing marks, fluid capacity, or repair procedure from public evidence alone.",
            "Use OEM or licensed service documentation for safety-critical and exact configuration-dependent work.",
        ],
    }
