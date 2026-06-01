from __future__ import annotations

import hashlib
import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit, parse_qsl
from urllib.request import Request, urlopen

from .parts_intent import normalize_part_intent
from .vin_lookup import normalize_vin


VIN17_BASE_URL = "http://api.17vin.com:8080"
PARTS_CATALOGS_DOCS_URL = "https://www.parts-catalogs.com/us/api"
PARTSAPI_DOCS_URL = "https://partsapi.ru/docs"
MANN_FILTER_GRAPHQL_ENDPOINT = "https://www.mann-filter.com/api/graphql/catalog-prod"
MANN_FILTER_STORE = "pcat_mf_us_store_en"
DENSO_AFTERMARKET_BASE_URL = "https://www.denso-am.eu"

MANN_FILTER_PART_SEARCH_QUERY = """
query ($search: String!, $currentPage: Int!, $pageSize: Int!) {
  productSearch: product_search_name(search: $search, pageSize: $pageSize, currentPage: $currentPage) {
    totalCount: total_count
    pageInfo: page_info {
      pageSize: page_size
      currentPage: current_page
      totalPages: total_pages
    }
    items {
      product {
        sku
        name
        stockStatus: stock_status
        urlKey: url_key
        oeNumbers: oe_numbers {
          label
          value
        }
        comparisonNumbers: comparison_numbers {
          label
          value
        }
      }
    }
  }
}
""".strip()

PARTSAPI_OPERATIONS: dict[str, dict[str, Any]] = {
    "vin_decode_oe": {
        "method": "VINdecodeOE",
        "required": ("identifier",),
        "params": {"vin": "identifier"},
        "docs_url": "https://partsapi.ru/method/doc/VINdecodeOE",
        "role": "VIN/frame decode by original catalogs.",
    },
    "parts_by_vin": {
        "method": "getPartsbyVIN",
        "required": ("identifier", "part_type", "category"),
        "params": {"vin": "identifier", "type": "part_type", "cat": "category"},
        "docs_url": "https://partsapi.ru/method/doc/getPartsbyVIN",
        "role": "OEM/non-OEM parts list by VIN and part group.",
    },
    "oe_applicability": {
        "method": "getOEApplicability",
        "required": ("part_number",),
        "params": {"query": "part_number"},
        "docs_url": "https://partsapi.ru/method/doc/getOEApplicability",
        "role": "Applicability by original catalog part number.",
    },
    "crosses": {
        "method": "getCrosses",
        "required": ("part_number",),
        "params": {"number": "part_number"},
        "docs_url": "https://partsapi.ru/method/doc/getCrosses",
        "role": "Cross/replacement lookup by part number.",
    },
    "crosses_with_brand": {
        "method": "getCrossesWithBrand",
        "required": ("part_number", "brand"),
        "params": {"number": "part_number", "brand": "brand"},
        "docs_url": "https://partsapi.ru/method/doc/getCrossesWithBrand",
        "role": "Cross/replacement lookup by part number and brand.",
    },
    "search_articles": {
        "method": "searchArticles",
        "required": ("part_number",),
        "params": {"SEARCH_NUMBER": "part_number", "LANG": "lang_id"},
        "defaults": {"lang_id": 16},
        "docs_url": "https://partsapi.ru/method/doc/searchArticles",
        "role": "TecDoc article search by any part-number form.",
    },
}

PARTS_CATALOGS_OPERATIONS: dict[str, dict[str, Any]] = {
    "car_info": {
        "path": "/car/info",
        "required": ("identifier",),
        "role": "Vehicle profile lookup by VIN/frame before catalog group selection.",
    },
    "groups": {
        "path": "/catalogs/{catalog_id}/groups2",
        "required": ("catalog_id", "car_id"),
        "role": "Catalog group tree for a resolved Parts-Catalogs vehicle id.",
    },
    "parts": {
        "path": "/catalogs/{catalog_id}/parts2",
        "required": ("catalog_id", "car_id", "group_id"),
        "role": "OEM catalog parts in a selected vehicle catalog group.",
    },
}

_OEM_PART_NUMBER_KEYS = (
    "number",
    "part_number",
    "partNumber",
    "partnumber",
    "partnumber_original",
    "oem",
    "oe",
    "OEM",
    "OE",
    "article",
    "Article",
)
_OEM_NAME_KEYS = ("name", "part_name", "partName", "name_en", "Name", "description", "Description")
_OEM_BRAND_KEYS = ("brand", "Brand", "manufacturer", "Manufacturer", "maker", "Maker")
_OEM_GROUP_KEYS = ("group", "groupName", "category", "Category", "cata_name_en", "catalog_group")
_OEM_APPLICABILITY_KEYS = ("applicability", "Applicability", "fitment", "Fitment", "usage", "model")
_OEM_QUANTITY_KEYS = ("quantity", "qty", "Qty", "amount")


