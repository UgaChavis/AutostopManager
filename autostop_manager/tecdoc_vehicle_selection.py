"""Select TecDoc candidates from independently supplied vehicle facts, without VINdecode."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import date
from email.utils import parsedate_to_datetime
from typing import Any

from .vehicle_identity import identity_values_agree

MAX_MATCHED_MODELS = 8
MAX_ENGINE_LOOKUPS = 8
_CONTEXT_PROFILES = ("vehicle_profile", "vehicle_profile_compact", "crm_vehicle_profile")
_ALIASES = {
    "make_display": "make",
    "model_display": "model",
    "engine_model": "engine",
    "gearbox_model": "transmission",
    "engine_code": "engine",
    "engine_displacement_cc": "displacement_cc",
    "horsepower": "power_hp",
    "engine_power_kw": "power_kw",
}
_FIELDS = (
    "make",
    "model",
    "engine",
    "modification",
    "series",
    "transmission",
    "transmission_speeds",
    "drivetrain",
    "engine_type",
    "fuel_type",
    "displacement_cc",
    "power_hp",
    "power_kw",
    "production_year",
    "production_date",
    "model_year",
)
_NUMBERS = {"displacement_cc": 20.0, "power_hp": 1.0, "power_kw": 0.75}


def _present(value: Any) -> bool:
    return value is not None and value != "" and not isinstance(value, (dict, list, bool))


def _number(value: Any) -> float | None:
    if not _present(value):
        return None
    try:
        result = float(str(value).strip().replace(",", "."))
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _agrees(field: str, left: Any, right: Any) -> bool:
    if field in _NUMBERS:
        a, b = _number(left), _number(right)
        return a is not None and b is not None and abs(a - b) <= max(_NUMBERS[field], a * 0.01)
    if field in {"engine_type", "fuel_type"}:
        aliases = {"бензин": "petrol", "gasoline": "petrol", "дизель": "diesel", "дизельный": "diesel"}
        left_text, right_text = (str(value).strip().casefold() for value in (left, right))
        return aliases.get(left_text, left_text) == aliases.get(right_text, right_text)
    if field == "engine" and _engine_description(left) != _engine_description(right):
        return True  # A code and a marketing description are incomparable.
    if field == "engine" and _engine_description(left) and _engine_description(right):
        return _engine_comparison(left, {"engine": right}) is not False
    return identity_values_agree(field, left, right)


def _engine_description(value: Any) -> bool:
    return bool(re.search(r"\d[.,]\d|\b(?:TDI|FSI|TFSI|HDI|DIESEL|PETROL)\b", str(value), re.I))


def _engine_comparison(left: Any, profile: dict[str, Any]) -> bool | None:
    right = profile.get("engine_code") or profile.get("engine")
    if not _engine_description(left):
        if not _present(right) or _engine_description(right):
            return None
        alternatives = re.split(r"[,;/]", str(right))
        return any(identity_values_agree("engine", left, value.strip()) for value in alternatives)
    descriptors = [str(profile.get(key) or "") for key in ("engine", "engine_name", "modification")]
    volume = re.search(r"(\d[.,]\d)\b", str(left))
    compared = []
    if volume:
        displacement = _number(profile.get("displacement_cc"))
        other = next((match for text in descriptors if (match := re.search(r"(\d[.,]\d)\b", text))), None)
        if displacement:
            compared.append(abs(float(volume[1].replace(",", ".")) * 1000 - displacement) <= 100)
        elif other:
            compared.append(volume[1].replace(",", ".") == other[1].replace(",", "."))
    diesel = re.search(r"\b(?:TDI|HDI|DIESEL)\b", str(left), re.I)
    petrol = re.search(r"\b(?:FSI|TFSI|PETROL)\b", str(left), re.I)
    if diesel or petrol:
        fuel = profile.get("fuel_type")
        if fuel:
            compared.append(_agrees("fuel_type", "diesel" if diesel else "petrol", fuel))
        elif any(re.search(r"\b(?:TDI|HDI|DIESEL|FSI|TFSI|PETROL)\b", text, re.I) for text in descriptors):
            pattern = r"\b(?:TDI|HDI|DIESEL)\b" if diesel else r"\b(?:FSI|TFSI|PETROL)\b"
            compared.append(any(re.search(pattern, text, re.I) for text in descriptors))
    return all(compared) if compared else None


def _context_rows(context: dict[str, Any], *, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 8:
        return []
    rows = []
    for key in _CONTEXT_PROFILES:
        nested = context.get(key)
        if isinstance(nested, dict):
            rows.extend(_context_rows(nested, depth=depth + 1))
    rows.append(context)
    return rows


def _independent_values(identity: dict[str, Any], context: dict[str, Any] | None) -> dict[str, list[Any]]:
    values: dict[str, list[Any]] = {field: [] for field in _FIELDS}
    evidence = list(identity.get("field_evidence") or [])
    for row in _context_rows(context or {}):
        evidence.extend({"field": key, "value": value, "source": "CRM context"} for key, value in row.items())
    for row in evidence:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "")
        # CRM includes supplied client/photo facts. Local VIN patterns and the
        # catalogue being queried cannot establish characteristics of this car.
        trusted = source.startswith("CRM") or (
            source == "NHTSA vPIC"
            and row.get("bound") is not False
            and row.get("identifier_binding") not in {"mismatch", "unverified", "identifier_mismatch"}
            and not row.get("depends_on")
        )
        field = _ALIASES.get(str(row.get("field")), row.get("field"))
        value = row.get("value")
        if trusted and field in values and _present(value) and value not in values[field]:
            values[field].append(value)
    return values


def _invalid_context_numbers(context: dict[str, Any] | None) -> list[str]:
    invalid = set()
    for row in _context_rows(context or {}):
        for key, value in row.items():
            field = _ALIASES.get(key, key)
            if field in _NUMBERS and value is not None and value != "" and _number(value) is None:
                invalid.add(field)
    return sorted(invalid)


def _context_conflicts(identity: dict[str, Any], values: dict[str, list[Any]]) -> list[dict[str, Any]]:
    conflicts = [
        {"field": field, "identity": candidates, "source": "independent_context"}
        for field, candidates in values.items()
        if len(candidates) > 1
        and any(
            not _agrees(field, left, right)
            for index, left in enumerate(candidates)
            for right in candidates[index + 1 :]
        )
    ]
    for row in identity.get("conflicts") or []:
        if isinstance(row, dict) and row.get("field") in _FIELDS:
            scopes = row.get("blocking_scopes")
            if not isinstance(scopes, list) or "vehicle" in scopes:
                conflicts.append(row)
    for field, state in (identity.get("field_statuses") or {}).items():
        if (
            field in _FIELDS
            and isinstance(state, dict)
            and state.get("status") == "disputed"
            and not any(row.get("field") == field for row in conflicts)
        ):
            conflicts.append({"field": field, "source": "disputed_identity_field"})
    return conflicts


def _records(payload: Any, *, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 8:
        raise ValueError("catalogue_envelope_too_deep")
    if isinstance(payload, list):
        records = []
        for item in payload:
            records.extend(_records(item, depth=depth + 1))
        return records
    if not isinstance(payload, dict):
        return []
    wrappers = [key for key in ("data", "result", "array", "items", "makes", "models") if key in payload]
    if wrappers:
        records = []
        for key in wrappers:
            records.extend(_records(payload[key], depth=depth + 1))
        return records
    return [payload]


def _first(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    return next((row[key] for key in keys if _present(row.get(key))), None)


def _id(value: Any) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isascii() and text.isdigit() and int(text) > 0 else None


def _directory_rows(call: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    id_keys = (
        kind + "Id",
        kind + "_id",
        "MFA_ID" if kind == "make" else "MOD_ID",
        "manuId" if kind == "make" else "modelId",
        "id",
        "ID",
    )
    name_keys = (
        kind + "Name",
        kind + "_name",
        kind,
        "MFA_BRAND" if kind == "make" else "MOD_CDS_TEXT",
        "manuName" if kind == "make" else "modelName",
        "name",
        "NAME",
        "description",
    )
    rows: dict[int, dict[str, Any]] = {}
    for raw in _records(call.get("payload")):
        identifier, name = _id(_first(raw, id_keys)), _first(raw, name_keys)
        if identifier and name:
            if identifier in rows and rows[identifier]["name"].strip().casefold() != str(name).strip().casefold():
                raise ValueError("conflicting_catalogue_reference_names")
            rows[identifier] = {"id": identifier, "name": str(name)}
    return list(rows.values())


def _model_family(value: Any) -> str:
    # TecDoc generation/platform suffixes are navigation clues, not evidence of
    # the customer's generation. Preserve different families such as Corolla Cross.
    text = re.sub(r"\s*[([][^\])]*[\])]", "", str(value or "")).strip()
    return re.sub(r"\s+(?:I{1,3}|IV|V|VI{0,3}|IX|X)$", "", text, flags=re.I).strip()


def _date(value: Any) -> date | None:
    if not _present(value):
        return None
    text = str(value).strip()
    match = re.fullmatch(r"(\d{4})(?:[-/]?(\d{2}))?(?:[-/]?(\d{2}))?", text)
    try:
        if match:
            return date(int(match[1]), int(match[2] or 1), int(match[3] or 1))
        return parsedate_to_datetime(text).date()
    except (ValueError, TypeError, OverflowError):
        return None


def _range_conflicts(profile: dict[str, Any], values: dict[str, list[Any]]) -> list[dict[str, Any]]:
    start, end = (_date(profile.get("production_date_" + boundary)) for boundary in ("from", "to"))
    start_year = start.year if start else _number(profile.get("production_year_from"))
    end_year = end.year if end else _number(profile.get("production_year_to"))
    conflicts = []
    for year in values["production_year"]:
        number = _number(year)
        if number and ((start_year and number < start_year) or (end_year and number > end_year)):
            conflicts.append({"field": "production_year", "identity": year, "catalogue": [start_year, end_year]})
    for raw in values["production_date"]:
        value = _date(raw)
        # An input month denotes that whole month; its first day must not reject
        # a vehicle whose catalogue production starts later in that month.
        month_only = re.fullmatch(r"\d{4}-\d{2}", str(raw)) is not None
        before = bool(
            value
            and start
            and (value < start if not month_only else (value.year, value.month) < (start.year, start.month))
        )
        after = bool(value and end and value > end)
        if before or after:
            conflicts.append(
                {
                    "field": "production_date",
                    "identity": raw,
                    "catalogue": [start.isoformat() if start else None, end.isoformat() if end else None],
                }
            )
    return conflicts


def _profile_conflicts(profile: dict[str, Any], values: dict[str, list[Any]]) -> list[dict[str, Any]]:
    conflicts = _range_conflicts(profile, values)
    for field, known in values.items():
        if field in {"production_year", "production_date"}:
            continue
        right = profile.get("drive_type") or profile.get("drivetrain") if field == "drivetrain" else profile.get(field)
        if field == "engine":
            for left in known:
                if _engine_comparison(left, profile) is False:
                    conflicts.append(
                        {
                            "field": field,
                            "identity": left,
                            "catalogue": profile.get("engine_code")
                            or profile.get("engine")
                            or profile.get("modification"),
                        }
                    )
            continue
        if field in _NUMBERS:
            minimum = _number(profile.get(field + "_from", right))
            maximum = _number(profile.get(field + "_to", right))
            minimum = minimum if minimum is not None else maximum
            maximum = maximum if maximum is not None else minimum
            for left in known:
                number = _number(left)
                tolerance = max(_NUMBERS[field], (number or 0) * 0.01)
                if number and (
                    (minimum and number < minimum - tolerance) or (maximum and number > maximum + tolerance)
                ):
                    conflicts.append({"field": field, "identity": left, "catalogue": [minimum, maximum]})
            continue
        if not _present(right):
            continue
        for left in known:
            agrees = _agrees(field, left, right)
            if field == "model" and _model_family(left).casefold() == str(left).strip().casefold():
                agrees = agrees or _agrees("model", _model_family(left), _model_family(right))
            if not agrees:
                conflicts.append({"field": field, "identity": left, "catalogue": right})
    return conflicts


def _missing_discriminators(profiles: list[dict[str, Any]], values: dict[str, list[Any]]) -> list[str]:
    fields = (
        "engine",
        "displacement_cc",
        "power_hp",
        "power_kw",
        "production_year",
        "transmission",
        "drivetrain",
        "modification",
        "series",
    )
    aliases = {"engine": ("engine_code", "engine"), "production_year": ("production_date_from", "production_date_to")}
    missing = []
    for field in fields:
        if values[field]:
            continue
        keys = aliases.get(field, (field, field + "_from", field + "_to"))
        alternatives = {tuple(str(profile.get(key) or "") for key in keys) for profile in profiles}
        if len(alternatives) > 1 and any(any(row) for row in alternatives):
            missing.append(field)
    return (
        missing
        or [field for field in ("series", "production_date", "modification") if not values[field]]
        or ["catalogue_variant_confirmation"]
    )


def _provider_failure(response: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "not_checked" if response.get("dry_run") else "provider_failed",
        "provider_outcome": response.get("outcome"),
    }


def _vehicle_type(value: Any) -> str | None:
    return {"pc": "PC", "cv": "CV", "motorcycle": "Motorcycle", "mc": "Motorcycle"}.get(
        str(value or "").strip().casefold()
    )


def _reference_mismatch(profile: dict[str, Any], *, car_type: str, **identifiers: int | None) -> bool:
    explicit_type = profile.get("vehicle_type")
    if _present(explicit_type) and _vehicle_type(explicit_type) != car_type:
        return True
    return any(
        _present(profile.get(field)) and _id(profile[field]) != expected
        for field, expected in identifiers.items()
        if expected is not None
    )


def _navigate_catalogue(
    values: dict[str, list[Any]], car_type: str, invoke: Callable[..., dict[str, Any]]
) -> dict[str, Any]:
    makes = invoke("getMakes", provider_parameters={"carType": car_type})
    if not makes.get("ok") or makes.get("dry_run"):
        return _provider_failure(makes)
    try:
        matching_makes = [
            row for row in _directory_rows(makes, "make") if _agrees("make", values["make"][0], row["name"])
        ]
    except ValueError:
        return {"status": "provider_failed", "provider_outcome": "unparsed_response"}
    if len(matching_makes) != 1:
        return {
            "status": "ambiguous_vehicle_modification" if matching_makes else "no_catalog_vehicle",
            "missing_fields": ["make"],
            "choices": matching_makes,
        }
    make = matching_makes[0]
    models = invoke("getModels", provider_parameters={"makeId": make["id"], "carType": car_type, "lang": 16})
    if not models.get("ok") or models.get("dry_run"):
        return _provider_failure(models)
    try:
        matching_models = [
            row
            for row in _directory_rows(models, "model")
            if _agrees("model", _model_family(values["model"][0]), _model_family(row["name"]))
        ]
    except ValueError:
        return {"status": "provider_failed", "provider_outcome": "unparsed_response"}
    if not matching_models or len(matching_models) > MAX_MATCHED_MODELS:
        return {
            "status": "ambiguous_vehicle_modification" if matching_models else "no_catalog_vehicle",
            "missing_fields": ["series"],
            "choices": matching_models,
        }
    profiles = []
    for model in matching_models:
        cars = invoke(
            "getCars", provider_parameters={"makeId": make["id"], "modelId": model["id"], "carType": car_type}
        )
        if not cars.get("ok") or cars.get("dry_run"):
            return _provider_failure(cars)
        for raw in cars.get("vehicle_profiles") or []:
            if isinstance(raw, dict) and _id(raw.get("tecdoc_car_id")):
                if _reference_mismatch(raw, car_type=car_type, make_id=make["id"], model_id=model["id"]):
                    return {"status": "provider_failed", "provider_outcome": "catalogue_reference_mismatch"}
                profiles.append({"make": make["name"], "model": model["name"], "vehicle_type": car_type, **raw})
    # Preserve separate variants even when the provider repeats their ID.
    unique = {repr(sorted(profile.items(), key=lambda item: item[0])): profile for profile in profiles}
    profiles = list(unique.values())
    return {"status": "profiles_present" if profiles else "no_catalog_vehicle", "vehicle_profiles": profiles}


def _enrich_engines(
    profiles: list[dict[str, Any]], values: dict[str, list[Any]], car_type: str, invoke: Callable[..., dict[str, Any]]
) -> dict[str, Any]:
    eligible = [profile for profile in profiles if not _profile_conflicts(profile, values)]
    lacking_engine = [
        profile
        for profile in eligible
        if values["engine"] and any(_engine_comparison(value, profile) is None for value in values["engine"])
    ]
    if not lacking_engine:
        return {"status": "profiles_present", "vehicle_profiles": profiles}
    if len(lacking_engine) > MAX_ENGINE_LOOKUPS:
        return {"status": "ambiguous_vehicle_modification", "vehicle_profiles": eligible, "missing_fields": ["engine"]}
    enriched = [profile for profile in profiles if profile not in lacking_engine]
    for profile in lacking_engine:
        engine = invoke("engine_info", type_id=_id(profile["tecdoc_car_id"]), vehicle_type=car_type, lang_id=16)
        if not engine.get("ok") or engine.get("dry_run"):
            return _provider_failure(engine)
        details = [row for row in engine.get("vehicle_profiles") or [] if isinstance(row, dict)]
        if any(
            _reference_mismatch(
                row,
                car_type=car_type,
                tecdoc_car_id=_id(profile["tecdoc_car_id"]),
                make_id=_id(profile.get("make_id")),
                model_id=_id(profile.get("model_id")),
            )
            for row in details
        ):
            return {"status": "provider_failed", "provider_outcome": "catalogue_reference_mismatch"}
        # Different engine codes are separate alternatives, never merged.
        enriched.extend({**profile, **row, "tecdoc_car_id": profile["tecdoc_car_id"]} for row in details)
        if not details:
            enriched.append(profile)
    return {"status": "profiles_present", "vehicle_profiles": enriched}


def _matched_fields(profile: dict[str, Any], values: dict[str, list[Any]]) -> list[str]:
    fields = []
    for field, known in values.items():
        if not known:
            continue
        if field == "engine":
            confirmed = all(_engine_comparison(value, profile) is True for value in known)
        elif field in {"production_year", "production_date"}:
            confirmed = any(
                _present(profile.get(key))
                for key in ("production_date_from", "production_date_to", "production_year_from", "production_year_to")
            )
        else:
            keys = (field, field + "_from", field + "_to", "drive_type" if field == "drivetrain" else field)
            confirmed = any(_present(profile.get(key)) for key in keys)
        if confirmed:
            fields.append(field)
    return fields


def resolve_tecdoc_vehicle(
    identity: dict[str, Any],
    *,
    vehicle_type: str | None,
    call: Callable[..., dict[str, Any]],
    independent_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Navigate the catalogue without using its VINdecode as identity evidence.

    ``call(operation, **kwargs)`` is injected; this function owns no credentials
    or transport. A selected profile remains a catalogue candidate, never fitment.
    """
    result: dict[str, Any] = {
        "status": "needs_vehicle_context",
        "vehicle_profiles": [],
        "selected_profile": None,
        "missing_fields": [],
        "conflicting_fields": [],
        "matched_fields": [],
        "calls": [],
        "requires_exact_identifier_confirmation": True,
        "exact_applicability_confirmed": False,
        "identity_source": "independent_vehicle_context",
        "catalogue_route": "getMakes/getModels/getCars",
    }
    values = _independent_values(identity, independent_context)
    conflicts = _context_conflicts(identity, values)
    if conflicts:
        return {
            **result,
            "status": "conflict",
            "conflict_scope": "independent_vehicle",
            "conflicting_fields": conflicts,
        }
    invalid = _invalid_context_numbers(independent_context) + [
        field for field in _NUMBERS for value in values[field] if _number(value) is None
    ]
    if invalid:
        return {**result, "invalid_context_fields": invalid, "missing_fields": invalid}
    missing = [field for field in ("make", "model") if not values[field]]
    car_type = _vehicle_type(vehicle_type)
    if not car_type:
        missing.append("vehicle_type")
    if missing:
        return {**result, "missing_fields": missing}
    if car_type is None:
        return {**result, "missing_fields": ["vehicle_type"]}

    def invoke(operation: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = call(operation, **kwargs)
        except (OSError, ValueError, TimeoutError):
            response = {"ok": False, "outcome": "provider_failed"}
        if not isinstance(response, dict):
            response = {"ok": False, "outcome": "unparsed_response"}
        result["calls"].append(
            {
                "operation": operation,
                **{key: response.get(key) for key in ("ok", "outcome", "failure_class", "dry_run")},
            }
        )
        return response

    navigation = _navigate_catalogue(values, car_type, invoke)
    if navigation["status"] != "profiles_present":
        return {**result, **navigation}
    enrichment = _enrich_engines(navigation["vehicle_profiles"], values, car_type, invoke)
    if enrichment["status"] != "profiles_present":
        return {**result, **enrichment}
    profiles = enrichment["vehicle_profiles"]
    eligible = [profile for profile in profiles if not _profile_conflicts(profile, values)]
    if not eligible:
        rejected = [
            {"tecdoc_car_id": profile["tecdoc_car_id"], **conflict}
            for profile in profiles
            for conflict in _profile_conflicts(profile, values)
        ]
        return {
            **result,
            "status": "conflict",
            "conflict_scope": "catalogue_candidates",
            "vehicle_profiles": profiles,
            "conflicting_fields": rejected,
        }
    if len(eligible) > 1:
        return {
            **result,
            "status": "ambiguous_vehicle_modification",
            "vehicle_profiles": eligible,
            "choices": eligible,
            "missing_fields": _missing_discriminators(eligible, values),
        }
    selected = eligible[0]
    matched = _matched_fields(selected, values)
    technical = set(matched) - {"make", "model", "model_year"}
    unmatched = [
        field
        for field, known in values.items()
        if known and field not in matched and field not in {"model_year", "production_year", "production_date"}
    ]
    if not technical or unmatched:
        return {
            **result,
            "vehicle_profiles": eligible,
            "matched_fields": matched,
            "missing_fields": unmatched or ["engine", "power_hp", "production_year"],
        }
    unverified = [field for field in ("engine", "transmission", "drivetrain") if not values[field]]
    return {
        **result,
        "status": "matched",
        "vehicle_profiles": eligible,
        "selected_profile": selected,
        "matched_fields": matched,
        "missing_fields": unverified,
    }
