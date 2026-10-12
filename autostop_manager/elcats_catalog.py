"""Independent profile-based navigation of registered public passenger catalogs."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .automotive_contracts import MAX_ROWS, _public_ref_identity, identity_errors, public_oem_catalog_ref, result
from .elcats_number_ocr import read_number_ocr
from .parts_intent import normalize_part_intent
from .public_catalog_http import CatalogReadError, PublicCatalogReader, safe_catalog_url


_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "docs/agent/elcats_catalog_registry.json"
_OPERATIONS = {"resolve_vehicle", "list_groups", "list_diagrams", "list_parts", "lookup_candidates"}
_PROFILE_FIELDS = (
    "make",
    "model",
    "model_year",
    "year",
    "market",
    "production_date",
    "engine",
    "engine_code",
    "transmission",
    "transmission_code",
    "drivetrain",
    "body",
    "body_type",
    "steering",
    "model_code",
    "pr_codes",
    "pr_codes_complete",
)
_READ_ERRORS = {
    "robots_disallowed": "permission_error",
    "robots_unavailable": "transport_error",
    "catalog_auth_required": "permission_error",
    "catalog_rate_limited": "quota_error",
    "catalog_provider_error": "provider_error",
    "catalog_transport_failed": "transport_error",
    "catalog_timeout": "transport_error",
    "catalog_dns_failed": "transport_error",
}


def elcats_catalog_entries() -> list[dict[str, Any]]:
    document = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    return [dict(entry) for entry in document["entries"]]


def elcats_catalog_status() -> dict[str, Any]:
    document = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    entries = elcats_catalog_entries()
    counts: dict[str, int] = {}
    for entry in entries:
        state = str(entry.get("readiness", "not_commissioned"))
        counts[state] = counts.get(state, 0) + 1
    return {
        "enabled": os.environ.get("AUTOSTOP_ELCATS_ENABLED", "").casefold() in {"1", "true", "yes", "on"},
        "registry_version": document["registry_version"],
        "entries": entries,
        "counts": counts,
        "network_calls": 0,
    }


def _compact(value: Any) -> str:
    return re.sub(r"[^\w]", "", str(value).casefold())


def _profile(identity: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    data: dict[str, Any] = identity["data"] if isinstance(identity.get("data"), dict) else identity
    errors = identity_errors(identity, data)
    # A family/group match can support family navigation, without upgrading its
    # provider binding into exact-identifier applicability.
    if data.get("ready_for_family_lookup") is True:
        errors = [error for error in errors if error.get("code") != "provider_identifier_unverified"]
    supplied = data.get("vehicle_profile")
    if not isinstance(supplied, dict):
        return {}, errors, ["vehicle_identity.vehicle_profile"]
    profile = {key: supplied[key] for key in _PROFILE_FIELDS if supplied.get(key) not in (None, "", [])}
    missing = [
        "vehicle_profile." + key
        for key in ("make", "model")
        if not isinstance(profile.get(key), str) or not profile[key].strip()
    ]
    if any(not isinstance(value, (str, int, bool, list)) for value in profile.values()):
        missing.append("vehicle_profile.scalar_fields")
    # The only values crossing the public-reader boundary are registered paths
    # and published catalog IDs. Fail closed even on VIN accidentally in a name.
    try:
        serialized = json.dumps(profile, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return {}, errors, [*missing, "vehicle_profile.serializable_fields"]
    if len(serialized) > 16384 or re.search(r"\b[A-HJ-NPR-Z0-9]{17}\b", serialized, re.I):
        missing.append("deidentified_vehicle_profile")
    return profile, errors, missing


def _part(item: dict[str, Any] | None) -> tuple[dict[str, Any] | None, list[str]]:
    if item is None:
        return None, []
    intent = normalize_part_intent(
        str(item.get("raw") or item.get("name") or ""),
        axle=item.get("axle"),
        side=item.get("side"),
        position=item.get("position"),
    )
    if intent["intent_id"] in {"unknown", "multiple_parts", "brake_pads_multiple_axles"}:
        return intent, ["part_request_item.single_recognized_part"]
    return intent, ["part_request_item." + field for field in intent.get("clarification_fields", [])]


def _route(entry: dict[str, Any], url: str, *, image: bool = False) -> str:
    safe = safe_catalog_url(urljoin(entry["entry_url"], url))
    parsed = urlsplit(safe)
    origin = urlsplit(entry["entry_url"])
    if parsed.hostname != origin.hostname or parsed.scheme != "https":
        raise CatalogReadError("unsafe_catalog_route")
    path = parsed.path.casefold()
    if parsed.hostname == "ssangyong.exist.ru":
        allowed = {"/", "/group.aspx", "/unit.aspx", "/parts.aspx"}
        if image:
            allowed = {"/pcode.ashx"}
        if path not in allowed:
            raise CatalogReadError("unsafe_catalog_route")
    elif not path.startswith(origin.path.casefold()):
        raise CatalogReadError("unsafe_catalog_route")
    if any(token in path for token in ("price", "login", "logout", "cart", "captcha", "ajax")):
        raise CatalogReadError("unsafe_catalog_route")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if len(query) > 32 or len({key.casefold() for key, _ in query}) != len(query):
        raise CatalogReadError("unsafe_catalog_route")
    if any(
        re.search(r"vin|fin$|frame|token|session|password|viewstate|eventvalidation", key, re.I) for key, _ in query
    ):
        raise CatalogReadError("unsafe_catalog_route")
    if parsed.hostname == "ssangyong.exist.ru":
        allowed_keys = {"code"} if image else {"model", "group", "subgroup", "unit", "title"}
        if any(key.casefold() not in allowed_keys for key, _ in query):
            raise CatalogReadError("unsafe_catalog_route")
    return safe


def _ref(
    entry: dict[str, Any],
    profile: dict[str, Any],
    row: dict[str, Any],
    kind: str,
    parent: dict[str, Any] | None,
) -> dict[str, Any]:
    url = _route(entry, str(row.get("url") or entry["entry_url"]))
    parsed = urlsplit(url)
    reference = {
        "provider": entry["provider"],
        "namespace": entry["namespace"],
        "entry_id": entry["id"],
        "entity_kind": kind,
        "id": str(row.get("id") or hashlib.sha256(url.encode()).hexdigest()[:24]),
        "vehicle_context": profile,
        "path": parsed.path,
        "parameters": dict(parse_qsl(parsed.query, keep_blank_values=True)),
    }
    if parent is not None:
        reference["parent_ref"] = parent
    return reference


def _ref_url(entry: dict[str, Any], reference: dict[str, Any], profile: dict[str, Any]) -> str:
    kind = reference.get("entity_kind")
    if not isinstance(kind, str) or not public_oem_catalog_ref(reference, entity_kind=kind, vehicle_profile=profile):
        raise CatalogReadError("invalid_catalog_ref")
    cursor: dict[str, Any] | None = reference
    while cursor is not None:
        if cursor.get("entry_id") != entry["id"] or cursor.get("provider") != entry["provider"]:
            raise CatalogReadError("invalid_catalog_ref")
        path, parameters = cursor.get("path"), cursor.get("parameters")
        if not isinstance(path, str) or not path.startswith("/") or not isinstance(parameters, dict):
            raise CatalogReadError("invalid_catalog_ref")
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in parameters.items()):
            raise CatalogReadError("invalid_catalog_ref")
        parent = cursor.get("parent_ref")
        if isinstance(parent, dict):
            parent_parameters = parent.get("parameters", {})
            if any(
                parameters.get(key)
                and parent_parameters.get(key)
                and parameters[key].casefold() != parent_parameters[key].casefold()
                for key in ("Model", "Mdl")
            ):
                raise CatalogReadError("invalid_catalog_ref")
        origin = urlsplit(entry["entry_url"])
        _route(entry, urlunsplit((origin.scheme, origin.netloc, path, urlencode(parameters), "")))
        cursor = cursor.get("parent_ref")
    origin = urlsplit(entry["entry_url"])
    return _route(
        entry, urlunsplit((origin.scheme, origin.netloc, reference["path"], urlencode(reference["parameters"]), ""))
    )


def _model_matches(row: dict[str, Any], profile: dict[str, Any]) -> bool:
    label = _compact(row.get("label") or row.get("name") or "")
    model = _compact(profile["model"])
    model_match = model in label or label == _compact(profile.get("model_code", ""))
    if not model_match:
        return False
    return _context_matches(row, profile)


def _context_matches(row: dict[str, Any], profile: dict[str, Any]) -> bool:
    context = {**row, **row.get("vehicle_context", {})}
    return not any(
        context.get(field) and profile.get(field) and _compact(context[field]) != _compact(profile[field])
        for field in ("engine", "engine_code", "transmission", "market", "body", "drivetrain", "model_year")
    )


def _group_matches(row: dict[str, Any], intent: dict[str, Any]) -> bool:
    label = str(row.get("label") or row.get("name") or "").casefold()
    if intent["intent_id"] in {"front_brake_pads", "rear_brake_pads"} and re.search(r"парков|стояноч|parking", label):
        return False
    terms = intent.get("catalog_group_terms", [])
    if any(str(term).casefold() in label for term in terms):
        return True
    roots = {token[:5] for term in terms for token in re.findall(r"[a-zа-я]{4,}", str(term).casefold())}
    roots -= {"перед", "задни", "систе", "syste", "front", "rear", "детал", "parts", "left", "right"}
    return any(root in label for root in roots)


def _part_matches(row: dict[str, Any], intent: dict[str, Any]) -> bool:
    name = str(row.get("name") or row.get("label") or "")
    parsed = normalize_part_intent(name)
    wanted, observed = intent["intent_id"], parsed["intent_id"]
    if "brake_pads" in wanted or wanted in {"front_brake_pads", "rear_brake_pads"}:
        return "brake_pads" in observed or bool(
            re.search(r"\bpad\s*(?:set|kit)|\bset[- ]?(?:frt|rr)?\s*brake", name, re.I)
        )
    return wanted == observed


def _placement_conflict(row: dict[str, Any], intent: dict[str, Any]) -> bool:
    context = {**intent.get("inferred_position_context", {}), **intent.get("explicit_position_context", {})}
    return any(
        row.get(key) and context.get(key) and row[key] != context[key] for key in ("axle", "side", "inner_outer")
    )


class _Navigation:
    def __init__(self, reader: PublicCatalogReader, profile: dict[str, Any], intent: dict[str, Any] | None) -> None:
        self.reader = reader
        self.profile = profile
        self.intent = intent
        self.evidence: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.pending: list[dict[str, Any]] = []
        self.candidates: list[dict[str, Any]] = []
        self.nodes: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.excluded: list[dict[str, Any]] = []

    def page(self, entry: dict[str, Any], url: str, operation: str) -> dict[str, Any]:
        from .elcats_parsers import parse_catalog_html

        if time.monotonic() >= self.reader.deadline:
            raise CatalogReadError("catalog_deadline_exceeded")
        page = self.reader.read(_route(entry, url), route_guard=lambda target: _route(entry, target))
        if time.monotonic() >= self.reader.deadline:
            raise CatalogReadError("catalog_deadline_exceeded")
        parsed = parse_catalog_html(page.text, url=page.url, operation=operation, entry=entry)
        self.evidence.append(
            {
                "provider": entry["provider"],
                "primary_lineage": entry["provider"],
                "method": "public_html_catalog",
                "fetched_at": datetime.now(UTC).isoformat(),
                "locator": page.url,
                "scope": "family",
                "identifier_binding": {"status": "family", "verified": False},
            }
        )
        self.warnings.extend(parsed.get("warnings", []))
        flags = [flag for flag in parsed.get("access_flags", []) if flag != "rendered_evidence_required"]
        if flags:
            raise CatalogReadError(str(flags[0]))
        if not parsed.get("rows") and "no_supported_public_catalog_rows" in parsed.get("warnings", []):
            raise CatalogReadError("unsupported_structure")
        return parsed

    def vehicles(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        queue: list[tuple[str, int, dict[str, Any]]] = [(entry["entry_url"], 0, {})]
        seen = set()
        selected = []
        branches = 0
        while queue:
            url, depth, selection = queue.pop(0)
            key = (url, self.context_key(selection))
            if key in seen:
                continue
            seen.add(key)
            parsed = self.page(entry, url, "resolve_vehicle")
            rows = parsed.get("rows", [])
            if parsed.get("page_kind") in {"groups", "group", "diagrams"}:
                node = self.child(
                    entry, None, {"url": self.evidence[-1]["locator"]}, "modification", selection=selection
                )
                selected.append(node["catalog_ref"])
                continue
            matching = [row for row in rows if _model_matches(row, self.profile)]
            if depth and not matching:
                # A selected model may then publish modifications with engine/
                # year labels. Keep all branches; never silently pick the first.
                matching = [row for row in rows if row.get("entity_kind") in {"modification", "selection"}]
                matching = [row for row in matching if _context_matches(row, self.profile)]
            for row in matching:
                if branches >= MAX_ROWS:
                    self.errors.append("vehicle_selection_branch_budget")
                    self.pending.append(
                        {"entry_id": entry["id"], "url": url, "reason": "vehicle_selection_branch_budget"}
                    )
                    break
                branches += 1
                destination = _route(entry, row["url"])
                if row.get("page_kind") in {"groups", "group"} or entry["provider"] == "exist_ssangyong_catalog":
                    node = self.child(entry, None, row, "modification", selection=selection)
                    selected.append(node["catalog_ref"])
                elif depth < 2:
                    _node, context = self.carry(row, selection, binding="vehicle_selection")
                    queue.append((destination, depth + 1, context))
                else:
                    self.errors.append("vehicle_selection_depth")
                    self.pending.append(
                        {"entry_id": entry["id"], "url": destination, "reason": "vehicle_selection_depth"}
                    )
            if not matching:
                self.warnings.append("vehicle_model_not_found:" + entry["id"])
        return selected

    @staticmethod
    def context_key(context: dict[str, Any]) -> str:
        state = {
            key: context.get(key, {}) if key == "placement_context" else context.get(key, [])
            for key in ("inherited_conditions", "inherited_restrictions", "placement_context")
        }
        return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()

    def carry(
        self,
        row: dict[str, Any],
        inherited: dict[str, Any],
        *,
        binding: str,
        reference: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        node = dict(row)
        configuration = row.get("vehicle_context", {})
        conditions = {
            key: configuration[key]
            for key in _PROFILE_FIELDS
            if key not in {"make", "model", "pr_codes", "pr_codes_complete"}
            and configuration.get(key) not in (None, "", [])
        }
        conditions.update(row.get("conditions", {}))
        node["conditions"] = conditions
        node["unparsed_conditions"] = list(dict.fromkeys(row.get("unparsed_conditions", [])))
        node["inherited_restrictions"] = list(
            dict.fromkeys([*inherited.get("inherited_restrictions", []), *row.get("inherited_restrictions", [])])
        )
        node["inherited_conditions"] = [
            *inherited.get("inherited_conditions", []),
            *row.get("inherited_conditions", []),
        ]
        for coordinate, value in inherited.get("placement_context", {}).items():
            node.setdefault(coordinate, value)
        context: dict[str, Any] = {}
        context["inherited_restrictions"] = list(
            dict.fromkeys(
                [
                    *node["inherited_restrictions"],
                    *node["unparsed_conditions"],
                    *row.get("unparsed_restrictions", []),
                ]
            )
        )
        context["inherited_conditions"] = [
            *node["inherited_conditions"],
            *(
                [
                    {
                        "conditions": conditions,
                        "raw_conditions": row.get("raw_conditions"),
                        "unparsed_conditions": list(
                            dict.fromkeys([*node["unparsed_conditions"], *row.get("unparsed_restrictions", [])])
                        ),
                        "source_url": self.evidence[-1]["locator"],
                        "binding": binding,
                        **(
                            {"catalog_ref": _public_ref_identity(reference)}
                            if reference is not None
                            else {
                                "selection_url": row.get("url"),
                                "row_id": row.get("id"),
                                "scope": "family",
                                "identifier_verified": False,
                            }
                        ),
                    }
                ]
                if conditions or node["unparsed_conditions"] or row.get("unparsed_restrictions")
                else []
            ),
        ]
        context["placement_context"] = {key: node[key] for key in ("axle", "side", "inner_outer") if node.get(key)}
        return node, context

    def child(
        self,
        entry: dict[str, Any],
        parent: dict[str, Any] | None,
        row: dict[str, Any],
        kind: str,
        *,
        selection: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        reference = _ref(entry, self.profile, row, kind, parent)
        node, context = self.carry(row, parent or selection or {}, binding=kind, reference=reference)
        reference.update(context)
        node["catalog_ref"] = reference
        return node

    def children(self, entry: dict[str, Any], parent: dict[str, Any], operation: str) -> list[dict[str, Any]]:
        url = _ref_url(entry, parent, self.profile)
        parsed = self.page(entry, url, operation)
        expected = {"list_groups": "group", "list_diagrams": "diagram", "list_parts": "part"}[operation]
        if operation == "list_diagrams" and parsed.get("page_kind") == "parts":
            actual_url = self.evidence[-1]["locator"]
            parameters = dict(parse_qsl(urlsplit(actual_url).query))
            row = {
                "url": actual_url,
                "id": parameters.get("Unit") or parameters.get("SubId"),
                "name": parameters.get("Title") or "catalog_single_diagram",
                "entity_kind": "diagram",
            }
            return [self.child(entry, parent, row, "diagram")]
        nodes = []
        for row in parsed.get("rows", []):
            kind = row.get("entity_kind", expected)
            if kind == "selection":
                kind = expected
            if kind not in {"group", "diagram", "part"}:
                continue
            if (kind == "diagram" and parent["entity_kind"] != "group") or (
                kind == "part" and parent["entity_kind"] != "diagram"
            ):
                raise CatalogReadError("unsupported_catalog_hierarchy")
            nodes.append(self.child(entry, parent, row, kind))
        return nodes

    def candidate(self, entry: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
        candidate = dict(node)
        modification = node["catalog_ref"]
        while modification.get("parent_ref") is not None:
            modification = modification["parent_ref"]
        candidate.update(
            {
                "provider": entry["provider"],
                "namespace": entry["namespace"],
                "brand": node.get("brand") or entry["brand"],
                "oem_confirmed": False,
                "fitment_confirmed": False,
                "scope": "modification",
                "lookup_method": "decoded_vehicle_profile",
                "source": dict(self.evidence[-1]),
                "modification_ref": modification,
            }
        )
        candidate["source"]["catalog_ref"] = modification
        if not candidate.get("raw_number") and node.get("number_image_url"):
            try:
                image = self.reader.read_number_image(_route(entry, node["number_image_url"], image=True))
                observed = read_number_ocr(image.body, deadline=self.reader.deadline)
                candidate.update(observed)
                candidate["number_image_url"] = image.url
                candidate["source"].update({"method": observed["method"], "number_image_locator": image.url})
                if not observed["raw_number"]:
                    self.warnings.append("catalog_ocr_inconclusive")
            except CatalogReadError as exc:
                candidate["number_extraction_reason"] = exc.code
                self.warnings.append(exc.code)
                self.pending.append({"catalog_ref": node["catalog_ref"], "reason": exc.code})
        raw = candidate.get("raw_number")
        if raw and not candidate.get("normalized_number"):
            candidate["normalized_number"] = re.sub(r"[^A-Z0-9]", "", str(raw).upper())
        if not raw:
            candidate["status"] = "number_unavailable"
        else:
            candidate.setdefault("status", "public_catalog_candidate_unverified")
        candidate["source"].update(
            {
                "scope": "modification",
                "part_number": candidate.get("normalized_number"),
                "brand": candidate["brand"],
                "conditions": candidate.get("conditions", {}),
                "unparsed_conditions": candidate.get("unparsed_conditions", []),
                "inherited_restrictions": candidate.get("inherited_restrictions", []),
                "inherited_conditions": candidate.get("inherited_conditions", []),
                "identifier_verified": False,
            }
        )
        return candidate

    def lookup(self, entry: dict[str, Any], modifications: list[dict[str, Any]]) -> None:
        assert self.intent is not None
        queue = [(modification, "list_groups", 0) for modification in modifications]
        scheduled = len(queue)
        seen = set()
        while queue:
            reference, operation, depth = queue.pop(0)
            key = (
                reference["path"],
                json.dumps(reference["parameters"], sort_keys=True),
                operation,
                self.context_key(reference),
            )
            if key in seen:
                continue
            seen.add(key)
            try:
                nodes = self.children(entry, reference, operation)
            except CatalogReadError as exc:
                self.pending.append({"catalog_ref": reference, "reason": exc.code})
                self.errors.append(exc.code)
                continue
            for node in nodes:
                kind = node["catalog_ref"]["entity_kind"]
                if kind == "part":
                    if _part_matches(node, self.intent):
                        if _placement_conflict(node, self.intent):
                            self.excluded.append(
                                {"catalog_ref": node["catalog_ref"], "reason": "requested_position_conflict"}
                            )
                        else:
                            self.candidates.append(self.candidate(entry, node))
                elif _group_matches(node, self.intent) and not _placement_conflict(node, self.intent):
                    if depth < 5:
                        if scheduled >= MAX_ROWS:
                            self.errors.append("navigation_branch_budget")
                            self.pending.append(
                                {"catalog_ref": node["catalog_ref"], "reason": "navigation_branch_budget"}
                            )
                            break
                        scheduled += 1
                        next_op = "list_parts" if kind == "diagram" else "list_diagrams"
                        queue.append((node["catalog_ref"], next_op, depth + 1))
                    else:
                        self.pending.append({"catalog_ref": node["catalog_ref"], "reason": "navigation_depth"})


def elcats_catalog_query(
    operation: Literal["resolve_vehicle", "list_groups", "list_diagrams", "list_parts", "lookup_candidates"],
    vehicle_identity: dict[str, Any],
    part_request_item: dict[str, Any] | None = None,
    catalog_ref: dict[str, Any] | None = None,
    page_budget: int = 12,
) -> dict[str, Any]:
    """Read one operation using a decoded, de-identified vehicle profile; no VIN API."""
    tool_id = "manager.elcats_catalog_query." + str(operation)
    if (
        not isinstance(operation, str)
        or operation not in _OPERATIONS
        or type(page_budget) is not int
        or not 1 <= page_budget <= 24
    ):
        return result(tool_id, "invalid_input", {}, missing_fields=["operation_or_page_budget"])
    if (
        not isinstance(vehicle_identity, dict)
        or (part_request_item is not None and not isinstance(part_request_item, dict))
        or (catalog_ref is not None and not isinstance(catalog_ref, dict))
        or (
            part_request_item is not None
            and any(
                part_request_item.get(key) is not None and not isinstance(part_request_item[key], str)
                for key in ("raw", "name", "axle", "side", "position")
            )
        )
    ):
        return result(tool_id, "invalid_input", {}, missing_fields=["vehicle_identity_or_part_request_item"])
    profile, conflicts, missing = _profile(vehicle_identity)
    intent, part_missing = _part(part_request_item)
    if operation == "lookup_candidates":
        missing.extend(part_missing or ([] if intent is not None else ["part_request_item"]))
    if conflicts or missing:
        return result(tool_id, "invalid_input", {}, conflicts=conflicts, missing_fields=missing)
    entries = [
        entry
        for entry in elcats_catalog_entries()
        if _compact(profile["make"])
        in {
            _compact(entry["brand"]),
            *(_compact(alias) for alias in entry.get("aliases", [])),
        }
    ]
    if catalog_ref is not None:
        entries = [entry for entry in entries if entry["id"] == catalog_ref.get("entry_id")]
        expected = {"list_groups": "modification", "list_diagrams": "group", "list_parts": "diagram"}.get(operation)
        try:
            if not entries or (expected and catalog_ref.get("entity_kind") != expected):
                raise CatalogReadError("invalid_catalog_ref")
            _ref_url(entries[0], catalog_ref, profile)
        except CatalogReadError:
            return result(tool_id, "invalid_input", {}, missing_fields=["catalog_ref"])
    elif operation.startswith("list_"):
        return result(tool_id, "invalid_input", {}, missing_fields=["catalog_ref"])
    if not entries:
        return result(
            tool_id, "unsupported", {"reason": "passenger_catalog_not_registered", "nodes": [], "candidates": []}
        )
    status = elcats_catalog_status()
    if not status["enabled"]:
        return result(
            tool_id,
            "configuration_error",
            {
                "reason": "feature_disabled",
                "registry_version": status["registry_version"],
                "entries": [{key: entry.get(key) for key in ("id", "readiness", "reason")} for entry in entries],
            },
        )
    reader = PublicCatalogReader(page_budget=page_budget, deadline_seconds=45)
    navigation = _Navigation(reader, profile, intent)
    for entry in entries:
        try:
            if operation == "resolve_vehicle":
                navigation.nodes.extend(navigation.vehicles(entry))
            elif operation == "lookup_candidates":
                selected = [catalog_ref] if catalog_ref is not None else navigation.vehicles(entry)
                navigation.lookup(entry, selected)
            else:
                assert catalog_ref is not None
                children = navigation.children(entry, catalog_ref, operation)
                if operation == "list_parts":
                    navigation.candidates.extend(navigation.candidate(entry, node) for node in children)
                else:
                    navigation.nodes.extend(children)
        except CatalogReadError as exc:
            navigation.errors.append(exc.code)
            navigation.pending.append({"entry_id": entry["id"], "reason": exc.code})
    found = bool(navigation.nodes or navigation.candidates)
    complete = not navigation.pending
    outcome = "success" if found and complete else "partial" if found else "empty"
    if navigation.errors and not found:
        outcome = _READ_ERRORS.get(navigation.errors[0], "partial")
    if any(not candidate.get("raw_number") for candidate in navigation.candidates):
        outcome = "partial"
    data = {
        "operation": operation,
        "registry_version": status["registry_version"],
        "vehicle_profile": profile,
        "scope": "vehicle_profile_catalog",
        "nodes": navigation.nodes,
        "candidates": navigation.candidates,
        "coverage": {"complete": complete, "remaining_branches": navigation.pending},
        "reasons": list(dict.fromkeys(navigation.errors)),
        "excluded_candidates": navigation.excluded,
    }
    return result(
        tool_id,
        outcome,
        data,
        evidence=navigation.evidence,
        warnings=list(dict.fromkeys(navigation.warnings)),
        missing_fields=["catalog_number"] if any(not row.get("raw_number") for row in navigation.candidates) else [],
        network_calls=reader.network_calls,
        attempts=reader.attempts,
    )