def _md5(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def _redact_identifier(identifier: str) -> str:
    compact = "".join(str(identifier or "").split()).upper()
    if len(compact) <= 6:
        return compact[:2] + "***" if compact else ""
    return f"{compact[:3]}***{compact[-3:]}"


_SENSITIVE_REQUEST_PARAM_NAMES = {
    "vin",
    "frame",
    "frame_no",
    "frameno",
    "chassis",
    "chassis_no",
    "chassisno",
    "body",
    "body_no",
    "identifier",
}


def _is_sensitive_request_param(name: str) -> bool:
    return str(name or "").strip().lower() in _SENSITIVE_REQUEST_PARAM_NAMES


def _redact_sensitive_request_params(params: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _redact_identifier(str(value)) if _is_sensitive_request_param(str(key)) else value
        for key, value in params.items()
    }


def _redact_sensitive_query_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    parsed = urlsplit(value)
    if not parsed.query:
        return value
    redacted_pairs = [
        (key, _redact_identifier(param_value) if _is_sensitive_request_param(key) else param_value)
        for key, param_value in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    query = urlencode(redacted_pairs).replace("%2A%2A%2A", "***")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))


def _safe_request_plan(request_plan: dict[str, Any], *, omit: set[str] | None = None) -> dict[str, Any]:
    omitted = omit or set()
    safe = {key: value for key, value in request_plan.items() if key not in omitted}
    if isinstance(safe.get("params"), dict):
        safe["params"] = _redact_sensitive_request_params(safe["params"])
    for key in ("redacted_url", "url_parameters"):
        if key in safe:
            safe[key] = _redact_sensitive_query_text(safe[key])
    return safe


def _without_secret_query(url: str, secret_param_names: set[str]) -> str:
    parsed = urlsplit(url)
    redacted_pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        redacted_pairs.append((key, "***" if key in secret_param_names else value))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(redacted_pairs), parsed.fragment))


def _clamp_page_size(page_size: int, *, default: int = 5, maximum: int = 25) -> int:
    try:
        value = int(page_size)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, maximum))


def _read_json_url(url: str, *, headers: dict[str, str] | None = None, timeout: float = 20.0) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "AutostopManager/0.1", **(headers or {})})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _reference_groups(raw_references: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_references, list):
        return []
    groups = []
    for reference in raw_references:
        if not isinstance(reference, dict):
            continue
        values = reference.get("value") if isinstance(reference.get("value"), list) else []
        groups.append(
            {
                "label": reference.get("label") or "",
                "values": [str(value) for value in values if value not in (None, "")],
            }
        )
    return groups


def _mann_filter_product(item: dict[str, Any]) -> dict[str, Any]:
    product = item.get("product") if isinstance(item.get("product"), dict) else item
    return {
        "sku": product.get("sku"),
        "name": product.get("name"),
        "stock_status": product.get("stockStatus"),
        "url_key": product.get("urlKey"),
        "product_url": f"https://www.mann-filter.com/us-en/catalog/product/{product.get('urlKey')}.html"
        if product.get("urlKey")
        else None,
        "oe_numbers": _reference_groups(product.get("oeNumbers")),
        "comparison_numbers": _reference_groups(product.get("comparisonNumbers")),
    }


def build_mann_filter_catalog_request(
    *,
    part_number: str,
    current_page: int = 1,
    page_size: int = 5,
    endpoint: str | None = None,
    store: str | None = None,
) -> dict[str, Any]:
    clean_part = str(part_number or "").strip()
    actual_endpoint = (endpoint or os.getenv("MANN_FILTER_GRAPHQL_ENDPOINT") or MANN_FILTER_GRAPHQL_ENDPOINT).rstrip("?&")
    actual_store = store or os.getenv("MANN_FILTER_STORE") or MANN_FILTER_STORE
    clean_page_size = _clamp_page_size(page_size)
    clean_current_page = max(1, int(current_page or 1))
    variables = {"search": clean_part, "currentPage": clean_current_page, "pageSize": clean_page_size}
    query = urlencode(
        {
            "query": MANN_FILTER_PART_SEARCH_QUERY,
            "variables": json.dumps(variables, separators=(",", ":")),
        }
    )
    url = f"{actual_endpoint}?{query}"
    return {
        "ok": bool(clean_part),
        "provider": "mann_filter_catalog",
        "method": "GET",
        "endpoint": actual_endpoint,
        "store": actual_store,
        "variables": variables,
        "url": url if clean_part else None,
        "redacted_url": url if clean_part else None,
        "secret_exposed": False,
    }


