from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import json
import math
import re
from typing import Any, Literal, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .vin_sources import (
    AMAYAMA_SOURCE_ID,
    PARTSOUQ_SOURCE_ID,
    canonical_source_id,
    load_source_registry,
    normalize_make,
    sources_for_inputs,
    sources_for_make,
)

LookupKind = Literal["vin", "vin_partial", "frame_number", "market_code", "unknown"]
ConfidenceLevel = Literal["high", "medium", "low", "blocked"]

_VIN_ALLOWED = re.compile(r"^[A-HJ-NPR-Z0-9*]+$")
_FRAME_ALLOWED = re.compile(r"^[A-Z0-9][A-Z0-9\-]*$")
_PART_NUMBER_ALLOWED = re.compile(r"^[A-Z0-9][A-Z0-9./\-\s]{2,}$")

_VAG_MAKES = {"AUDI", "VOLKSWAGEN", "VW", "SKODA", "SEAT", "CUPRA", "VAG"}
_BMW_MAKES = {"BMW", "MINI"}

_STEERING_TERMS = (
    "рулевая рейка",
    "рейка",
    "steering rack",
    "eps rack",
    "servotronic",
)
_DSG_TERMS = (
    "мехатроник",
    "mechatronic",
    "dsg",
    "s-tronic",
    "s tronic",
    "dq200",
    "dq250",
    "dq381",
    "0b5",
    "0d9",
)
_ELECTRONICS_TERMS = (
    "блок",
    "module",
    "control unit",
    "ecu",
    "dme",
    "dde",
    "egs",
    "abs",
    "dsc",
    "j743",
)
_SIDE_DEPENDENT_TERMS = (
    "лев",
    "прав",
    "left",
    "right",
    "фара",
    "фонарь",
    "зеркал",
    "молдинг",
    "крыло",
    "двер",
    "рычаг",
    "стойка",
)


@dataclass(frozen=True)
class IdentifierClassification:
    raw: str
    normalized: str
    kind: LookupKind
    market_hint: str | None
    confidence: float
    notes: list[str]


@dataclass(frozen=True)
class LookupStep:
    source_id: str
    source_name: str
    kind: str
    authority: str
    url: str
    query: str
    notes: str
    brands: list[str] = field(default_factory=list)
    markets: list[str] = field(default_factory=list)
    accepts: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    access_mode: str = ""
    trust_level: str = ""
    requires_login: bool = False
    adapter_status: list[str] = field(default_factory=list)
    preferred_for: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)


def _compact_text(raw: str) -> str:
    return re.sub(r"\s+", "", raw.strip().upper())


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)] if str(value).strip() else []


def normalize_vin(raw: str) -> str:
    return re.sub(r"[\s\-]+", "", raw.strip().upper())


def normalize_frame_number(raw: str) -> str:
    return re.sub(r"\s+", "", raw.strip().upper())


def normalize_market_code(raw: str) -> str:
    return _compact_text(raw).replace("-", "")


