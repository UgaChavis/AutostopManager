from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .vin_sources import load_source_registry, sources_for_inputs, sources_for_make

LookupKind = Literal["vin", "vin_partial", "frame_number", "market_code", "unknown"]

_VIN_ALLOWED = re.compile(r"^[A-HJ-NPR-Z0-9*]+$")
_FRAME_ALLOWED = re.compile(r"^[A-Z0-9][A-Z0-9\-]*$")


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
    source_name: str
    kind: str
    authority: str
    url: str
    query: str
    notes: str


def _compact_text(raw: str) -> str:
    return re.sub(r"\s+", "", raw.strip().upper())


def normalize_vin(raw: str) -> str:
    return re.sub(r"[\s\-]+", "", raw.strip().upper())


def normalize_frame_number(raw: str) -> str:
    return re.sub(r"\s+", "", raw.strip().upper())


def normalize_market_code(raw: str) -> str:
    return _compact_text(raw).replace("-", "")


def classify_identifier(raw: str) -> IdentifierClassification:
    original = raw.strip()
    compact = _compact_text(raw)
    notes: list[str] = []

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
    if len(alnum) >= 4 and len(alnum) <= 16 and any(ch.isalpha() for ch in alnum) and any(
        ch.isdigit() for ch in alnum
    ):
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


def _extract_vpic_vehicle(result: dict[str, Any]) -> dict[str, Any]:
    keys = [
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
    ]
    vehicle = {key.lower(): result.get(key) for key in keys if result.get(key) not in (None, "", "Not Applicable")}
    if "modelyear" in vehicle:
        try:
            vehicle["modelyear"] = int(vehicle["modelyear"])
        except (TypeError, ValueError):
            pass
    return vehicle


def _vpic_request_json(request_url: str, *, timeout: float, data: bytes | None = None) -> dict[str, Any]:
    request = Request(request_url, data=data, headers={"User-Agent": "AutostopManager/0.1"})
    if data is not None:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def decode_vin_vpic(
    vin: str,
    *,
    model_year: int | None = None,
    timeout: float = 10.0,
    extended: bool = False,
) -> dict[str, Any]:
    normalized = normalize_vin(vin)
    if len(normalized) < 8:
        return {
            "ok": False,
            "source": "NHTSA vPIC",
            "error": "VIN is too short for vPIC decoding",
            "vin": normalized,
        }
    endpoint = "DecodeVinValuesExtended" if extended else "DecodeVinValues"
    base_url = f"https://vpic.nhtsa.dot.gov/api/vehicles/{endpoint}/{quote(normalized, safe='*')}"
    params = ["format=json"]
    if model_year is not None:
        params.append(f"modelyear={int(model_year)}")
    request_url = f"{base_url}?{'&'.join(params)}"

    try:
        payload = _vpic_request_json(request_url, timeout=timeout)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return {
            "ok": False,
            "source": "NHTSA vPIC Extended" if extended else "NHTSA vPIC",
            "request_url": request_url,
            "vin": normalized,
            "error": str(exc),
            "extended": extended,
        }

    results = payload.get("Results") or []
    if not results:
        return {
            "ok": False,
            "source": "NHTSA vPIC Extended" if extended else "NHTSA vPIC",
            "request_url": request_url,
            "vin": normalized,
            "error": "vPIC returned no results",
            "payload": payload,
            "extended": extended,
        }

    first = results[0]
    vehicle = _extract_vpic_vehicle(first)
    return {
        "ok": True,
        "source": "NHTSA vPIC Extended" if extended else "NHTSA vPIC",
        "request_url": request_url,
        "vin": normalized,
        "vehicle": vehicle,
        "error_code": first.get("ErrorCode"),
        "error_text": first.get("ErrorText"),
        "payload": payload,
        "extended": extended,
    }


def decode_wmi_vpic(wmi: str, *, timeout: float = 10.0) -> dict[str, Any]:
    normalized = normalize_vin(wmi)[:3]
    if len(normalized) != 3:
        return {"ok": False, "source": "NHTSA vPIC WMI", "wmi": normalized, "error": "WMI must be 3 characters"}
    request_url = f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeWMI/{quote(normalized)}?format=json"
    try:
        payload = _vpic_request_json(request_url, timeout=timeout)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return {"ok": False, "source": "NHTSA vPIC WMI", "request_url": request_url, "wmi": normalized, "error": str(exc)}

    results = payload.get("Results") or []
    if not results:
        return {"ok": False, "source": "NHTSA vPIC WMI", "request_url": request_url, "wmi": normalized, "error": "vPIC returned no WMI results", "payload": payload}

    first = results[0]
    return {
        "ok": True,
        "source": "NHTSA vPIC WMI",
        "request_url": request_url,
        "wmi": normalized,
        "wmi_profile": {
            key.lower(): value
            for key, value in first.items()
            if value not in (None, "", "Not Applicable")
        },
        "payload": payload,
    }


def _batch_item(item: str | dict[str, Any]) -> tuple[str, int | None]:
    if isinstance(item, dict):
        identifier = str(item.get("identifier") or item.get("vin") or "")
        model_year = item.get("model_year") or item.get("production_year")
    else:
        identifier = str(item)
        model_year = None
    try:
        year = int(model_year) if model_year not in (None, "") else None
    except (TypeError, ValueError):
        year = None
    return normalize_vin(identifier), year


