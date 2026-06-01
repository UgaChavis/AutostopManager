from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import re
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .vin_sources import load_source_registry, normalize_make, sources_for_inputs, sources_for_make

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
    accepts = _as_list(source.get("accepts") or source.get("inputs"))
    adapter_status = _as_list(source.get("adapter_status") or ["route_only"])
    return asdict(
        LookupStep(
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


def _provider_adapters(catalog_routes: list[dict[str, Any]], *, captured_oem_number: str | None) -> list[dict[str, Any]]:
    connected_routes = [
        route["source_name"]
        for route in catalog_routes
        if "connected" in route.get("adapter_status", []) and route.get("requires_login") is False
    ]
    manual_routes = [
        route["source_name"]
        for route in catalog_routes
        if "manual_capture" in route.get("adapter_status", []) or route.get("requires_login")
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
    normalized = source_name.casefold().strip()
    for route in catalog_routes:
        if str(route.get("source_name") or "").casefold().strip() == normalized:
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
        if source.get("authority") == "official" or trust_level in {"official", "preferred_paid"}:
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
        elif len(oem_candidates) == 1 and not missing_context:
            level = "high"
            score = 90
        elif any(level == "high" for level in candidate_levels):
            level = "medium" if missing_context else "high"
            score = 70 if missing_context else 90
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
    if not part_name:
        actions.append("Add part_name/part_group before OEM lookup.")
    if preferred and not oem_candidates:
        actions.append(f"Open {preferred['source_name']} and capture VIN-specific OEM number, supersession, and quantity.")
    elif catalog_routes and not oem_candidates:
        actions.append(f"Open {catalog_routes[0]['source_name']} and capture the OEM number from the matching catalog group.")
    if family == "bmw" and not oem_candidates:
        actions.append("For BMW, verify the result in AOS/AIR/ETK with VIN and SA/options before purchase search.")
    if family == "vag" and not oem_candidates:
        actions.append("For VAG, verify ETKA/partslink24 result with VIN, PR/options, and gearbox/body code.")
    if oem_candidates:
        actions.append("Use the confirmed OEM number as the only starting point for market/price search.")
        actions.append("Keep full EPC evidence outside CRM; write only concise OEM/result/next-action summary.")
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
        plan["steps"] = _catalog_steps_for_vin(make, classification.normalized)
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
        plan["steps"] = _catalog_steps_for_frame_number(classification.normalized, make_hint=make_hint)
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
        plan["steps"] = _catalog_steps_for_market_code(classification.normalized, make_hint=make_hint)
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
    plan["steps"] = _catalog_steps_for_market_code(classification.normalized, make_hint=make_hint)
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
    plan = build_lookup_plan(
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
    return plan