def normalize_part_number(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", raw.strip().upper())


def _redact_identifier(identifier: str | None) -> dict[str, Any]:
    normalized = re.sub(r"[^A-Z0-9*]", "", str(identifier or "").upper())
    if not normalized:
        return {"display": "", "length": 0, "prefix": ""}
    if len(normalized) <= 6:
        return {"display": f"{normalized[:2]}***", "length": len(normalized), "prefix": normalized[:2]}
    return {"display": f"{normalized[:3]}***{normalized[-3:]}", "length": len(normalized), "prefix": normalized[:3]}


def _public_identifier(classification: IdentifierClassification) -> dict[str, Any]:
    return {
        "redacted": _redact_identifier(classification.normalized or classification.raw),
        "kind": classification.kind,
        "market_hint": classification.market_hint,
        "confidence": classification.confidence,
        "notes": list(classification.notes),
        "raw_identifier_is_sensitive": True,
    }


def _redact_identifier_text(value: str, identifier: str | None) -> str:
    normalized = re.sub(r"[^A-Z0-9*]", "", str(identifier or "").upper())
    if not normalized:
        return value
    display = _redact_identifier(normalized)["display"]
    redacted = value.replace(normalized, display)
    if "-" in value:
        parts = []
        for split_at in range(3, min(len(normalized), 7)):
            parts.append(f"{normalized[:split_at]}-{normalized[split_at:]}")
        for variant in parts:
            redacted = redacted.replace(variant, display)
    return redacted


def _redact_identifier_payload(value: Any, identifier: str | None) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if (
                str(key).casefold() in {"vin", "vehicledescriptor", "vehicle_descriptor"}
                and _redact_identifier(identifier)["display"]
            ):
                redacted[key] = _redact_identifier(identifier)["display"]
            else:
                redacted[key] = _redact_identifier_payload(item, identifier)
        return redacted
    if isinstance(value, list):
        return [_redact_identifier_payload(item, identifier) for item in value]
    if isinstance(value, str):
        return _redact_identifier_text(value, identifier)
    return value


def classify_identifier(raw: str, *, identifier_type: str = "auto") -> IdentifierClassification:
    if not isinstance(raw, str):
        return IdentifierClassification("", "", "unknown", None, 0.0, ["identifier must be text"])
    if not isinstance(identifier_type, str) or identifier_type not in {
        "auto",
        "vin",
        "vin_partial",
        "frame_number",
        "market_code",
    }:
        return IdentifierClassification(raw.strip(), "", "unknown", None, 0.0, ["invalid explicit identifier type"])
    original = raw.strip()
    compact = _compact_text(raw)
    notes: list[str] = []

    if identifier_type != "auto":
        candidate = normalize_vin(raw) if identifier_type in {"vin", "vin_partial"} else compact
        valid = {
            "vin": len(candidate) == 17 and "*" not in candidate and bool(_VIN_ALLOWED.fullmatch(candidate)),
            "vin_partial": 8 <= len(candidate) <= 17 and bool(_VIN_ALLOWED.fullmatch(candidate)),
            "frame_number": bool(_FRAME_ALLOWED.fullmatch(candidate)),
            "market_code": bool(re.fullmatch(r"[A-Z0-9-]{4,32}", candidate)),
        }.get(identifier_type, False)
        if not valid:
            return IdentifierClassification(
                original, compact, "unknown", None, 0.0, ["invalid explicit identifier type"]
            )
        return IdentifierClassification(
            original,
            candidate,
            cast(LookupKind, identifier_type),
            "global" if identifier_type.startswith("vin") else None,
            0.99 if identifier_type == "vin" else 0.82,
            ["explicit identifier type"],
        )

    if not compact:
        return IdentifierClassification(
            raw=raw,
            normalized="",
            kind="unknown",
            market_hint=None,
            confidence=0.0,
            notes=["empty identifier"],
        )

    vin_candidate = normalize_vin(raw)
    if "-" not in compact and _VIN_ALLOWED.fullmatch(vin_candidate):
        if len(vin_candidate) == 17 and "*" not in vin_candidate:
            return IdentifierClassification(
                raw=original,
                normalized=vin_candidate,
                kind="vin",
                market_hint="global",
                confidence=0.99,
                notes=["standard 17-character VIN"],
            )
        if 8 <= len(vin_candidate) <= 17 and "*" in vin_candidate:
            return IdentifierClassification(
                raw=original,
                normalized=vin_candidate,
                kind="vin_partial",
                market_hint="global",
                confidence=0.82,
                notes=["partial VIN with wildcard"],
            )

    if "-" in compact and _FRAME_ALLOWED.fullmatch(compact):
        market_hint = "japan"
        notes.append("hyphenated chassis/frame number")
        return IdentifierClassification(
            raw=original,
            normalized=normalize_frame_number(raw),
            kind="frame_number",
            market_hint=market_hint,
            confidence=0.9,
            notes=notes,
        )

    alnum = normalize_market_code(raw)
    if len(alnum) >= 4 and len(alnum) <= 16 and any(ch.isalpha() for ch in alnum) and any(ch.isdigit() for ch in alnum):
        return IdentifierClassification(
            raw=original,
            normalized=alnum,
            kind="market_code",
            market_hint=None,
            confidence=0.65,
            notes=["market-specific code"],
        )

    return IdentifierClassification(
        raw=original,
        normalized=compact,
        kind="unknown",
        market_hint=None,
        confidence=0.2,
        notes=["could not classify safely"],
    )


_VPIC_FIELDS = (
    "VIN",
    "VehicleDescriptor",
    "Make",
    "Manufacturer",
    "ManufacturerName",
    "Model",
    "ModelYear",
    "Trim",
    "Series",
    "Series2",
    "BodyClass",
    "VehicleType",
    "PlantCountry",
    "PlantCity",
    "PlantCompanyName",
    "PlantState",
    "EngineModel",
    "EngineConfiguration",
    "EngineCylinders",
    "DisplacementL",
    "DisplacementCC",
    "EngineHP",
    "FuelTypePrimary",
    "FuelTypeSecondary",
    "Turbo",
    "TransmissionStyle",
    "TransmissionSpeeds",
    "DriveType",
    "Doors",
    "Seats",
    "GVWR",
)
_VPIC_NUMERIC_FIELDS = {
    "EngineCylinders",
    "DisplacementL",
    "DisplacementCC",
    "EngineHP",
    "TransmissionSpeeds",
    "Doors",
    "Seats",
}
_VPIC_BASE_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/"
MAX_VPIC_RESPONSE_BYTES = 5 * 1024 * 1024


def _finite_number(value: int | float) -> bool:
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _validate_vpic_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("vPIC returned a non-object JSON payload")
    results = payload.get("Results")
    if not isinstance(results, list) or any(not isinstance(row, dict) for row in results):
        raise ValueError("vPIC returned a malformed Results payload")
    return payload


def _provider_year(value: Any) -> int:
    if isinstance(value, str) and re.fullmatch(r"[0-9]{4}", value.strip()):
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or not 1900 <= value <= datetime.now(UTC).year + 1:
        raise ValueError("vPIC returned a malformed ModelYear field")
    return value


def _extract_vpic_vehicle(result: dict[str, Any]) -> dict[str, Any]:
    vehicle: dict[str, Any] = {}
    for key in _VPIC_FIELDS:
        value = result.get(key)
        if value is None or value == "" or value == "Not Applicable":
            continue
        if key == "ModelYear":
            value = _provider_year(value)
        elif key in _VPIC_NUMERIC_FIELDS and isinstance(value, (int, float)) and not isinstance(value, bool):
            if not _finite_number(value):
                raise ValueError("vPIC returned a malformed numeric field")
            value = str(value)
        elif not isinstance(value, str):
            raise ValueError("vPIC returned a malformed vehicle field")
        vehicle[key.lower()] = value
    return vehicle


def _vpic_request_json(request_url: str, *, timeout: float, data: bytes | None = None) -> dict[str, Any]:
    request = Request(request_url, data=data, headers={"User-Agent": "AutostopManager/0.1"})
    if data is not None:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_VPIC_RESPONSE_BYTES + 1)
    if len(raw) > MAX_VPIC_RESPONSE_BYTES:
        raise ValueError("vPIC response exceeds the response size limit")
    return _validate_vpic_payload(json.loads(raw.decode("utf-8")))


def _vpic_failure_details(exc: BaseException) -> tuple[str, bool]:
    if isinstance(exc, HTTPError):
        if int(exc.code) == 429:
            return "provider_throttled", True
        return ("provider_http_5xx", True) if 500 <= int(exc.code) <= 599 else ("provider_http_4xx", False)
    if isinstance(exc, TimeoutError):
        return "timeout", True
    if isinstance(exc, (URLError, OSError)):
        return "network_error", True
    if isinstance(exc, ValueError):
        return "adapter_malformed_payload", False
    return "adapter_error", False


