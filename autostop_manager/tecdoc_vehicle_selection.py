"""Select TecDoc candidates from independently supplied vehicle facts, without VINdecode."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import date
from email.utils import parsedate_to_datetime
from typing import Any

from .vehicle_identity import identity_values_agree
from .vehicle_identity_inputs import canonical_identity_field

MAX_MATCHED_MODELS = 8
MAX_ENGINE_LOOKUPS = 8
_CONTEXT_PROFILES = ("vehicle_profile", "vehicle_profile_compact", "crm_vehicle_profile")
_FIELDS = (
    "make",
    "model",
    "generation",
    "engine",
    "modification",
    "series",
    "transmission",
    "transmission_code",
    "transmission_speeds",
    "drivetrain",
    "transfer_case",
    "front_differential",
    "rear_differential",
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


def _engine_description_remainder(value: Any) -> str:
    """Retain everything not explained by the supported volume/fuel description."""
    if not _engine_description(value):
        return ""
    text = re.sub(r"\b\d+[.,]\d+(?:\s*L)?\b", " ", str(value), flags=re.I)
    text = re.sub(r"\b(?:TDI|FSI|TFSI|HDI|DIESEL|PETROL)\b", " ", text, flags=re.I)
    return text.strip(" \t\r\n()[]{}")


def _unresolved_engine_description(profile: dict[str, Any], known: list[Any]) -> bool:
    return any(_engine_description_remainder(value) and _engine_comparison(value, profile) is None for value in known)


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
    if False in compared:
        return False
    remainder = _engine_description_remainder(left)
    if remainder:
        # A residual token is not proof of an exact code. Only a supplied
        # catalogue code can confirm it; unknown prose must keep enrichment.
        if not re.fullmatch(r"[A-Z0-9]+(?:[.-][A-Z0-9]+)*", remainder, flags=re.I):
            return None
        if not _present(right) or _engine_description(right):
            return None
        if not any(identity_values_agree("engine", remainder, code.strip()) for code in re.split(r"[,;/]", str(right))):
            return None
        return True
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
    from .automotive_contracts import context_field_evidence

    values: dict[str, list[Any]] = {field: [] for field in _FIELDS}
    identity_evidence = list(identity.get("field_evidence") or [])
    context_evidence = context_field_evidence(context or {})
    evidence = [(row, True) for row in identity_evidence] + [(row, False) for row in context_evidence]
    for row, from_identity in evidence:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "")
        trust_value = row.get("trust")
        trust = trust_value if isinstance(trust_value, dict) else {}
        # CRM includes supplied client/photo facts. Local VIN patterns and the
        # catalogue being queried cannot establish characteristics of this car.
        trusted = (source.startswith("CRM") and trust.get("state") != "caller_supplied_unverified") or (
            source == "NHTSA vPIC"
            and from_identity
            and row.get("source_kind") != "caller"
            and row.get("bound") is not False
            and row.get("identifier_binding") not in {"mismatch", "unverified", "identifier_mismatch"}
            and not row.get("depends_on")
        )
        field = canonical_identity_field(row.get("field"))
        value = row.get("value")
        if trusted and isinstance(field, str) and field in values and _present(value) and value not in values[field]:
            values[field].append(value)
    return values


def _constraint_values(identity: dict[str, Any], context: dict[str, Any] | None) -> dict[str, list[Any]]:
    """Keep caller claims as negative constraints without treating them as independent proof."""
    from .automotive_contracts import context_field_evidence

    values = _independent_values(identity, context)
    evidence = [
        row
        for row in identity.get("field_evidence") or []
        if isinstance(row, dict) and row.get("source_kind") == "caller"
    ]
    evidence.extend(context_field_evidence(context or {}))
    for row in evidence:
        field = canonical_identity_field(row.get("field"))
        value = row.get("value")
        if isinstance(field, str) and field in values and _present(value) and value not in values[field]:
            values[field].append(value)
    return values


def _invalid_context_numbers(context: dict[str, Any] | None) -> list[str]:
    invalid = set()
    for row in _context_rows(context or {}):
        for key, value in row.items():
            field = canonical_identity_field(key)
            if field in _NUMBERS and value is not None and value != "" and _number(value) is None:
                invalid.add(field)
    return sorted(invalid)


def _context_conflicts(identity: dict[str, Any], values: dict[str, list[Any]]) -> list[dict[str, Any]]:
    conflicts = [
        {"field": field, "identity": candidates, "source": "vehicle_context_constraints"}
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


def _id(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text.isascii() or not text.isdigit():
        return None
    try:
        identifier = int(text)
    except ValueError:
        return None
    return identifier if identifier > 0 else None


def _directory_aliases(
    raw: dict[str, Any], id_keys: tuple[str, ...], name_keys: tuple[str, ...]
) -> tuple[list[int | None], dict[str, str], list[str]]:
    identifiers: list[int | None] = []
    invalid_id = False
    for key in dict.fromkeys(id_keys):
        value = raw.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        identifier = _id(value)
        if identifier is None:
            invalid_id = True
        elif identifier not in identifiers:
            identifiers.append(identifier)
    names: dict[str, str] = {}
    invalid_name = False
    has_name_field = any(key in raw for key in name_keys)
    for key in dict.fromkeys(name_keys):
        value = raw.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            invalid_name = True
            continue
        normalized = " ".join(str(value).split()).casefold()
        if normalized:
            names.setdefault(normalized, str(value))
    errors = []
    if invalid_id or not identifiers:
        errors.append("invalid_catalogue_reference_id")
    if len(identifiers) > 1:
        errors.append("conflicting_catalogue_reference_ids")
    if invalid_name or (has_name_field and not names):
        errors.append("invalid_catalogue_reference_name")
    return identifiers or [None], names, errors


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
    rows: dict[tuple[int | None, str], dict[str, Any]] = {}
    unsafe_references: dict[int, set[str]] = {}
    for raw in _records(call.get("payload")):
        identifiers, names, errors = _directory_aliases(raw, id_keys, name_keys)
        for identifier in identifiers:
            if identifier is not None and errors:
                unsafe_references.setdefault(identifier, set()).update(errors)
            for normalized_name, name in names.items():
                row = rows.setdefault((identifier, normalized_name), {"id": identifier, "name": name})
                if errors:
                    row["reference_errors"] = sorted(set(row.get("reference_errors", [])) | set(errors))
    for row in rows.values():
        reference_errors = unsafe_references.get(row["id"])
        if reference_errors:
            row["reference_errors"] = sorted(set(row.get("reference_errors", [])) | reference_errors)
    return list(rows.values())


def _conflicting_directory_references(
    directory: list[dict[str, Any]], selected: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep matching references and all their names if any selected ID conflicts."""
    selected_ids = {row["id"] for row in selected if row["id"] is not None}
    counts: dict[int, int] = {}
    for row in directory:
        if row["id"] in selected_ids:
            counts[row["id"]] = counts.get(row["id"], 0) + 1
    ambiguous_ids = {identifier for identifier, count in counts.items() if count > 1}
    unsafe_alias = any(row.get("reference_errors") for row in selected) or any(
        row.get("reference_errors") for row in directory if row["id"] in selected_ids
    )
    if not ambiguous_ids and not unsafe_alias:
        return []
    return [row for row in directory if row["id"] in selected_ids or (row["id"] is None and row in selected)]


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
        "generation",
        "engine",
        "displacement_cc",
        "power_hp",
        "power_kw",
        "production_year",
        "transmission",
        "transmission_code",
        "drivetrain",
        "transfer_case",
        "front_differential",
        "rear_differential",
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
        directory_makes = _directory_rows(makes, "make")
        matching_makes = [row for row in directory_makes if _agrees("make", values["make"][0], row["name"])]
    except ValueError:
        return {"status": "provider_failed", "provider_outcome": "unparsed_response"}
    conflicting_makes = _conflicting_directory_references(directory_makes, matching_makes)
    if conflicting_makes:
        return {
            "status": "ambiguous_vehicle_modification",
            "provider_outcome": "ambiguous_catalogue_reference_aliases"
            if any(row.get("reference_errors") for row in conflicting_makes)
            else "conflicting_catalogue_reference_names",
            "missing_fields": ["make"],
            "choices": conflicting_makes,
        }
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
        directory_models = _directory_rows(models, "model")
        matching_models = [
            row
            for row in directory_models
            if _agrees("model", _model_family(values["model"][0]), _model_family(row["name"]))
        ]
    except ValueError:
        return {"status": "provider_failed", "provider_outcome": "unparsed_response"}
    conflicting_models = _conflicting_directory_references(directory_models, matching_models)
    if conflicting_models:
        return {
            "status": "ambiguous_vehicle_modification",
            "provider_outcome": "ambiguous_catalogue_reference_aliases"
            if any(row.get("reference_errors") for row in conflicting_models)
            else "conflicting_catalogue_reference_names",
            "missing_fields": ["series"],
            "choices": conflicting_models,
        }
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