def mann_filter_catalog_lookup(
    *,
    part_number: str,
    current_page: int = 1,
    page_size: int = 5,
    timeout: float = 20.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    request_plan = build_mann_filter_catalog_request(part_number=part_number, current_page=current_page, page_size=page_size)
    base = {
        "provider": "mann_filter_catalog",
        "operation": "part_number_search",
        "docs_url": "https://www.mann-filter.com/us-en/catalog.html",
        "role": "Official public MANN-FILTER aftermarket catalog lookup by part/OE number.",
        "request_plan": {key: value for key, value in request_plan.items() if key != "url"},
        "privacy": {"raw_identifier_is_sensitive": False, "secret_exposed": False},
    }
    if not request_plan["ok"]:
        return {**base, "ok": False, "error": "part_number is required."}
    if dry_run:
        return {**base, "ok": True, "dry_run": True}

    try:
        payload = _read_json_url(
            request_plan["url"],
            headers={"Accept": "application/json", "Store": request_plan["store"]},
            timeout=timeout,
        )
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return {**base, "ok": False, "error": str(exc)}

    if payload.get("errors"):
        return {**base, "ok": False, "errors": payload.get("errors")}

    product_search = ((payload.get("data") or {}).get("productSearch") or {}) if isinstance(payload, dict) else {}
    raw_items = product_search.get("items") if isinstance(product_search.get("items"), list) else []
    items = [_mann_filter_product(item) for item in raw_items if isinstance(item, dict)]
    return {
        **base,
        "ok": True,
        "total_count": product_search.get("totalCount", 0),
        "page_info": product_search.get("pageInfo") or {},
        "items": items,
    }


def build_denso_aftermarket_search_request(
    *,
    part_number: str,
    country: str = "europe",
    base_url: str | None = None,
) -> dict[str, Any]:
    clean_part = str(part_number or "").strip()
    clean_country = str(country or "europe").strip() or "europe"
    actual_base_url = (base_url or os.getenv("DENSO_AFTERMARKET_BASE_URL") or DENSO_AFTERMARKET_BASE_URL).rstrip("/")
    query = urlencode({"q": clean_part, "country": clean_country})
    url = f"{actual_base_url}/api/v1/search?{query}"
    return {
        "ok": bool(clean_part),
        "provider": "denso_aftermarket_catalog",
        "method": "GET",
        "endpoint": f"{actual_base_url}/api/v1/search",
        "country": clean_country,
        "url": url if clean_part else None,
        "redacted_url": url if clean_part else None,
        "secret_exposed": False,
    }


def _denso_detail_url(*, part_key: str, country: str, base_url: str | None = None) -> str:
    actual_base_url = (base_url or os.getenv("DENSO_AFTERMARKET_BASE_URL") or DENSO_AFTERMARKET_BASE_URL).rstrip("/")
    return f"{actual_base_url}/api/v1/parts/{quote(str(part_key).strip())}?{urlencode({'country': country})}"


def _denso_catalog_item(raw_item: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": raw_item.get("key"),
        "value": raw_item.get("val"),
        "type": raw_item.get("type"),
        "url": raw_item.get("url"),
        "image": raw_item.get("image"),
        "description": raw_item.get("description"),
        "part_name": raw_item.get("part_name"),
    }


def _denso_detail_summary(raw_detail: dict[str, Any]) -> dict[str, Any]:
    criteria = raw_detail.get("criteria") if isinstance(raw_detail.get("criteria"), list) else []
    return {
        "tid": raw_detail.get("tid"),
        "name": raw_detail.get("name"),
        "title": raw_detail.get("title"),
        "generic_article": raw_detail.get("generic_article"),
        "criteria": [
            {
                "label": criterion.get("label"),
                "value": criterion.get("val"),
                "values": criterion.get("vals") if isinstance(criterion.get("vals"), list) else [],
            }
            for criterion in criteria[:20]
            if isinstance(criterion, dict)
        ],
    }


def denso_aftermarket_catalog_lookup(
    *,
    part_number: str,
    country: str = "europe",
    include_detail: bool = True,
    detail_limit: int = 3,
    timeout: float = 20.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    request_plan = build_denso_aftermarket_search_request(part_number=part_number, country=country)
    base = {
        "provider": "denso_aftermarket_catalog",
        "operation": "part_number_search",
        "docs_url": "https://www.denso-am.eu/catalog/vin",
        "role": "Official public DENSO Aftermarket catalog lookup by DENSO/OE number.",
        "request_plan": {key: value for key, value in request_plan.items() if key != "url"},
        "privacy": {"raw_identifier_is_sensitive": False, "secret_exposed": False},
    }
    if not request_plan["ok"]:
        return {**base, "ok": False, "error": "part_number is required."}
    if dry_run:
        return {**base, "ok": True, "dry_run": True}

    try:
        payload = _read_json_url(request_plan["url"], headers={"Accept": "application/json"}, timeout=timeout)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return {**base, "ok": False, "error": str(exc)}

    if payload.get("status") != "success":
        return {**base, "ok": False, "payload_status": payload.get("status"), "errors": payload.get("errors", [])}

    data = payload.get("data") or {}
    parts = data.get("parts") if isinstance(data.get("parts"), list) else []
    items = [_denso_catalog_item(item) for item in parts if isinstance(item, dict)]
    details = []
    if include_detail:
        for item in items[: _clamp_page_size(detail_limit, default=3, maximum=10)]:
            part_key = item.get("part_name") or item.get("key")
            if not part_key:
                continue
            detail_url = _denso_detail_url(part_key=str(part_key), country=request_plan["country"])
            try:
                detail_payload = _read_json_url(detail_url, headers={"Accept": "application/json"}, timeout=timeout)
            except (HTTPError, URLError, TimeoutError, ValueError) as exc:
                details.append({"part_key": part_key, "ok": False, "error": str(exc)})
                continue
            detail_data = detail_payload.get("data") if isinstance(detail_payload.get("data"), list) else []
            details.append(
                {
                    "part_key": part_key,
                    "ok": detail_payload.get("status") == "success",
                    "items": [_denso_detail_summary(detail) for detail in detail_data if isinstance(detail, dict)],
                }
            )

    return {
        **base,
        "ok": True,
        "total_count": payload.get("total", len(items)),
        "offset": payload.get("offset", 0),
        "items": items,
        "details": details,
    }


def public_aftermarket_catalog_lookup(
    *,
    provider: str,
    part_number: str,
    page_size: int = 5,
    country: str = "europe",
    include_detail: bool = True,
    timeout: float = 20.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider in {"mann", "mann_filter", "mann_filter_catalog"}:
        return mann_filter_catalog_lookup(part_number=part_number, page_size=page_size, timeout=timeout, dry_run=dry_run)
    if normalized_provider in {"denso", "denso_aftermarket", "denso_aftermarket_catalog"}:
        return denso_aftermarket_catalog_lookup(
            part_number=part_number,
            country=country,
            include_detail=include_detail,
            detail_limit=page_size,
            timeout=timeout,
            dry_run=dry_run,
        )
    if normalized_provider == "all":
        return {
            "ok": True,
            "provider": "public_aftermarket_catalogs",
            "operation": "part_number_search",
            "results": [
                mann_filter_catalog_lookup(part_number=part_number, page_size=page_size, timeout=timeout, dry_run=dry_run),
                denso_aftermarket_catalog_lookup(
                    part_number=part_number,
                    country=country,
                    include_detail=include_detail,
                    detail_limit=page_size,
                    timeout=timeout,
                    dry_run=dry_run,
                ),
            ],
            "privacy": {"raw_identifier_is_sensitive": False, "secret_exposed": False},
        }
    return {
        "ok": False,
        "provider": normalized_provider,
        "error": "Unknown public aftermarket catalog provider.",
        "available_providers": ["mann_filter_catalog", "denso_aftermarket_catalog", "all"],
    }


def _vin17_credentials() -> dict[str, Any]:
    user = os.getenv("VIN17_ACCOUNT") or ""
    secret = os.getenv("VIN17_SECRET") or ""
    missing = [name for name, value in {"VIN17_ACCOUNT": user, "VIN17_SECRET": secret}.items() if not value]
    return {"configured": not missing, "user": user, "secret": secret, "missing_env_names": missing}


def _partsapi_credentials() -> dict[str, Any]:
    key = os.getenv("PARTSAPI_KEY") or ""
    base_url = os.getenv("PARTSAPI_BASE_URL") or ""
    missing = [name for name, value in {"PARTSAPI_KEY": key, "PARTSAPI_BASE_URL": base_url}.items() if not value]
    return {
        "configured": not missing,
        "key": key,
        "base_url": base_url,
        "key_param": os.getenv("PARTSAPI_KEY_PARAM") or "key",
        "method_param": os.getenv("PARTSAPI_METHOD_PARAM") or "method",
        "missing_env_names": missing,
    }


def _parts_catalogs_credentials() -> dict[str, Any]:
    key = os.getenv("PARTS_CATALOGS_API_KEY") or ""
    base_url = os.getenv("PARTS_CATALOGS_BASE_URL") or ""
    missing = [name for name, value in {"PARTS_CATALOGS_API_KEY": key, "PARTS_CATALOGS_BASE_URL": base_url}.items() if not value]
    return {"configured": not missing, "key": key, "base_url": base_url, "missing_env_names": missing}


def _first_value(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def _contains_part_number(item: dict[str, Any]) -> bool:
    return _first_value(item, _OEM_PART_NUMBER_KEYS) not in (None, "")


def _iter_oem_candidate_dicts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        candidates: list[dict[str, Any]] = []
        for item in value:
            candidates.extend(_iter_oem_candidate_dicts(item))
        return candidates
    if not isinstance(value, dict):
        return []

    candidates = [value] if _contains_part_number(value) else []
    for nested in value.values():
        if isinstance(nested, (dict, list)):
            candidates.extend(_iter_oem_candidate_dicts(nested))
    return candidates


def _candidate_confidence(raw: dict[str, Any], evidence: dict[str, Any]) -> float:
    if raw.get("is_fit_for_this_vin") in (1, "1", True):
        return 0.95
    if evidence.get("applicability"):
        return 0.82
    if evidence.get("group"):
        return 0.72
    return 0.55


def extract_oem_candidates(*, provider: str, payload: dict[str, Any], operation: str | None = None) -> list[dict[str, Any]]:
    candidates = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for item in _iter_oem_candidate_dicts(payload):
        part_number = _first_value(item, _OEM_PART_NUMBER_KEYS)
        if part_number in (None, ""):
            continue
        evidence = {
            "group": _first_value(item, _OEM_GROUP_KEYS),
            "applicability": _first_value(item, _OEM_APPLICABILITY_KEYS),
            "quantity": _first_value(item, _OEM_QUANTITY_KEYS),
            "is_fit_for_this_vin": item.get("is_fit_for_this_vin"),
        }
        evidence = {key: value for key, value in evidence.items() if value not in (None, "")}
        normalized = {
            "provider": provider,
            "part_number": str(part_number).strip(),
            "brand": _first_value(item, _OEM_BRAND_KEYS),
            "name": _first_value(item, _OEM_NAME_KEYS),
            "source_operation": operation,
            "fitment_evidence": evidence,
            "confidence": _candidate_confidence(item, evidence),
            "raw_keys": sorted(str(key) for key in item.keys()),
        }
        identity = (normalized["part_number"], normalized["brand"], normalized["name"])
        if identity in seen:
            continue
        seen.add(identity)
        candidates.append(normalized)
    return candidates


def build_parts_catalogs_request(
    *,
    operation: str,
    params: dict[str, Any],
    api_key: str | None = None,
    base_url: str | None = None,
    catalog_id: str | None = None,
) -> dict[str, Any]:
    credentials = _parts_catalogs_credentials()
    actual_key = api_key if api_key is not None else credentials["key"]
    actual_base_url = (base_url if base_url is not None else credentials["base_url"]).rstrip("/")
    missing_env = []
    if not actual_key:
        missing_env.append("PARTS_CATALOGS_API_KEY")
    if not actual_base_url:
        missing_env.append("PARTS_CATALOGS_BASE_URL")

    if operation not in PARTS_CATALOGS_OPERATIONS:
        return {
            "ok": False,
            "provider": "parts_catalogs_api",
            "operation": operation,
            "error": "Unknown Parts-Catalogs operation.",
            "available_operations": sorted(PARTS_CATALOGS_OPERATIONS),
            "missing_env_names": missing_env,
            "secret_exposed": False,
        }

    spec = PARTS_CATALOGS_OPERATIONS[operation]
    clean_catalog_id = str(catalog_id or "").strip("/")
    path = spec["path"].format(catalog_id=quote(clean_catalog_id)) if "{catalog_id}" in spec["path"] else spec["path"]
    query_params = {key: value for key, value in params.items() if value not in (None, "")}
    query = urlencode(query_params)
    url = f"{actual_base_url}{path}?{query}" if actual_base_url and query else f"{actual_base_url}{path}" if actual_base_url else None
    headers = {"Authorization": actual_key} if actual_key else {}
    return {
        "ok": not missing_env,
        "provider": "parts_catalogs_api",
        "operation": operation,
        "configured": not missing_env,
        "method": "GET",
        "path": path,
        "params": query_params,
        "base_url_configured": bool(actual_base_url),
        "missing_env_names": missing_env,
        "headers": headers,
        "redacted_headers": {"Authorization": "***"} if actual_key else {},
        "url": url if not missing_env else None,
        "redacted_url": url if url else None,
        "secret_exposed": False,
    }


def _parts_catalogs_lookup_params(
    *,
    operation: str,
    identifier: str | None,
    car_id: str | None,
    group_id: str | None,
) -> dict[str, Any]:
    if operation == "car_info":
        clean_identifier = "".join(str(identifier or "").split()).upper()
        return {"vin": clean_identifier}
    if operation == "groups":
        return {"carId": car_id}
    if operation == "parts":
        return {"carId": car_id, "groupId": group_id}
    return {}


def parts_catalogs_lookup(
    *,
    operation: str,
    identifier: str | None = None,
    catalog_id: str | None = None,
    car_id: str | None = None,
    group_id: str | None = None,
    timeout: float = 20.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    if operation not in PARTS_CATALOGS_OPERATIONS:
        return {
            "ok": False,
            "provider": "parts_catalogs_api",
            "operation": operation,
            "error": "Unknown Parts-Catalogs operation.",
            "available_operations": sorted(PARTS_CATALOGS_OPERATIONS),
        }

    spec = PARTS_CATALOGS_OPERATIONS[operation]
    input_values = {
        "identifier": identifier,
        "catalog_id": catalog_id,
        "car_id": car_id,
        "group_id": group_id,
    }
    missing_params = [name for name in spec["required"] if input_values.get(name) in (None, "")]
    params = _parts_catalogs_lookup_params(operation=operation, identifier=identifier, car_id=car_id, group_id=group_id)
    request_plan = build_parts_catalogs_request(operation=operation, params=params, catalog_id=catalog_id)
    safe_request_plan = _safe_request_plan(request_plan, omit={"url", "headers"})
    base = {
        "provider": "parts_catalogs_api",
        "operation": operation,
        "docs_url": PARTS_CATALOGS_DOCS_URL,
        "role": spec["role"],
        "request_plan": safe_request_plan,
        "redacted_identifier": _redact_identifier(identifier or "") if identifier else None,
        "privacy": {"raw_identifier_is_sensitive": bool(identifier), "secret_exposed": False},
    }
    if missing_params:
        return {**base, "ok": False, "missing_params": missing_params, "error": "Required Parts-Catalogs parameters are missing."}
    if not request_plan["configured"]:
        return {
            **base,
            "ok": False,
            "missing_env_names": request_plan["missing_env_names"],
            "error": "PARTS_CATALOGS_API_KEY and PARTS_CATALOGS_BASE_URL are required for live Parts-Catalogs requests.",
        }
    if dry_run:
        return {**base, "ok": True, "dry_run": True}

    request = Request(
        request_plan["url"],
        headers={"User-Agent": "AutostopManager/0.1", "Accept": "application/json", **request_plan["headers"]},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return {**base, "ok": False, "error": str(exc)}

    return {
        **base,
        "ok": True,
        "payload": payload,
        "oem_candidates": extract_oem_candidates(provider="parts_catalogs_api", payload=payload, operation=operation),
    }


def build_partsapi_request(
    *,
    method: str,
    params: dict[str, Any],
    key: str | None = None,
    base_url: str | None = None,
    key_param: str | None = None,
    method_param: str | None = None,
) -> dict[str, Any]:
    credentials = _partsapi_credentials()
    actual_key = key if key is not None else credentials["key"]
    actual_base_url = (base_url if base_url is not None else credentials["base_url"]).rstrip("?&")
    actual_key_param = key_param or credentials["key_param"]
    actual_method_param = method_param or credentials["method_param"]
    missing = []
    if not actual_key:
        missing.append("PARTSAPI_KEY")
    if not actual_base_url:
        missing.append("PARTSAPI_BASE_URL")

    query_pairs = [(actual_method_param, method)]
    query_pairs.extend((key, str(value)) for key, value in params.items() if value not in (None, ""))
    if actual_key:
        query_pairs.append((actual_key_param, actual_key))
    query = urlencode(query_pairs)
    url = f"{actual_base_url}?{query}" if actual_base_url else None
    redacted_url = _without_secret_query(url, {actual_key_param}) if url else None

    return {
        "ok": not missing,
        "provider": "partsapi_ru",
        "configured": not missing,
        "method": "GET",
        "partsapi_method": method,
        "params": {key: value for key, value in params.items() if value not in (None, "")},
        "base_url_configured": bool(actual_base_url),
        "key_param": actual_key_param,
        "method_param": actual_method_param,
        "missing_env_names": missing,
        "url": url if not missing else None,
        "redacted_url": redacted_url,
        "secret_exposed": False,
    }


def _partsapi_operation_params(
    operation: str,
    *,
    identifier: str | None = None,
    part_number: str | None = None,
    brand: str | None = None,
    part_type: str | None = None,
    category: str | None = None,
    lang_id: int | None = None,
) -> dict[str, Any]:
    spec = PARTSAPI_OPERATIONS[operation]
    values = dict(spec.get("defaults", {}))
    values.update(
        {
            key: value
            for key, value in {
                "identifier": identifier,
                "part_number": part_number,
                "brand": brand,
                "part_type": part_type,
                "category": category,
                "lang_id": lang_id,
            }.items()
            if value not in (None, "")
        }
    )
    params = {}
    for api_param, source_name in spec["params"].items():
        params[api_param] = values.get(source_name)
    return params


def partsapi_catalog_lookup(
    *,
    operation: str,
    identifier: str | None = None,
    part_number: str | None = None,
    brand: str | None = None,
    part_type: str | None = None,
    category: str | None = None,
    lang_id: int | None = None,
    timeout: float = 20.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    if operation not in PARTSAPI_OPERATIONS:
        return {
            "ok": False,
            "provider": "partsapi_ru",
            "operation": operation,
            "error": "Unknown PartsAPI operation.",
            "available_operations": sorted(PARTSAPI_OPERATIONS),
        }

    spec = PARTSAPI_OPERATIONS[operation]
    input_values = dict(spec.get("defaults", {}))
    input_values.update(
        {
            key: value
            for key, value in {
                "identifier": identifier,
                "part_number": part_number,
                "brand": brand,
                "part_type": part_type,
                "category": category,
                "lang_id": lang_id,
            }.items()
            if value not in (None, "")
        }
    )
    missing_params = [name for name in spec["required"] if input_values.get(name) in (None, "")]
    params = _partsapi_operation_params(
        operation,
        identifier=identifier,
        part_number=part_number,
        brand=brand,
        part_type=part_type,
        category=category,
        lang_id=lang_id,
    )
    request_plan = build_partsapi_request(method=spec["method"], params=params)
    base = {
        "provider": "partsapi_ru",
        "operation": operation,
        "partsapi_method": spec["method"],
        "docs_url": spec["docs_url"],
        "role": spec["role"],
        "request_plan": _safe_request_plan(request_plan, omit={"url"}),
        "redacted_identifier": _redact_identifier(identifier or "") if identifier else None,
        "privacy": {"raw_identifier_is_sensitive": bool(identifier), "secret_exposed": False},
    }
    if missing_params:
        return {**base, "ok": False, "missing_params": missing_params, "error": "Required PartsAPI parameters are missing."}
    if not request_plan["configured"]:
        return {
            **base,
            "ok": False,
            "missing_env_names": request_plan["missing_env_names"],
            "error": "PARTSAPI_KEY and PARTSAPI_BASE_URL are required for live PartsAPI requests.",
        }
    if dry_run:
        return {**base, "ok": True, "dry_run": True}

    request = Request(request_plan["url"], headers={"User-Agent": "AutostopManager/0.1"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return {**base, "ok": False, "error": str(exc)}

    return {
        **base,
        "ok": True,
        "payload": payload,
        "oem_candidates": extract_oem_candidates(provider="partsapi_ru", payload=payload, operation=operation),
    }


def build_17vin_token(*, user: str, secret: str, url_parameters: str) -> str:
    return _md5(_md5(user) + _md5(secret) + url_parameters)


def build_17vin_signed_request(
    *,
    path: str = "/",
    params: dict[str, str | int | None],
    user: str | None = None,
    secret: str | None = None,
) -> dict[str, Any]:
    credentials = _vin17_credentials()
    actual_user = user or credentials["user"]
    actual_secret = secret or credentials["secret"]
    missing = []
    if not actual_user:
        missing.append("VIN17_ACCOUNT")
    if not actual_secret:
        missing.append("VIN17_SECRET")

    clean_path = path if path.startswith("/") else f"/{path}"
    filtered = {key: value for key, value in params.items() if value not in (None, "")}
    query = urlencode(filtered)
    url_parameters = f"{clean_path}?{query}" if query else clean_path
    token = build_17vin_token(user=actual_user, secret=actual_secret, url_parameters=url_parameters) if not missing else ""
    signed_query = f"{query}&user={quote(actual_user)}&token={token}" if query else f"user={quote(actual_user)}&token={token}"
    signed_url = f"{VIN17_BASE_URL}{clean_path}?{signed_query}"

    return {
        "ok": not missing,
        "provider": "vin17_api",
        "configured": not missing,
        "missing_env_names": missing,
        "method": "GET",
        "url_parameters": url_parameters,
        "signed_url": signed_url if not missing else None,
        "redacted_url": f"{VIN17_BASE_URL}{clean_path}?{query}&user={quote(actual_user)}&token=***" if query else f"{VIN17_BASE_URL}{clean_path}?user={quote(actual_user)}&token=***",
        "token_algorithm": "MD5(MD5(user) + MD5(secret) + url_parameters)",
        "secret_exposed": False,
    }


def vin17_decode_vehicle(identifier: str, *, timeout: float = 20.0, dry_run: bool = False) -> dict[str, Any]:
    normalized = normalize_vin(identifier)
    request_plan = build_17vin_signed_request(path="/", params={"vin": normalized})
    if not request_plan["configured"]:
        return {
            "ok": False,
            "provider": "vin17_api",
            "redacted_identifier": _redact_identifier(identifier),
            "missing_env_names": request_plan["missing_env_names"],
            "error": "VIN17_ACCOUNT and VIN17_SECRET are required for live 17VIN requests.",
            "request_plan": _safe_request_plan(request_plan, omit={"signed_url"}),
        }
    if dry_run:
        return {
            "ok": True,
            "provider": "vin17_api",
            "redacted_identifier": _redact_identifier(identifier),
            "dry_run": True,
            "request_plan": _safe_request_plan(request_plan, omit={"signed_url"}),
        }

    request = Request(request_plan["signed_url"], headers={"User-Agent": "AutostopManager/0.1"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return {
            "ok": False,
            "provider": "vin17_api",
            "redacted_identifier": _redact_identifier(identifier),
            "error": str(exc),
            "request_plan": _safe_request_plan(request_plan, omit={"signed_url"}),
        }

    return {
        "ok": payload.get("code") == 1,
        "provider": "vin17_api",
        "redacted_identifier": _redact_identifier(identifier),
        "request_plan": _safe_request_plan(request_plan, omit={"signed_url"}),
        "payload": payload,
        "oem_candidates": extract_oem_candidates(provider="vin17_api", payload=payload, operation="search_part_number"),
    }


def vin17_search_std_part_name_by_vin(
    *,
    epc: str,
    identifier: str,
    query_part_name: str,
    query_match_type: str = "exact",
    timeout: float = 20.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    normalized = normalize_vin(identifier)
    request_plan = build_17vin_signed_request(
        path=f"/{epc.strip('/')}",
        params={
            "action": "search_std_part_name",
            "vin": normalized,
            "query_match_type": query_match_type,
            "query_part_name": query_part_name,
        },
    )
    base = {
        "provider": "vin17_api",
        "operation": "search_std_part_name",
        "docs_url": "https://www.17vin.com/doc/5101.html",
        "role": "17VIN standard part-name search scoped by VIN and EPC code.",
        "redacted_identifier": _redact_identifier(identifier),
        "request_plan": _safe_request_plan(request_plan, omit={"signed_url"}),
        "privacy": {"raw_identifier_is_sensitive": True, "secret_exposed": False},
    }
    if not request_plan["configured"]:
        return {
            **base,
            "ok": False,
            "missing_env_names": request_plan["missing_env_names"],
            "error": "VIN17_ACCOUNT and VIN17_SECRET are required for live 17VIN requests.",
        }
    if dry_run:
        return {**base, "ok": True, "dry_run": True}

    request = Request(request_plan["signed_url"], headers={"User-Agent": "AutostopManager/0.1"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return {**base, "ok": False, "error": str(exc)}

    return {
        **base,
        "ok": payload.get("code") == 1,
        "payload": payload,
        "oem_candidates": extract_oem_candidates(provider="vin17_api", payload=payload, operation="search_std_part_name"),
    }


def _result_oem_candidates(result: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(result.get("oem_candidates"), list):
        return [candidate for candidate in result["oem_candidates"] if isinstance(candidate, dict)]
    payload = result.get("payload")
    if isinstance(payload, dict):
        return extract_oem_candidates(provider=str(result.get("provider") or "unknown"), payload=payload, operation=result.get("operation"))
    return []


def _compact_provider_result(result: dict[str, Any]) -> dict[str, Any]:
    compact = {key: value for key, value in result.items() if key not in {"payload"}}
    if "oem_candidates" in compact:
        compact["candidate_count"] = len(compact["oem_candidates"]) if isinstance(compact["oem_candidates"], list) else 0
        compact.pop("oem_candidates", None)
    return compact


def _provider_blocker(result: dict[str, Any]) -> dict[str, Any] | None:
    if result.get("ok") is True:
        return None
    blocker = {
        "provider": result.get("provider"),
        "operation": result.get("operation"),
        "error": result.get("error") or "Provider did not return an OK result.",
    }
    if result.get("missing_env_names"):
        blocker["missing_env_names"] = result["missing_env_names"]
    if result.get("missing_params"):
        blocker["missing_params"] = result["missing_params"]
    return blocker


def lookup_oem_catalog_candidates(
    *,
    identifier: str,
    requested_part: str,
    catalog_id: str | None = None,
    car_id: str | None = None,
    group_id: str | None = None,
    epc: str | None = None,
    partsapi_part_type: str = "original",
    partsapi_category: str | None = None,
    timeout: float = 20.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    part_profile = normalize_part_intent(requested_part)
    terms = [str(term).strip() for term in part_profile.get("catalog_search_terms", []) if str(term).strip()]
    primary_term = terms[0] if terms else requested_part
    partsapi_terms = part_profile.get("partsapi_cat_candidates") or []
    category = partsapi_category or (partsapi_terms[0] if partsapi_terms else primary_term)

    provider_results: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    oem_candidates: list[dict[str, Any]] = []

    if catalog_id and car_id and group_id:
        provider_results.append(
            parts_catalogs_lookup(
                operation="parts",
                catalog_id=catalog_id,
                car_id=car_id,
                group_id=group_id,
                timeout=timeout,
                dry_run=dry_run,
            )
        )
    else:
        blockers.append(
            {
                "provider": "parts_catalogs_api",
                "operation": "parts",
                "missing_params": [
                    name
                    for name, value in {"catalog_id": catalog_id, "car_id": car_id, "group_id": group_id}.items()
                    if value in (None, "")
                ],
                "error": "Parts-Catalogs OEM parts lookup requires catalog_id, car_id, and group_id.",
            }
        )

    provider_results.append(
        partsapi_catalog_lookup(
            operation="parts_by_vin",
            identifier=identifier,
            part_type=partsapi_part_type,
            category=category,
            timeout=timeout,
            dry_run=dry_run,
        )
    )

    if epc:
        provider_results.append(
            vin17_search_std_part_name_by_vin(
                epc=epc,
                identifier=identifier,
                query_part_name=primary_term,
                query_match_type="exact",
                timeout=timeout,
                dry_run=dry_run,
            )
        )
    else:
        blockers.append(
            {
                "provider": "vin17_api",
                "operation": "search_std_part_name",
                "missing_params": ["epc"],
                "error": "17VIN standard part-name lookup requires the EPC code returned by 17VIN vehicle decode.",
            }
        )

    for result in provider_results:
        oem_candidates.extend(_result_oem_candidates(result))
        blocker = _provider_blocker(result)
        if blocker:
            blockers.append(blocker)

    return {
        "ok": bool(oem_candidates) or (dry_run and bool(provider_results)),
        "provider": "multi_oem_catalog_lookup",
        "identifier": {"redacted": _redact_identifier(identifier), "raw_identifier_is_sensitive": True},
        "requested_part": requested_part,
        "part_profile": part_profile,
        "provider_count": len(provider_results),
        "candidate_count": len(oem_candidates),
        "provider_results": [_compact_provider_result(result) for result in provider_results],
        "oem_candidates": oem_candidates,
        "blockers": blockers,
        "privacy": {"raw_identifier_is_sensitive": True, "secret_exposed": False},
    }


def vin17_search_part_number_by_vin(
    *,
    epc: str,
    identifier: str,
    query_part_number: str,
    query_match_type: str = "exact",
    timeout: float = 20.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    normalized = normalize_vin(identifier)
    request_plan = build_17vin_signed_request(
        path=f"/{epc.strip('/')}",
        params={
            "action": "search_part_number",
            "vin": normalized,
            "query_match_type": query_match_type,
            "query_part_number": query_part_number,
        },
    )
    if not request_plan["configured"]:
        return {
            "ok": False,
            "provider": "vin17_api",
            "redacted_identifier": _redact_identifier(identifier),
            "missing_env_names": request_plan["missing_env_names"],
            "error": "VIN17_ACCOUNT and VIN17_SECRET are required for live 17VIN requests.",
            "request_plan": _safe_request_plan(request_plan, omit={"signed_url"}),
        }
    if dry_run:
        return {
            "ok": True,
            "provider": "vin17_api",
            "redacted_identifier": _redact_identifier(identifier),
            "dry_run": True,
            "request_plan": _safe_request_plan(request_plan, omit={"signed_url"}),
        }

    request = Request(request_plan["signed_url"], headers={"User-Agent": "AutostopManager/0.1"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return {
            "ok": False,
            "provider": "vin17_api",
            "redacted_identifier": _redact_identifier(identifier),
            "error": str(exc),
            "request_plan": _safe_request_plan(request_plan, omit={"signed_url"}),
        }

    return {
        "ok": payload.get("code") == 1,
        "provider": "vin17_api",
        "redacted_identifier": _redact_identifier(identifier),
            "request_plan": _safe_request_plan(request_plan, omit={"signed_url"}),
        "payload": payload,
    }