def _provider_failure(
    outcome: str, *, source: str = "NHTSA vPIC", retryable: bool = False, error: str | None = None
) -> dict[str, Any]:
    # Never expose exception strings: HTTP/transport exceptions commonly include the request VIN URL.
    return {
        "ok": False,
        "source": source,
        "outcome": outcome,
        "error": error or outcome,
        "retryable": retryable,
        "requires_fallback": outcome not in {"invalid_input", "identity_mismatch"},
        "vehicle": {},
        "epc_confirmed": False,
    }


def _identifier_binding(expected: str, echo: Any, *, partial: bool = False) -> dict[str, Any]:
    if not isinstance(echo, str) or not echo.strip():
        return {"status": "missing", "verified": False}
    returned = normalize_vin(echo)
    if not 8 <= len(returned) <= 17 or not _VIN_ALLOWED.fullmatch(returned):
        return {"status": "mismatch", "verified": False}
    if not partial:
        matches = returned == expected
        return {"status": "exact" if matches else "mismatch", "verified": matches}
    # Every known input position must be echoed; an asterisk in the response cannot prove a known character.
    matches = len(returned) >= len(expected) and all(
        char == "*" or returned[index] == char for index, char in enumerate(expected)
    )
    return {"status": "compatible_partial" if matches else "mismatch", "verified": False}


def _parse_vpic_row(row: dict[str, Any], vin: str, *, source: str, partial: bool = False) -> dict[str, Any]:
    binding = _identifier_binding(vin, row.get("VIN"), partial=partial)
    if binding["status"] in {"missing", "mismatch"}:
        result = _provider_failure(
            "identity_unverified" if binding["status"] == "missing" else "identity_mismatch", source=source
        )
        result["identifier_binding"] = binding
        return result
    try:
        vehicle = _extract_vpic_vehicle(row)
        error_code = row.get("ErrorCode")
        error_text = row.get("ErrorText")
        if error_code is not None and (isinstance(error_code, bool) or not isinstance(error_code, (str, int))):
            raise ValueError("vPIC returned a malformed ErrorCode field")
        if error_text is not None and not isinstance(error_text, str):
            raise ValueError("vPIC returned a malformed ErrorText field")
    except ValueError as exc:
        result = _provider_failure("adapter_malformed_payload", source=source, error=str(exc))
        result["identifier_binding"] = binding
        return result
    if not any(vehicle.get(key) for key in ("make", "model", "manufacturer", "manufacturername", "vehicletype")):
        result = _provider_failure("empty_result", source=source)
        result["identifier_binding"] = binding
        return result
    clean = error_code in ("0", 0)
    return {
        "ok": True,
        "source": source,
        "vin": vin,
        "vehicle": vehicle,
        "error_code": error_code,
        "error_text": error_text,
        "outcome": "success",
        "retryable": False,
        "requires_fallback": False,
        "coverage": "basic" if clean and not partial else "partial_or_unsupported",
        "identifier_binding": binding,
        "diagnostics_status": "reported" if error_code not in (None, "") else "missing",
        "epc_confirmed": False,
    }


def _vin_request(vin: str, *, model_year: int | None = None, extended: bool = False) -> str:
    normalized = normalize_vin(vin)
    if not 8 <= len(normalized) <= 17 or not _VIN_ALLOWED.fullmatch(normalized):
        raise ValueError("invalid_input")
    if model_year is not None:
        _provider_year(model_year)
    endpoint = "DecodeVinValuesExtended" if extended else "DecodeVinValues"
    params = {"format": "json"}
    if model_year is not None:
        params["modelyear"] = str(model_year)
    return f"{_VPIC_BASE_URL}{endpoint}/{quote(normalized, safe='*')}?{urlencode(params)}"


def _parse_vin_payload(
    payload: dict[str, Any], vin: str, *, partial: bool = False, extended: bool = False
) -> dict[str, Any]:
    source = "NHTSA vPIC Extended" if extended else "NHTSA vPIC"
    _validate_vpic_payload(payload)
    rows = payload["Results"]
    if not rows:
        return _provider_failure("empty_result", source=source)
    # A single decode is a single request-bound row, not an arbitrary selection from a result list.
    if len(rows) != 1:
        return _provider_failure("adapter_malformed_payload", source=source)
    result = _parse_vpic_row(rows[0], vin, source=source, partial=partial)
    result["extended"] = extended
    return result


def _wmi_request(wmi: str) -> str:
    if not isinstance(wmi, str):
        raise ValueError("invalid_input")
    normalized = normalize_vin(wmi)
    if len(normalized) not in {3, 6} or not _VIN_ALLOWED.fullmatch(normalized) or "*" in normalized:
        raise ValueError("invalid_input")
    return f"{_VPIC_BASE_URL}DecodeWMI/{quote(normalized)}?format=json"


def _parse_wmi_payload(payload: dict[str, Any], wmi: str) -> dict[str, Any]:
    _validate_vpic_payload(payload)
    rows = payload["Results"]
    if not rows:
        return _provider_failure("empty_result", source="NHTSA vPIC WMI")
    if len(rows) != 1:
        return _provider_failure("adapter_malformed_payload", source="NHTSA vPIC WMI")
    row = rows[0]
    echo = row.get("WMI")
    binding = {"status": "missing", "verified": False}
    if isinstance(echo, str) and echo.strip():
        matches = normalize_vin(echo) == wmi
        binding = {"status": "exact" if matches else "mismatch", "verified": matches}
    if not binding["verified"]:
        result = _provider_failure(
            "identity_unverified" if binding["status"] == "missing" else "identity_mismatch", source="NHTSA vPIC WMI"
        )
        result["identifier_binding"] = binding
        return result
    profile: dict[str, Any] = {}
    for key, value in row.items():
        if value is None or value == "" or value == "Not Applicable":
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return _provider_failure("adapter_malformed_payload", source="NHTSA vPIC WMI")
        if isinstance(value, (int, float)) and (
            not _finite_number(value) or key not in {"ManufacturerId", "VehicleTypeId", "Id"}
        ):
            return _provider_failure("adapter_malformed_payload", source="NHTSA vPIC WMI")
        profile[key.lower()] = value
    if not any(profile.get(key) for key in ("name", "manufacturername", "make")):
        return _provider_failure("empty_result", source="NHTSA vPIC WMI")
    return {
        "ok": True,
        "source": "NHTSA vPIC WMI",
        "wmi": wmi,
        "wmi_profile": profile,
        "outcome": "success",
        "retryable": False,
        "requires_fallback": False,
        "coverage": "basic",
        "identifier_binding": binding,
        "epc_confirmed": False,
    }