def _selection_dependencies(
    profiles: list[dict[str, Any]],
    selected: dict[str, Any],
    identity: dict[str, Any],
    values: dict[str, list[Any]],
    constraints: dict[str, list[Any]],
    *,
    trusted_provider_evidence: bool,
) -> list[str]:
    """Disclose caller facts which eliminated otherwise eligible catalog alternatives."""
    verified: dict[str, list[Any]] = {field: [] for field in _FIELDS}
    identity_trust = identity.get("trust")
    caller_dossier = isinstance(identity_trust, dict) and identity_trust.get("state") == "caller_supplied_unverified"
    for row in identity.get("field_evidence") or []:
        if (
            not trusted_provider_evidence
            or caller_dossier
            or not isinstance(row, dict)
            or row.get("source") != "NHTSA vPIC"
            or row.get("source_kind") != "provider"
            or row.get("bound") is not True
            or row.get("independent") is not True
            or row.get("identifier_binding") != "exact"
            or row.get("strength") != "supported"
            or row.get("depends_on")
        ):
            continue
        # A fresh decoder result uses canonical field names. Public aliases do
        # not by themselves establish that a provider supplied the value.
        field = row.get("field")
        if isinstance(field, str) and field in verified and _present(row.get("value")):
            verified[field].append(row["value"])

    def corroborated(field: str, value: Any) -> bool:
        return any(
            _number(value) is not None and _number(value) == _number(claim)
            if field in _NUMBERS
            else " ".join(str(value).split()).casefold() == " ".join(str(claim).split()).casefold()
            for claim in verified[field]
        )

    unverified = {
        field: [value for value in known if not corroborated(field, value)] for field, known in constraints.items()
    }
    fields: set[str] = set()
    for profile in profiles:
        if _profile_conflicts(profile, verified):
            continue
        fields.update(row["field"] for row in _profile_conflicts(profile, unverified))
    verified_values = {
        field: [value for value in known if corroborated(field, value)] for field, known in values.items()
    }
    unverified_values = {
        field: [value for value in known if not corroborated(field, value)] for field, known in values.items()
    }
    fields.update(field for field in ("make", "model") if values[field] and not verified_values[field])
    if not set(_matched_fields(selected, verified_values)) - {"make", "model", "model_year"}:
        fields.update(set(_matched_fields(selected, unverified_values)) - {"make", "model", "model_year"})
    return sorted(fields)