def decode_vins_vpic_batch(items: list[str | dict[str, Any]], *, timeout: float = 20.0) -> dict[str, Any]:
    normalized_items = [_batch_item(item) for item in items]
    vin_rows = [(vin, year) for vin, year in normalized_items if classify_identifier(vin).kind in {"vin", "vin_partial"}]
    if not vin_rows:
        return {"ok": True, "source": "NHTSA vPIC Batch", "count": 0, "results_by_vin": {}, "request_url": None}

    data_rows = [f"{vin},{year}" if year is not None else vin for vin, year in vin_rows]
    request_url = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVINValuesBatch/"
    body = urlencode({"format": "json", "data": ";".join(data_rows)}).encode("utf-8")
    try:
        payload = _vpic_request_json(request_url, timeout=timeout, data=body)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return {
            "ok": False,
            "source": "NHTSA vPIC Batch",
            "request_url": request_url,
            "count": len(vin_rows),
            "error": str(exc),
            "results_by_vin": {},
        }

    results_by_vin: dict[str, dict[str, Any]] = {}
    for row in payload.get("Results") or []:
        vin = normalize_vin(str(row.get("VIN") or ""))
        if not vin:
            continue
        results_by_vin[vin] = {
            "ok": True,
            "source": "NHTSA vPIC Batch",
            "request_url": request_url,
            "vin": vin,
            "vehicle": _extract_vpic_vehicle(row),
            "error_code": row.get("ErrorCode"),
            "error_text": row.get("ErrorText"),
            "payload": {"Results": [row]},
            "batch": True,
        }
    return {
        "ok": True,
        "source": "NHTSA vPIC Batch",
        "request_url": request_url,
        "count": len(vin_rows),
        "results_by_vin": results_by_vin,
        "payload": payload,
    }


def _step_from_source(source: dict[str, Any], query: str, notes_prefix: str = "") -> dict[str, Any]:
    notes = str(source.get("notes") or "").strip()
    if notes_prefix:
        notes = f"{notes_prefix}{notes}" if notes else notes_prefix.rstrip()
    return asdict(
        LookupStep(
            source_name=str(source.get("name") or "").strip(),
            kind=str(source.get("kind") or "").strip(),
            authority=str(source.get("authority") or "").strip(),
            url=str(source.get("url") or "").strip(),
            query=query,
            notes=notes,
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
    return [_step_from_source(source, query, notes_prefix="Confirm the brand before trusting fitment. ") for source in steps]


def _catalog_steps_for_market_code(query: str, make_hint: str | None = None) -> list[dict[str, Any]]:
    if make_hint:
        hinted_sources = sources_for_make(make_hint)
        if hinted_sources:
            return [
                _step_from_source(source, query, notes_prefix="Resolve the market family first. ")
                for source in hinted_sources
            ]
    ordered_sources = sources_for_inputs("vin", "frame_number", "chassis_number", "model_name", "catalog_code")
    return [_step_from_source(source, query, notes_prefix="Resolve the market family first. ") for source in ordered_sources]


def build_lookup_plan(
    raw_identifier: str,
    *,
    model_year: int | None = None,
    make_hint: str | None = None,
) -> dict[str, Any]:
    classification = classify_identifier(raw_identifier)
    plan: dict[str, Any] = {
        "ok": True,
        "identifier": asdict(classification),
        "source_registry_version": load_source_registry().get("version", 0),
        "decoded_vehicle": None,
        "steps": [],
        "hints": [],
        "warnings": [],
    }

    if classification.kind in {"vin", "vin_partial"}:
        decode = decode_vin_vpic(classification.normalized, model_year=model_year)
        plan["decoded_vehicle"] = decode.get("vehicle")
        if not decode.get("ok"):
            plan["warnings"].append(decode.get("error", "VIN decode failed"))
            plan["steps"] = _catalog_steps_for_vin(None, classification.normalized)
            return plan

        make = str((decode.get("vehicle") or {}).get("make") or make_hint or "").strip()
        if not make:
            plan["warnings"].append("vPIC did not return a make; follow the generic catalog route")
        else:
            plan["hints"].append(f"Decoded make: {make}")
        plan["steps"] = _catalog_steps_for_vin(make, classification.normalized)
        return plan

    if classification.kind == "frame_number":
        plan["warnings"].append(
            "Frame numbers need a market-appropriate catalog route; confirm the brand before trusting the output."
        )
        plan["steps"] = _catalog_steps_for_frame_number(classification.normalized, make_hint=make_hint)
        return plan

    if classification.kind == "market_code":
        plan["warnings"].append("Market-specific code detected; resolve the vehicle family before OEM lookup.")
        plan["steps"] = _catalog_steps_for_market_code(classification.normalized, make_hint=make_hint)
        return plan

    plan["ok"] = False
    plan["warnings"].append("Could not classify the identifier safely.")
    plan["steps"] = _catalog_steps_for_market_code(classification.normalized, make_hint=make_hint)
    return plan


def lookup_original_parts(
    raw_identifier: str,
    *,
    model_year: int | None = None,
    make_hint: str | None = None,
) -> dict[str, Any]:
    plan = build_lookup_plan(raw_identifier, model_year=model_year, make_hint=make_hint)
    return plan