def _batch_request(rows: list[tuple[str, int | None]]) -> tuple[str, bytes]:
    if not rows or len(rows) > 50:
        raise ValueError("invalid_batch_size")
    data = ";".join(f"{vin},{year}" if year is not None else vin for vin, year in rows)
    return f"{_VPIC_BASE_URL}DecodeVINValuesBatch/", urlencode({"format": "json", "data": data}).encode("utf-8")


def _parse_batch_payload(payload: dict[str, Any], rows: list[tuple[str, int | None]]) -> dict[str, Any]:
    _validate_vpic_payload(payload)
    by_vin: dict[str, list[dict[str, Any]]] = {}
    for row in payload["Results"]:
        echo = row.get("VIN")
        if isinstance(echo, str):
            by_vin.setdefault(normalize_vin(echo), []).append(row)
    results: dict[str, dict[str, Any]] = {}
    for vin, _year in rows:
        matches = by_vin.get(vin, [])
        if not matches and (len(vin) < 17 or "*" in vin):
            # NHTSA may complete a partial input. Bind by its known positions only when unique.
            matches = [
                row
                for row in payload["Results"]
                if _identifier_binding(vin, row.get("VIN"), partial=True)["status"] == "compatible_partial"
            ]
        if len(matches) != 1:
            results[vin] = _provider_failure(
                "empty_result" if not matches else "ambiguous_provider_result", source="NHTSA vPIC Batch"
            )
            continue
        result = _parse_vpic_row(matches[0], vin, source="NHTSA vPIC Batch", partial=len(vin) < 17 or "*" in vin)
        result["batch"] = True
        results[vin] = result
    return {
        "ok": bool(results) and all(result.get("ok") for result in results.values()),
        "source": "NHTSA vPIC Batch",
        "count": len(rows),
        "results_by_vin": results,
        "outcome": "success" if results and all(row.get("ok") for row in results.values()) else "partial",
        "retryable": False,
        "requires_fallback": any(not row.get("ok") for row in results.values()),
    }


def decode_vin_vpic(
    vin: str, *, model_year: int | None = None, timeout: float = 8.0, extended: bool = False
) -> dict[str, Any]:
    try:
        request_url = _vin_request(vin, model_year=model_year, extended=extended)
    except (ValueError, TypeError, AttributeError):
        return _provider_failure("invalid_input")
    try:
        payload = _vpic_request_json(request_url, timeout=min(timeout, 8.0))
        result = _parse_vin_payload(
            payload, normalize_vin(vin), partial=len(normalize_vin(vin)) < 17 or "*" in vin, extended=extended
        )
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        outcome, retryable = _vpic_failure_details(exc)
        result = _provider_failure(
            outcome, retryable=retryable, error=str(exc) if isinstance(exc, ValueError) else None
        )
    result["request_url"] = request_url
    result["vin"] = normalize_vin(vin)
    result["extended"] = extended
    if model_year is not None:
        result["model_year_hint_requested"] = _provider_year(model_year)
    return result


def decode_wmi_vpic(wmi: str, *, timeout: float = 8.0) -> dict[str, Any]:
    try:
        request_url = _wmi_request(wmi)
    except (ValueError, TypeError, AttributeError):
        return _provider_failure("invalid_input", source="NHTSA vPIC WMI")
    normalized = normalize_vin(wmi)
    try:
        result = _parse_wmi_payload(_vpic_request_json(request_url, timeout=min(timeout, 8.0)), normalized)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        outcome, retryable = _vpic_failure_details(exc)
        result = _provider_failure(
            outcome,
            source="NHTSA vPIC WMI",
            retryable=retryable,
            error=str(exc) if isinstance(exc, ValueError) else None,
        )
    result.update({"request_url": request_url, "wmi": normalized})
    return result


def _batch_item(item: str | dict[str, Any]) -> tuple[str, int | None]:
    if isinstance(item, dict):
        identifier = item.get("identifier") or item.get("vin") or ""
        model_year = item.get("model_year")
        identifier_type = item.get("identifier_type", "auto")
    else:
        identifier, model_year, identifier_type = item, None, "auto"
    if not isinstance(identifier, str):
        return "", None
    classification = classify_identifier(identifier, identifier_type=identifier_type)
    if classification.kind not in {"vin", "vin_partial"}:
        return "", None
    try:
        year = _provider_year(model_year) if model_year not in (None, "") else None
    except ValueError:
        return "", None
    return classification.normalized, year