def resolve_tecdoc_vehicle(
    identity: dict[str, Any],
    *,
    vehicle_type: str | None,
    call: Callable[..., dict[str, Any]],
    independent_context: dict[str, Any] | None = None,
    trusted_provider_evidence: bool = False,
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
        "selection_depends_on_unverified_fields": [],
        "warnings": [],
        "calls": [],
        "requires_exact_identifier_confirmation": True,
        "exact_applicability_confirmed": False,
        "caller_context_independently_verified": False,
        "identity_source": "independent_vehicle_context",
        "catalogue_route": "getMakes/getModels/getCars",
    }
    values = _independent_values(identity, independent_context)
    constraints = _constraint_values(identity, independent_context)
    conflicts = _context_conflicts(identity, constraints)
    if conflicts:
        return {
            **result,
            "status": "conflict",
            "conflict_scope": "independent_vehicle",
            "conflicting_fields": conflicts,
        }
    invalid = _invalid_context_numbers(independent_context) + [
        field for field in _NUMBERS for value in constraints[field] if _number(value) is None
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
    enrichment = _enrich_engines(navigation["vehicle_profiles"], constraints, car_type, invoke)
    if enrichment["status"] != "profiles_present":
        return {**result, **enrichment}
    profiles = enrichment["vehicle_profiles"]
    eligible = [profile for profile in profiles if not _profile_conflicts(profile, constraints)]
    if not eligible:
        rejected = [
            {"tecdoc_car_id": profile["tecdoc_car_id"], **conflict}
            for profile in profiles
            for conflict in _profile_conflicts(profile, constraints)
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
    if _unresolved_engine_description(selected, constraints["engine"]) and "engine" not in unmatched:
        unmatched.append("engine")
    if not technical or unmatched:
        return {
            **result,
            "vehicle_profiles": eligible,
            "matched_fields": matched,
            "missing_fields": unmatched or ["engine", "power_hp", "production_year"],
        }
    unverified = [field for field in ("engine", "transmission", "drivetrain") if not values[field]]
    unverified.extend(
        field for field, claimed in constraints.items() if claimed and not values[field] and field not in unverified
    )
    dependencies = _selection_dependencies(
        profiles, selected, identity, values, constraints, trusted_provider_evidence=trusted_provider_evidence
    )
    warnings = (
        [
            "Выбор каталожного кандидата зависит от неподтверждённых характеристик: "
            + ", ".join(dependencies)
            + ". Уточните их; применимость к VIN и оригинальный артикул не подтверждены."
        ]
        if dependencies
        else []
    )
    return {
        **result,
        "status": "matched",
        "vehicle_profiles": eligible,
        "selected_profile": selected,
        "matched_fields": matched,
        "missing_fields": unverified,
        "selection_depends_on_unverified_fields": dependencies,
        "warnings": warnings,
    }
