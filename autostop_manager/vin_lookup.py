from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote
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
        "Make",
        "Model",
        "ModelYear",
        "Trim",
        "Series",
        "BodyClass",
        "VehicleType",
        "PlantCountry",
        "PlantCity",
        "EngineModel",
        "EngineConfiguration",
        "EngineCylinders",
        "TransmissionStyle",
        "TransmissionSpeeds",
        "DriveType",
    ]
    vehicle = {key.lower(): result.get(key) for key in keys if result.get(key) not in (None, "", "Not Applicable")}
    if "modelyear" in vehicle:
        try:
            vehicle["modelyear"] = int(vehicle["modelyear"])
        except (TypeError, ValueError):
            pass
    return vehicle


def decode_vin_vpic(vin: str, *, model_year: int | None = None, timeout: float = 10.0) -> dict[str, Any]:
    normalized = normalize_vin(vin)
    if len(normalized) < 8:
        return {
            "ok": False,
            "source": "NHTSA vPIC",
            "error": "VIN is too short for vPIC decoding",
            "vin": normalized,
        }
    base_url = f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{quote(normalized, safe='*')}"
    params = ["format=json"]
    if model_year is not None:
        params.append(f"modelyear={int(model_year)}")
    request_url = f"{base_url}?{'&'.join(params)}"

    request = Request(request_url, headers={"User-Agent": "AutostopManager/0.1"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return {
            "ok": False,
            "source": "NHTSA vPIC",
            "request_url": request_url,
            "vin": normalized,
            "error": str(exc),
        }

    results = payload.get("Results") or []
    if not results:
        return {
            "ok": False,
            "source": "NHTSA vPIC",
            "request_url": request_url,
            "vin": normalized,
            "error": "vPIC returned no results",
            "payload": payload,
        }

    first = results[0]
    vehicle = _extract_vpic_vehicle(first)
    return {
        "ok": True,
        "source": "NHTSA vPIC",
        "request_url": request_url,
        "vin": normalized,
        "vehicle": vehicle,
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