def decode_vins_vpic_batch(items: list[str | dict[str, Any]], *, timeout: float = 20.0) -> dict[str, Any]:
    # The collector handles <=50-row chunks, incompatible year hints and the shared request budget.
    from .vehicle_identity_inputs import MAX_IDENTITY_ITEMS
    from .vehicle_identity_transport import IdentityBudget, collect_identity_provider_results

    if not isinstance(items, list) or len(items) > MAX_IDENTITY_ITEMS:
        return {**_provider_failure("invalid_input", source="NHTSA vPIC Batch"), "count": 0, "results_by_vin": {}}
    rows = [_batch_item(item) for item in items]
    rows = [(vin, year) for vin, year in rows if vin]
    inputs = [
        {
            "ok": True,
            "identifier": vin,
            "identifier_type": "vin_partial" if len(vin) < 17 or "*" in vin else "vin",
            "context": {"model_year": year} if year is not None else {},
        }
        for vin, year in rows
    ]
    budget = IdentityBudget(max_attempts=20, deadline_seconds=min(max(timeout, 0.0), 30.0))
    collected = collect_identity_provider_results(inputs, live_wmi=False, budget=budget)
    variants: dict[str, set[int | None]] = {}
    for vin, year in rows:
        variants.setdefault(vin, set()).add(year)
    by_vin = {
        vin: result
        for (vin, _year), result in zip(rows, collected["vpic_results"], strict=True)
        if len(variants[vin]) == 1 and result is not None and result.get("ok")
    }
    by_request = [
        {"vin": vin, "model_year_hint": year, "result": result}
        for (vin, year), result in zip(rows, collected["vpic_results"], strict=True)
    ]
    errors = [result for result in collected["vpic_results"] if result and not result.get("ok")]
    return {
        "ok": not errors,
        "source": "NHTSA vPIC Batch",
        "count": len(rows),
        "results_by_vin": by_vin,
        "results_by_request": by_request,
        "request_url": f"{_VPIC_BASE_URL}DecodeVINValuesBatch/" if rows else None,
        "outcome": "partial" if errors else ("success" if rows else "empty_result"),
        "error": errors[0].get("error") if errors else None,
        "retryable": False,
        "requires_fallback": bool(errors),
        "processing": collected["processing"],
    }


def _step_from_source(source: dict[str, Any], query: str, notes_prefix: str = "") -> dict[str, Any]:
    notes = str(source.get("notes") or "").strip()
    if notes_prefix:
        notes = f"{notes_prefix}{notes}" if notes else notes_prefix.rstrip()
    accepts = _as_list(source.get("accepts") or source.get("inputs"))
    adapter_status = _as_list(source.get("adapter_status") or ["route_only"])
    return asdict(
        LookupStep(
            source_id=canonical_source_id(source.get("source_id") or source.get("name")),
            source_name=str(source.get("name") or "").strip(),
            kind=str(source.get("kind") or "").strip(),
            authority=str(source.get("authority") or "").strip(),
            url=str(source.get("url") or "").strip(),
            query=query,
            notes=notes,
            brands=_as_list(source.get("brands")),
            markets=_as_list(source.get("markets")),
            accepts=accepts,
            outputs=_as_list(source.get("outputs")),
            access_mode=str(source.get("access_mode") or "").strip(),
            trust_level=str(source.get("trust_level") or "").strip(),
            requires_login=bool(source.get("requires_login")),
            adapter_status=adapter_status,
            preferred_for=_as_list(source.get("preferred_for")),
            aliases=_as_list(source.get("aliases")),
        )
    )


def _catalog_steps_for_vin(make: str | None, query: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for source in sources_for_make(make):
        steps.append(_step_from_source(source, query))
    if not steps:
        for source in sources_for_inputs("vin"):
            steps.append(_step_from_source(source, query))
    return steps


def _catalog_steps_for_frame_number(query: str, make_hint: str | None = None) -> list[dict[str, Any]]:
    hinted_sources = sources_for_make(make_hint)
    if hinted_sources:
        return [
            _step_from_source(source, query, notes_prefix="Confirm the brand before trusting fitment. ")
            for source in hinted_sources
        ]
    ordered_sources = sources_for_inputs("frame_number", "chassis_number")
    official_first = [source for source in ordered_sources if source.get("authority") == "official"]
    public_second = [source for source in ordered_sources if source.get("authority") != "official"]
    steps = official_first + public_second
    return [
        _step_from_source(source, query, notes_prefix="Confirm the brand before trusting fitment. ") for source in steps
    ]


def _catalog_steps_for_market_code(query: str, make_hint: str | None = None) -> list[dict[str, Any]]:
    if make_hint:
        hinted_sources = sources_for_make(make_hint)
        if hinted_sources:
            return [
                _step_from_source(source, query, notes_prefix="Resolve the market family first. ")
                for source in hinted_sources
            ]
    ordered_sources = sources_for_inputs("vin", "frame_number", "chassis_number", "model_name", "catalog_code")
    return [
        _step_from_source(source, query, notes_prefix="Resolve the market family first. ") for source in ordered_sources
    ]


def _resolved_make(plan: dict[str, Any], make_hint: str | None) -> str:
    vehicle = plan.get("decoded_vehicle") or {}
    return str(vehicle.get("make") or make_hint or "").strip()


def _make_family(make: str) -> str:
    normalized = normalize_make(make)
    if normalized in _BMW_MAKES or normalized.startswith("BMW"):
        return "bmw"
    if normalized in _VAG_MAKES or normalized.startswith(("AUDI", "VOLKSWAGEN", "SKODA", "SEAT", "CUPRA")):
        return "vag"
    return "generic"


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.casefold()
    return any(term.casefold() in lowered for term in terms)


def _part_text(part_name: str | None, part_group: str | None) -> str:
    return " ".join(part for part in (part_name or "", part_group or "") if part).strip()


def _provider_adapters(
    catalog_routes: list[dict[str, Any]], *, captured_oem_number: str | None
) -> list[dict[str, Any]]:
    connected_routes = [
        route["source_name"]
        for route in catalog_routes
        if "connected" in _as_list(route.get("adapter_status")) and route.get("requires_login") is False
    ]
    manual_routes = [
        route["source_name"]
        for route in catalog_routes
        if "manual_capture" in _as_list(route.get("adapter_status")) or route.get("requires_login")
    ]
    return [
        {
            "mode": "route_only",
            "available": True,
            "description": "Return legal catalog routes and the fields the manager must capture.",
            "route_count": len(catalog_routes),
        },
        {
            "mode": "manual_capture",
            "available": True,
            "description": "Accept a manually captured OEM number from an EPC screen/export and validate the dossier shape.",
            "captured": bool(captured_oem_number),
            "candidate_routes": manual_routes,
        },
        {
            "mode": "connected",
            "available": bool(connected_routes),
            "description": "Future legal API/export mode; only active for sources with explicit connected adapter support.",
            "candidate_routes": connected_routes,
        },
    ]


def _source_by_name(catalog_routes: list[dict[str, Any]], source_name: str | None) -> dict[str, Any] | None:
    if not source_name:
        return None
    normalized = canonical_source_id(source_name).casefold()
    for route in catalog_routes:
        references = [route.get("source_id"), route.get("source_name"), *_as_list(route.get("aliases"))]
        if any(canonical_source_id(reference).casefold() == normalized for reference in references if reference):
            return route
    return None


def _validate_captured_oem(raw: str) -> dict[str, Any]:
    normalized = normalize_part_number(raw)
    warnings: list[str] = []
    if not _PART_NUMBER_ALLOWED.fullmatch(raw.strip().upper()) or len(normalized) < 5:
        warnings.append("Captured OEM number is too short or has unexpected characters.")
    return {
        "ok": not warnings,
        "normalized_number": normalized,
        "warnings": warnings,
    }


def _manual_capture_confidence(
    *,
    source: dict[str, Any] | None,
    identifier_kind: str,
    part_name: str | None,
) -> ConfidenceLevel:
    if source and identifier_kind in {"vin", "frame_number"} and part_name:
        access_mode = str(source.get("access_mode") or "")
        trust_level = str(source.get("trust_level") or "")
        if source.get("authority") in {"official", "manufacturer"} or trust_level in {"official", "preferred_paid"}:
            return "high"
        if access_mode in {"public", "public_mirror"}:
            return "low"
        return "medium"
    if part_name:
        return "medium"
    return "low"


def _build_oem_candidates(
    *,
    plan: dict[str, Any],
    part_name: str | None,
    part_group: str | None,
    side: str | None,
    position: str | None,
    old_part_number: str | None,
    captured_oem_number: str | None,
    captured_source: str | None,
    captured_note: str | None,
) -> list[dict[str, Any]]:
    if not captured_oem_number:
        return []

    source = _source_by_name(plan.get("catalog_routes") or [], captured_source)
    validation = _validate_captured_oem(captured_oem_number)
    confidence = _manual_capture_confidence(
        source=source,
        identifier_kind=str((plan.get("identifier") or {}).get("kind") or ""),
        part_name=part_name,
    )
    if not validation["ok"]:
        confidence = "blocked"
    return [
        {
            "number": captured_oem_number.strip().upper(),
            "normalized_number": validation["normalized_number"],
            "role": "captured_original",
            "part_name": part_name or "",
            "part_group": part_group or "",
            "side": side or "",
            "position": position or "",
            "old_part_reference": old_part_number or "",
            "source": captured_source or "manual_capture",
            "source_id": canonical_source_id((source or {}).get("source_id") or captured_source or "manual_capture"),
            "source_aliases": _as_list((source or {}).get("aliases")),
            "source_authority": str((source or {}).get("authority") or "manual"),
            "source_access_mode": str((source or {}).get("access_mode") or ""),
            "fitment_basis": "manual EPC capture for the given identifier and part request",
            "confidence": confidence,
            "validation": validation,
            "note": captured_note or "",
        }
    ]


def _build_supersessions(
    *,
    captured_oem_number: str | None,
    captured_supersedes: str | None,
    captured_source: str | None,
) -> list[dict[str, Any]]:
    if not captured_oem_number or not captured_supersedes:
        return []
    return [
        {
            "from": captured_supersedes.strip().upper(),
            "from_normalized": normalize_part_number(captured_supersedes),
            "to": captured_oem_number.strip().upper(),
            "to_normalized": normalize_part_number(captured_oem_number),
            "source": captured_source or "manual_capture",
            "status": "manual_capture_needs_epc_confirmation",
        }
    ]


def _missing_context(
    *,
    plan: dict[str, Any],
    make: str,
    part_name: str | None,
    part_group: str | None,
    side: str | None,
    position: str | None,
    old_part_number: str | None,
    captured_oem_number: str | None,
) -> list[str]:
    missing: list[str] = []
    text = _part_text(part_name, part_group)
    family = _make_family(make)

    if not text:
        missing.append("part_name or part_group")
    if not plan.get("catalog_routes"):
        missing.append("catalog route for the identifier")
    if not captured_oem_number:
        missing.append("OEM number captured from VIN-specific EPC")

    route_needs_login = any(route.get("requires_login") for route in plan.get("catalog_routes") or [])
    if route_needs_login and not captured_oem_number:
        missing.append("legal paid EPC access or manual EPC capture")

    if text and _contains_any(text, _STEERING_TERMS):
        if not old_part_number and not captured_oem_number:
            missing.append("old steering-rack part number or clear label photo if EPC returns variants")
        if not captured_oem_number:
            missing.append("steering options/drive side when EPC shows several rack variants")

    if text and (family == "vag" or _contains_any(text, _DSG_TERMS)) and _contains_any(text, _DSG_TERMS):
        if not old_part_number and not captured_oem_number:
            missing.append("old mechatronic label or hardware/software number")
        if not captured_oem_number:
            missing.append("gearbox code and PR/options if EPC asks")

    if text and _contains_any(text, _ELECTRONICS_TERMS) and not old_part_number and not captured_oem_number:
        missing.append("old control-unit part number or label photo")

    if text and _contains_any(text, _SIDE_DEPENDENT_TERMS):
        if not side:
            missing.append("side")
        if not position:
            missing.append("position")

    if family == "bmw" and text and not captured_oem_number:
        missing.append("BMW SA/options when AIR/ETK shows option-based variants")

    return list(dict.fromkeys(missing))


def _fitment_confidence(
    *,
    oem_candidates: list[dict[str, Any]],
    missing_context: list[str],
    catalog_routes: list[dict[str, Any]],
) -> dict[str, Any]:
    if oem_candidates:
        candidate_levels = [str(candidate.get("confidence") or "low") for candidate in oem_candidates]
        if any(level == "blocked" for level in candidate_levels):
            level: ConfidenceLevel = "blocked"
            score = 0
        elif len(oem_candidates) == 1:
            candidate_level = candidate_levels[0]
            if candidate_level == "high" and not missing_context:
                level = "high"
                score = 90
            elif candidate_level == "high":
                level = "medium"
                score = 70
            elif candidate_level == "medium":
                level = "medium"
                score = 60
            else:
                level = "low"
                score = 35
        elif all(level == "high" for level in candidate_levels) and missing_context:
            level = "medium"
            score = 70
        else:
            level = "medium"
            score = 60
    elif missing_context:
        level = "blocked"
        score = 0
    elif any(route.get("authority") == "official" for route in catalog_routes):
        level = "low"
        score = 35
    else:
        level = "blocked"
        score = 0

    reasons: list[str] = []
    if oem_candidates:
        reasons.append("OEM candidate captured")
        if any(str(candidate.get("confidence") or "low") != "high" for candidate in oem_candidates):
            reasons.append("Captured candidate source is not high-confidence EPC evidence")
    else:
        reasons.append("No OEM candidate captured yet")
    if missing_context:
        reasons.append("Missing context prevents final fitment confirmation")
    if any(route.get("requires_login") for route in catalog_routes):
        reasons.append("Preferred route requires legal catalog login/manual capture")

    return {
        "level": level,
        "score": score,
        "reasons": reasons,
        "required_evidence": missing_context,
    }


def _next_actions(
    *,
    make: str,
    part_name: str | None,
    oem_candidates: list[dict[str, Any]],
    missing_context: list[str],
    catalog_routes: list[dict[str, Any]],
) -> list[str]:
    actions: list[str] = []
    family = _make_family(make)
    preferred = next((route for route in catalog_routes if route.get("requires_login")), None)
    public_web_routes = [
        route
        for route in catalog_routes
        if canonical_source_id(route.get("source_id") or route.get("source_name"))
        in {PARTSOUQ_SOURCE_ID, AMAYAMA_SOURCE_ID}
    ]
    if not part_name:
        actions.append("Add part_name/part_group before OEM lookup.")
    if preferred and not oem_candidates:
        actions.append(
            f"Open {preferred['source_name']} and capture VIN-specific OEM number, supersession, and quantity."
        )
    elif catalog_routes and not oem_candidates:
        actions.append(
            f"Open {catalog_routes[0]['source_name']} and capture the OEM number from the matching catalog group."
        )
    if public_web_routes and not oem_candidates:
        web_source_names = ", ".join(route["source_name"] for route in public_web_routes)
        actions.append(
            f"If another route is unavailable or incomplete, use {web_source_names} as a public fallback and capture the "
            "diagram/page link, OEM candidate, and visible model/period/engine/transmission/market/position conditions. "
            "Do not bypass a JavaScript, cookie, or anti-bot challenge."
        )
    if family == "bmw" and not oem_candidates:
        actions.append("For BMW, verify the result in AOS/AIR/ETK with VIN and SA/options before purchase search.")
    if family == "vag" and not oem_candidates:
        actions.append("For VAG, verify the OEM catalog result with VIN, PR/options, and gearbox/body code.")
    if oem_candidates:
        if all(candidate.get("confidence") == "high" for candidate in oem_candidates):
            actions.append("Use the verified OEM number as the starting point for market/price search.")
        else:
            actions.append(
                "Treat the captured OEM number as preliminary: verify its diagram/applicability conditions against another "
                "catalog or manufacturer source before treating it as fitment-confirmed."
            )
        actions.append(
            "After a brand and article are captured, compare manufacturer, web-search, and clearly labelled forum evidence "
            "with available cross references; analogs and forum matches remain unconfirmed until applicability "
            "is checked."
        )
        actions.append(
            "Keep full EPC evidence outside CRM; the catalog section contains a concise OEM/result, evidence links, "
            "and next-action summary. The timing and complete client card follow "
            ".agents/skills/manage-autostop-client/SKILL.md."
        )
    if missing_context:
        actions.append("Collect missing context before making a purchase recommendation.")
    return list(dict.fromkeys(actions))


def _finalize_dossier(
    plan: dict[str, Any],
    *,
    make_hint: str | None,
    part_name: str | None,
    part_group: str | None,
    side: str | None,
    position: str | None,
    old_part_number: str | None,
    captured_oem_number: str | None,
    captured_source: str | None,
    captured_supersedes: str | None,
    captured_note: str | None,
) -> dict[str, Any]:
    catalog_routes = list(plan.get("steps") or [])
    plan["catalog_routes"] = catalog_routes
    plan["request"] = {
        "part_name": part_name or "",
        "part_group": part_group or "",
        "side": side or "",
        "position": position or "",
        "old_part_number": old_part_number or "",
        "captured_oem_number": captured_oem_number or "",
        "captured_source": captured_source or "",
    }
    make = _resolved_make(plan, make_hint)
    plan["catalog_vehicle"] = {
        "make": make,
        "family": _make_family(make),
        "source": "decoded_vehicle" if (plan.get("decoded_vehicle") or {}).get("make") else "make_hint",
    }
    plan["provider_adapters"] = _provider_adapters(catalog_routes, captured_oem_number=captured_oem_number)
    plan["oem_candidates"] = _build_oem_candidates(
        plan=plan,
        part_name=part_name,
        part_group=part_group,
        side=side,
        position=position,
        old_part_number=old_part_number,
        captured_oem_number=captured_oem_number,
        captured_source=captured_source,
        captured_note=captured_note,
    )
    plan["supersessions"] = _build_supersessions(
        captured_oem_number=captured_oem_number,
        captured_supersedes=captured_supersedes,
        captured_source=captured_source,
    )
    plan["missing_context"] = _missing_context(
        plan=plan,
        make=make,
        part_name=part_name,
        part_group=part_group,
        side=side,
        position=position,
        old_part_number=old_part_number,
        captured_oem_number=captured_oem_number,
    )
    plan["fitment_confidence"] = _fitment_confidence(
        oem_candidates=plan["oem_candidates"],
        missing_context=plan["missing_context"],
        catalog_routes=catalog_routes,
    )
    plan["next_actions"] = _next_actions(
        make=make,
        part_name=part_name,
        oem_candidates=plan["oem_candidates"],
        missing_context=plan["missing_context"],
        catalog_routes=catalog_routes,
    )
    return plan


def build_lookup_plan(
    raw_identifier: str,
    *,
    model_year: int | None = None,
    make_hint: str | None = None,
    identifier_type: str = "auto",
    live_vpic: bool = True,
    vpic_result: dict[str, Any] | None = None,
    part_name: str | None = None,
    part_group: str | None = None,
    side: str | None = None,
    position: str | None = None,
    old_part_number: str | None = None,
    captured_oem_number: str | None = None,
    captured_source: str | None = None,
    captured_supersedes: str | None = None,
    captured_note: str | None = None,
) -> dict[str, Any]:
    classification = classify_identifier(raw_identifier, identifier_type=identifier_type)
    public_query = _redact_identifier(classification.normalized)["display"]
    plan: dict[str, Any] = {
        "ok": True,
        "identifier": _public_identifier(classification),
        "source_registry_version": load_source_registry().get("version", 0),
        "decoded_vehicle": None,
        "steps": [],
        "hints": [],
        "warnings": [],
    }

    if classification.kind in {"vin", "vin_partial"}:
        if vpic_result is not None:
            decode = vpic_result
        elif live_vpic:
            decode = decode_vin_vpic(classification.normalized, model_year=model_year)
        else:
            decode = {
                "ok": False,
                "vehicle": {},
                "error": "vPIC decode skipped because live_vpic is false",
                "skipped": True,
            }
        plan["decoded_vehicle"] = _redact_identifier_payload(decode.get("vehicle"), classification.normalized)
        if not decode.get("ok"):
            plan["warnings"].append(decode.get("error", "VIN decode failed"))
            plan["steps"] = _catalog_steps_for_vin(make_hint, public_query)
            return _finalize_dossier(
                plan,
                make_hint=make_hint,
                part_name=part_name,
                part_group=part_group,
                side=side,
                position=position,
                old_part_number=old_part_number,
                captured_oem_number=captured_oem_number,
                captured_source=captured_source,
                captured_supersedes=captured_supersedes,
                captured_note=captured_note,
            )

        make = str((decode.get("vehicle") or {}).get("make") or make_hint or "").strip()
        if not make:
            plan["warnings"].append("vPIC did not return a make; follow the generic catalog route")
        else:
            plan["hints"].append(f"Decoded make: {make}")
        plan["steps"] = _catalog_steps_for_vin(make, public_query)
        return _finalize_dossier(
            plan,
            make_hint=make_hint,
            part_name=part_name,
            part_group=part_group,
            side=side,
            position=position,
            old_part_number=old_part_number,
            captured_oem_number=captured_oem_number,
            captured_source=captured_source,
            captured_supersedes=captured_supersedes,
            captured_note=captured_note,
        )

    if classification.kind == "frame_number":
        plan["warnings"].append(
            "Frame numbers need a market-appropriate catalog route; confirm the brand before trusting the output."
        )
        plan["steps"] = _catalog_steps_for_frame_number(public_query, make_hint=make_hint)
        return _finalize_dossier(
            plan,
            make_hint=make_hint,
            part_name=part_name,
            part_group=part_group,
            side=side,
            position=position,
            old_part_number=old_part_number,
            captured_oem_number=captured_oem_number,
            captured_source=captured_source,
            captured_supersedes=captured_supersedes,
            captured_note=captured_note,
        )

    if classification.kind == "market_code":
        plan["warnings"].append("Market-specific code detected; resolve the vehicle family before OEM lookup.")
        plan["steps"] = _catalog_steps_for_market_code(public_query, make_hint=make_hint)
        return _finalize_dossier(
            plan,
            make_hint=make_hint,
            part_name=part_name,
            part_group=part_group,
            side=side,
            position=position,
            old_part_number=old_part_number,
            captured_oem_number=captured_oem_number,
            captured_source=captured_source,
            captured_supersedes=captured_supersedes,
            captured_note=captured_note,
        )

    plan["ok"] = False
    plan["warnings"].append("Could not classify the identifier safely.")
    plan["steps"] = _catalog_steps_for_market_code(public_query, make_hint=make_hint)
    return _finalize_dossier(
        plan,
        make_hint=make_hint,
        part_name=part_name,
        part_group=part_group,
        side=side,
        position=position,
        old_part_number=old_part_number,
        captured_oem_number=captured_oem_number,
        captured_source=captured_source,
        captured_supersedes=captured_supersedes,
        captured_note=captured_note,
    )


def lookup_original_parts(
    raw_identifier: str,
    *,
    model_year: int | None = None,
    make_hint: str | None = None,
    part_name: str | None = None,
    part_group: str | None = None,
    side: str | None = None,
    position: str | None = None,
    old_part_number: str | None = None,
    captured_oem_number: str | None = None,
    captured_source: str | None = None,
    captured_supersedes: str | None = None,
    captured_note: str | None = None,
) -> dict[str, Any]:
    return build_lookup_plan(
        raw_identifier,
        model_year=model_year,
        make_hint=make_hint,
        part_name=part_name,
        part_group=part_group,
        side=side,
        position=position,
        old_part_number=old_part_number,
        captured_oem_number=captured_oem_number,
        captured_source=captured_source,
        captured_supersedes=captured_supersedes,
        captured_note=captured_note,
    )
