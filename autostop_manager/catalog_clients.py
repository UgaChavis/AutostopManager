from __future__ import annotations

import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit, parse_qsl
from urllib.request import Request, urlopen
from xml.etree.ElementTree import Element, ParseError
from xml.sax.saxutils import escape as xml_escape

from defusedxml.ElementTree import fromstring as safe_xml_fromstring

from .config import load_runtime_env
from .parts_intent import normalize_part_intent
from .partsapi_category_index import search_partsapi_category_index
from .vin_lookup import normalize_vin


VIN17_BASE_URL = "http://api.17vin.com:8080"
PARTS_CATALOGS_DOCS_URL = "https://www.parts-catalogs.com/us/api"
MANN_FILTER_GRAPHQL_ENDPOINT = "https://www.mann-filter.com/api/graphql/catalog-prod"
MANN_FILTER_STORE = "pcat_mf_us_store_en"
DENSO_AFTERMARKET_BASE_URL = "https://www.denso-am.eu"
EMEX_SEARCH_SERVICE_URL = "http://ws.emex.ru/EmExService.asmx"
EMEX_SEARCH_DOCS_URL = "http://wsdoc.emex.ru/FindDetailAdv5.html"
EMEX_SOAP_NAMESPACE = "http://tempuri.org/"
EMEX_MAX_RESPONSE_BYTES = 2_000_000
EXIST_BASE_URL = "https://www.exist.ru"
EXIST_OPEN_SEARCH_DOCS_URL = "https://s.exist.ru/xml/osd.xml"
EXIST_DEFAULT_OFFICE_ID = 905
EXIST_DEFAULT_OFFICE_NAME = "Красноярск, ул. Гайдашовка, д.3"

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
    "vin_decode": {
        "method": "VINdecode",
        "required": ("identifier", "lang"),
        "params": {"vin": "identifier", "lang": "lang"},
        "defaults": {"lang": "ru"},
        "docs_url": "https://partsapi.ru/method/doc/VINdecode",
        "role": "VIN decode into TecDoc/TecRMI vehicle identity and characteristics.",
    },
    "vin_decode_oe": {
        "method": "VINdecodeOE",
        "required": ("identifier",),
        "params": {"vin": "identifier"},
        "docs_url": "https://partsapi.ru/method/doc/VINdecodeOE",
        "role": "VIN/frame decode by original catalogs.",
    },
    "plate_to_vin": {
        "method": "gosnomer2vin",
        "required": ("registration_number",),
        "params": {"gosnomer": "registration_number"},
        "docs_url": "https://partsapi.ru/method/doc/gosnomer2vin",
        "role": "VIN lookup by Russian vehicle registration number; identity lead that must be verified before writes.",
    },
    "parts_by_vin": {
        "method": "getPartsbyVIN",
        "required": ("identifier", "part_type", "category"),
        "params": {"vin": "identifier", "type": "part_type", "cat": "category"},
        "defaults": {"part_type": "oem"},
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
    "crosses_title": {
        "method": "getCrossesTitle",
        "required": ("part_number", "lang"),
        "params": {"lang": "lang", "number": "part_number"},
        "defaults": {"lang": "ru"},
        "docs_url": "https://partsapi.ru/method/doc/getCrossesTitle",
        "role": "Cross/replacement lookup by part number with localized part names.",
    },
    "part_name_by_brand_number": {
        "method": "getPartnameByBrandNumber",
        "required": ("brand", "part_number", "lang"),
        "params": {"brand": "brand", "number": "part_number", "lang": "lang"},
        "defaults": {"lang": "ru"},
        "docs_url": "https://partsapi.ru/method/doc/getPartnameByBrandNumber",
        "role": "Part-name lookup by aftermarket brand and article number; article enrichment only.",
    },
    "article_crosses": {
        "method": "getArticleCrosses",
        "required": ("article_id", "lang_id"),
        "params": {"ART_ID": "article_id", "LANG": "lang_id"},
        "defaults": {"lang_id": 16},
        "docs_url": "https://partsapi.ru/method/doc/getArticleCrosses",
        "role": "TecDoc cross/replacement articles by article ID.",
    },
    "search_articles": {
        "method": "searchArticles",
        "required": ("part_number",),
        "params": {"SEARCH_NUMBER": "part_number", "LANG": "lang_id"},
        "defaults": {"lang_id": 16},
        "docs_url": "https://partsapi.ru/method/doc/searchArticles",
        "role": "TecDoc article search by any part-number form.",
    },
    "engine_info": {
        "method": "getEngine",
        "required": ("vehicle_type", "type_id", "lang_id"),
        "params": {"TYPE": "vehicle_type", "TYPE_ID": "type_id", "LANG": "lang_id"},
        "defaults": {"vehicle_type": "PC", "lang_id": 16},
        "docs_url": "https://partsapi.ru/method/doc/getEngine",
        "role": "TecDoc engine details and characteristics by vehicle type and modification ID.",
    },
    "search_tree": {
        "method": "getSearchTree",
        "required": ("vehicle_type", "type_id", "lang_id"),
        "params": {"TYPE": "vehicle_type", "TYPE_ID": "type_id", "LANG": "lang_id"},
        "defaults": {"vehicle_type": "PC", "lang_id": 16},
        "docs_url": "https://partsapi.ru/method/doc/getSearchTree",
        "role": "TecDoc/PartsAPI product group tree for a resolved vehicle modification.",
    },
    "articles": {
        "method": "getArticles",
        "required": ("vehicle_type", "type_id", "category", "lang_id"),
        "params": {"TYPE": "vehicle_type", "TYPE_ID": "type_id", "STR_ID": "category", "LANG": "lang_id"},
        "defaults": {"vehicle_type": "PC", "lang_id": 16},
        "docs_url": "https://partsapi.ru/method/doc/getArticles",
        "role": "TecDoc articles linked to a product group tree node for a resolved vehicle.",
    },
    "article": {
        "method": "getArticle",
        "required": ("article_id", "lang_id"),
        "params": {"ART_ID": "article_id", "LANG": "lang_id"},
        "defaults": {"lang_id": 16},
        "docs_url": "https://partsapi.ru/method/doc/getArticle",
        "role": "Full TecDoc article information by article identifier.",
    },
    "article_criteria": {
        "method": "getArticleCriteria",
        "required": ("article_id", "lang_id"),
        "params": {"ART_ID": "article_id", "LANG": "lang_id"},
        "defaults": {"lang_id": 16},
        "docs_url": "https://partsapi.ru/method/doc/getArticleCriteria",
        "role": "TecDoc article characteristics/criteria by article identifier.",
    },
    "norms_makes": {
        "method": "GetNormsMakes",
        "required": (),
        "params": {},
        "docs_url": "https://partsapi.ru/method/doc/GetNormsMakes",
        "role": "AUTONORMS makes and their provider identifiers.",
    },
    "norms_models": {
        "method": "GetNormsModels",
        "required": ("make_name_seo",),
        "params": {"makeNameSEO": "make_name_seo"},
        "docs_url": "https://partsapi.ru/method/doc/GetNormsModels",
        "role": "AUTONORMS models for one make identifier.",
    },
    "norms_motors": {
        "method": "GetNormsMotors",
        "required": ("model_id",),
        "params": {"modelId": "model_id"},
        "docs_url": "https://partsapi.ru/method/doc/GetNormsMotors",
        "role": "AUTONORMS engine modifications for one model identifier.",
    },
    "norms_times": {
        "method": "GetNormsTimes",
        "required": ("motor_id", "top_category_id", "sub_category_id"),
        "params": {"motorId": "motor_id", "TopCatId": "top_category_id", "SubCatId": "sub_category_id"},
        "docs_url": "https://partsapi.ru/method/doc/GetNormsTimes",
        "role": "AUTONORMS work list and norm-hours for one engine and work category.",
    },
    "fill_volumes": {
        "method": "GetFillVolumes",
        "required": ("car_id",),
        "params": {"carId": "car_id"},
        "docs_url": "https://partsapi.ru/method/doc/GetFillVolumes",
        "role": "AUTONORMS fluid fill volumes for one vehicle modification.",
    },
}

PARTSAPI_METHOD_KEY_ENV_NAMES = {
    "VINdecode": "PARTSAPI_VINDECODE_KEY",
    "VINdecodeOE": "PARTSAPI_VINDECODE_OE_KEY",
    "gosnomer2vin": "PARTSAPI_GOSNOMER2VIN_KEY",
    "getPartsbyVIN": "PARTSAPI_PARTS_BY_VIN_KEY",
    "getOEApplicability": "PARTSAPI_OE_APPLICABILITY_KEY",
    "getCrosses": "PARTSAPI_CROSSES_KEY",
    "getCrossesWithBrand": "PARTSAPI_CROSSES_WITH_BRAND_KEY",
    "getCrossesTitle": "PARTSAPI_CROSSES_TITLE_KEY",
    "getPartnameByBrandNumber": "PARTSAPI_PARTNAME_BY_BRAND_NUMBER_KEY",
    "getArticleCrosses": "PARTSAPI_ARTICLE_CROSSES_KEY",
    "searchArticles": "PARTSAPI_SEARCH_ARTICLES_KEY",
    "getEngine": "PARTSAPI_GET_ENGINE_KEY",
    "getSearchTree": "PARTSAPI_SEARCH_TREE_KEY",
    "getArticles": "PARTSAPI_ARTICLES_KEY",
    "getArticle": "PARTSAPI_ARTICLE_KEY",
    "getArticleCriteria": "PARTSAPI_ARTICLE_CRITERIA_KEY",
    "GetNormsMakes": "PARTSAPI_GET_NORMS_MAKES_KEY",
    "GetNormsModels": "PARTSAPI_GET_NORMS_MODELS_KEY",
    "GetNormsMotors": "PARTSAPI_GET_NORMS_MOTORS_KEY",
    "GetNormsTimes": "PARTSAPI_GET_NORMS_TIMES_KEY",
    "GetFillVolumes": "PARTSAPI_GET_FILL_VOLUMES_KEY",
}

PARTSAPI_OMIT_PART_TYPE_VALUES = {"omit", "none", "non-oem", "non_oem", "nonoriginal", "non-original", "aftermarket"}

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
    return hashlib.md5(value.encode("utf-8"), usedforsecurity=False).hexdigest()


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
    "gosnomer",
    "registration_number",
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


def _without_secret_query(
    url: str,
    secret_param_names: set[str],
    *,
    account_param_names: set[str] | None = None,
) -> str:
    parsed = urlsplit(url)
    secret_names = {name.lower() for name in secret_param_names}
    account_names = {name.lower() for name in (account_param_names or set())}
    redacted_pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        key_normalized = key.lower()
        if key_normalized in secret_names:
            redacted_value = "***"
        elif key_normalized in account_names:
            redacted_value = _redact_account(value)
        elif _is_sensitive_request_param(key):
            redacted_value = _redact_identifier(value)
        else:
            redacted_value = value
        redacted_pairs.append((key, redacted_value))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(redacted_pairs), parsed.fragment)).replace(
        "%2A%2A%2A", "***"
    )


def _clamp_page_size(page_size: int, *, default: int = 5, maximum: int = 25) -> int:
    try:
        value = int(page_size)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, maximum))


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [{str(key): nested for key, nested in item.items()} for item in value if isinstance(item, dict)]


def _read_json_url(url: str, *, headers: dict[str, str] | None = None, timeout: float = 20.0) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "AutostopManager/0.1", **(headers or {})})
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("provider returned a non-object JSON payload")
    return payload


def _reference_groups(raw_references: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_references, list):
        return []
    groups = []
    for reference in raw_references:
        if not isinstance(reference, dict):
            continue
        raw_values = reference.get("value")
        values = raw_values if isinstance(raw_values, list) else []
        groups.append(
            {
                "label": reference.get("label") or "",
                "values": [str(value) for value in values if value not in (None, "")],
            }
        )
    return groups


def _mann_filter_product(item: dict[str, Any]) -> dict[str, Any]:
    raw_product = item.get("product")
    product = {str(key): value for key, value in raw_product.items()} if isinstance(raw_product, dict) else item
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
    actual_endpoint = (endpoint or os.getenv("MANN_FILTER_GRAPHQL_ENDPOINT") or MANN_FILTER_GRAPHQL_ENDPOINT).rstrip(
        "?&"
    )
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
    request_plan = build_mann_filter_catalog_request(
        part_number=part_number, current_page=current_page, page_size=page_size
    )
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

    data = payload.get("data")
    if not isinstance(data, dict):
        return {**base, "ok": False, "error": "MANN-FILTER returned a malformed data payload."}
    product_search = data.get("productSearch")
    if not isinstance(product_search, dict):
        return {**base, "ok": False, "error": "MANN-FILTER returned a malformed productSearch payload."}
    items = [_mann_filter_product(item) for item in _dict_list(product_search.get("items"))]
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
    criteria = _dict_list(raw_detail.get("criteria"))
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

    data = payload.get("data")
    if not isinstance(data, dict):
        return {**base, "ok": False, "error": "DENSO returned a malformed search data payload."}
    items = [_denso_catalog_item(item) for item in _dict_list(data.get("parts"))]
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
            detail_data = _dict_list(detail_payload.get("data"))
            details.append(
                {
                    "part_key": part_key,
                    "ok": detail_payload.get("status") == "success",
                    "items": [_denso_detail_summary(detail) for detail in detail_data],
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
        return mann_filter_catalog_lookup(
            part_number=part_number, page_size=page_size, timeout=timeout, dry_run=dry_run
        )
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
        results = [
            mann_filter_catalog_lookup(part_number=part_number, page_size=page_size, timeout=timeout, dry_run=dry_run),
            denso_aftermarket_catalog_lookup(
                part_number=part_number,
                country=country,
                include_detail=include_detail,
                detail_limit=page_size,
                timeout=timeout,
                dry_run=dry_run,
            ),
        ]
        success_count = sum(result.get("ok") is True for result in results)
        return {
            "ok": success_count > 0,
            "provider": "public_aftermarket_catalogs",
            "operation": "part_number_search",
            "success_count": success_count,
            "failure_count": len(results) - success_count,
            "results": results,
            "privacy": {"raw_identifier_is_sensitive": False, "secret_exposed": False},
        }
    return {
        "ok": False,
        "provider": normalized_provider,
        "error": "Unknown public aftermarket catalog provider.",
        "available_providers": ["mann_filter_catalog", "denso_aftermarket_catalog", "all"],
    }


def _emex_xml_value(name: str, value: Any) -> str:
    if value in (None, ""):
        return f'<{name} xsi:nil="true" />'
    if isinstance(value, (list, tuple)):
        items = "".join(f"<string>{xml_escape(str(item))}</string>" for item in value if item not in (None, ""))
        return f"<{name}>{items}</{name}>" if items else f'<{name} xsi:nil="true" />'
    return f"<{name}>{xml_escape(str(value))}</{name}>"


def build_emex_find_detail_request(
    *,
    part_number: str,
    brand: str | None = None,
    subst_level: str = "All",
    subst_filter: str = "None",
    delivery_region_type: str = "PRI",
    min_delivery_percent: int | None = None,
    max_delivery_days: int | None = None,
    min_quantity: int | None = None,
    max_result_price: float | None = None,
    max_one_detail_offers_count: int | None = 10,
    detail_nums_to_load: list[str] | None = None,
    login: str | None = None,
    password: str | None = None,
    service_url: str | None = None,
) -> dict[str, Any]:
    credentials = _emex_credentials()
    actual_login = login if login is not None else credentials["login"]
    actual_password = password if password is not None else credentials["password"]
    actual_service_url = service_url or credentials["service_url"]
    clean_part = str(part_number or "").strip()
    clean_brand = str(brand or "").strip() or None
    missing = []
    if not actual_login:
        missing.append("EMEX_LOGIN")
    if not actual_password:
        missing.append("EMEX_PASSWORD")
    if not clean_part:
        missing.append("part_number")

    params = {
        "login": actual_login,
        "password": actual_password,
        "makeLogo": clean_brand,
        "detailNum": clean_part,
        "substLevel": subst_level or "All",
        "substFilter": subst_filter or "None",
        "deliveryRegionType": delivery_region_type or "PRI",
        "minDeliveryPercent": min_delivery_percent,
        "maxADDays": max_delivery_days,
        "minQuantity": min_quantity,
        "maxResultPrice": max_result_price,
        "maxOneDetailOffersCount": max_one_detail_offers_count,
        "detailNumsToLoad": detail_nums_to_load,
    }
    body_params = "".join(_emex_xml_value(name, value) for name, value in params.items())
    soap_body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soap:Body>"
        f'<FindDetailAdv5 xmlns="{EMEX_SOAP_NAMESPACE}">'
        f"{body_params}"
        "</FindDetailAdv5>"
        "</soap:Body>"
        "</soap:Envelope>"
    )
    safe_params = {
        key: (
            "***" if key == "password" and value else _redact_account(str(value)) if key == "login" and value else value
        )
        for key, value in params.items()
        if value not in (None, "")
    }
    return {
        "ok": not missing,
        "provider": "emex",
        "configured": not any(name in {"EMEX_LOGIN", "EMEX_PASSWORD"} for name in missing),
        "method": "POST",
        "emex_method": "FindDetailAdv5",
        "endpoint": actual_service_url,
        "soap_action": f"{EMEX_SOAP_NAMESPACE}FindDetailAdv5",
        "params": safe_params,
        "missing_env_names": [name for name in missing if name.startswith("EMEX_")],
        "missing_params": [name for name in missing if not name.startswith("EMEX_")],
        "body": soap_body if not missing else None,
        "body_sha256": hashlib.sha256(soap_body.encode("utf-8")).hexdigest() if not missing else None,
        "secret_exposed": False,
    }


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _xml_child_text(element: Element, name: str) -> str | None:
    for child in list(element):
        if _xml_local_name(child.tag) == name:
            return child.text
    return None


def _xml_text_as_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    return str(value).strip().lower() in {"true", "1", "yes"}


def _xml_text_as_number(value: str | None) -> int | float | None:
    if value in (None, ""):
        return None
    raw = str(value).strip().replace(",", ".")
    try:
        number = float(raw)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def _emex_detail_item(element: Element) -> dict[str, Any]:
    field_map = {
        "GroupId": "group_id",
        "PriceGroup": "price_group",
        "MakeLogo": "brand_logo",
        "MakeName": "brand",
        "DetailNum": "part_number",
        "NewDetailNum": "new_part_number",
        "DetailNameRus": "name",
        "PriceLogo": "price_logo",
        "DestinationLogo": "destination_logo",
        "PriceCountry": "price_country",
        "LotQuantity": "lot_quantity",
        "Quantity": "quantity",
        "DDPercent": "delivery_probability_percent",
        "ADDays": "average_delivery_days",
        "DeliverTimeGuaranteed": "delivery_time_guaranteed",
        "ResultPrice": "price_rub",
        "DeliveryRegionType": "delivery_region_type",
    }
    numeric_fields = {
        "GroupId",
        "LotQuantity",
        "Quantity",
        "DDPercent",
        "ADDays",
        "DeliverTimeGuaranteed",
        "ResultPrice",
    }
    item: dict[str, Any] = {}
    for child in list(element):
        source_name = _xml_local_name(child.tag)
        target_name = field_map.get(source_name)
        if not target_name:
            continue
        item[target_name] = _xml_text_as_number(child.text) if source_name in numeric_fields else (child.text or "")
    return item


def parse_emex_find_detail_response(raw_xml: str) -> dict[str, Any]:
    root = _safe_emex_xml_root(raw_xml)
    result = None
    for element in root.iter():
        if _xml_local_name(element.tag) == "FindDetailAdv5Result":
            result = element
            break
    if result is None:
        detail_nodes = [
            element for element in root.iter() if _xml_local_name(element.tag) in {"DetailItem", "FindDetailAdv5Result"}
        ]
        return {
            "is_success": bool(detail_nodes),
            "error_message": None if detail_nodes else "FindDetailAdv5Result not found in SOAP response.",
            "block_date_end": None,
            "details": [_emex_detail_item(element) for element in detail_nodes if list(element)],
        }

    details = []
    for element in result.iter():
        if _xml_local_name(element.tag) == "DetailItem":
            item = _emex_detail_item(element)
            if item:
                details.append(item)
    return {
        "is_success": _xml_text_as_bool(_xml_child_text(result, "IsSuccess")),
        "error_message": _xml_child_text(result, "ErrorMessage"),
        "block_date_end": _xml_child_text(result, "BlockDateEnd"),
        "details": details,
    }


def _safe_emex_xml_root(raw_xml: str) -> Element:
    if not isinstance(raw_xml, str):
        raise ValueError("invalid_xml_payload")
    if len(raw_xml.encode("utf-8", errors="replace")) > EMEX_MAX_RESPONSE_BYTES:
        raise ValueError("xml_response_too_large")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", raw_xml, flags=re.IGNORECASE):
        raise ValueError("EntitiesForbidden: DTD and entity declarations are not allowed")
    return safe_xml_fromstring(raw_xml)


def emex_price_lookup(
    *,
    part_number: str,
    brand: str | None = None,
    subst_level: str = "All",
    subst_filter: str = "None",
    delivery_region_type: str = "PRI",
    min_delivery_percent: int | None = None,
    max_delivery_days: int | None = None,
    min_quantity: int | None = None,
    max_result_price: float | None = None,
    max_one_detail_offers_count: int | None = 10,
    detail_nums_to_load: list[str] | None = None,
    timeout: float = 20.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    request_plan = build_emex_find_detail_request(
        part_number=part_number,
        brand=brand,
        subst_level=subst_level,
        subst_filter=subst_filter,
        delivery_region_type=delivery_region_type,
        min_delivery_percent=min_delivery_percent,
        max_delivery_days=max_delivery_days,
        min_quantity=min_quantity,
        max_result_price=max_result_price,
        max_one_detail_offers_count=max_one_detail_offers_count,
        detail_nums_to_load=detail_nums_to_load,
    )
    base = {
        "provider": "emex",
        "operation": "price_lookup",
        "emex_method": "FindDetailAdv5",
        "docs_url": EMEX_SEARCH_DOCS_URL,
        "role": "Official Emex SOAP read-only price/stock/lead-time lookup by exact article.",
        "request_plan": {key: value for key, value in request_plan.items() if key != "body"},
        "privacy": {"raw_identifier_is_sensitive": False, "secret_exposed": False},
    }
    if request_plan["missing_params"]:
        return {
            **base,
            "ok": False,
            "missing_params": request_plan["missing_params"],
            "error": "part_number is required.",
        }
    if request_plan["missing_env_names"]:
        return {
            **base,
            "ok": False,
            "missing_env_names": request_plan["missing_env_names"],
            "error": "EMEX_LOGIN and EMEX_PASSWORD are required for live Emex SOAP requests; Emex must also whitelist the server IP.",
        }
    if dry_run:
        return {**base, "ok": True, "dry_run": True}

    request = Request(
        request_plan["endpoint"],
        data=str(request_plan["body"]).encode("utf-8"),
        headers={
            "User-Agent": "AutostopManager/0.1",
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": request_plan["soap_action"],
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw_xml_bytes = response.read(EMEX_MAX_RESPONSE_BYTES + 1)
        if len(raw_xml_bytes) > EMEX_MAX_RESPONSE_BYTES:
            raise ValueError("xml_response_too_large")
        raw_xml = raw_xml_bytes.decode("utf-8", errors="replace")
        parsed = parse_emex_find_detail_response(raw_xml)
    except (HTTPError, URLError, TimeoutError, ParseError, ValueError) as exc:
        return {**base, "ok": False, "error": str(exc)}

    return {
        **base,
        "ok": bool(parsed.get("is_success")),
        "is_success": parsed.get("is_success"),
        "error_message": parsed.get("error_message"),
        "block_date_end": parsed.get("block_date_end"),
        "items": parsed.get("details") or [],
    }


def _exist_office_id(office_id: int | str | None) -> int:
    try:
        return int(office_id or EXIST_DEFAULT_OFFICE_ID)
    except (TypeError, ValueError):
        return EXIST_DEFAULT_OFFICE_ID


def _exist_base_url(base_url: str | None = None) -> str:
    return (base_url or os.getenv("EXIST_BASE_URL") or EXIST_BASE_URL).rstrip("/")


def _clean_exist_text(value: Any) -> str:
    text = unescape(str(value or "")).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _exist_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    match = re.search(r"-?\d+", str(value or "").replace("\xa0", " "))
    return int(match.group(0)) if match else None


def _exist_number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else value
    raw = str(value or "").replace("\xa0", "").replace(" ", "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", raw)
    if not match:
        return None
    number = float(match.group(0))
    return int(number) if number.is_integer() else number


def _exist_absolute_url(url: str | None, *, base_url: str | None = None) -> str | None:
    clean_url = str(url or "").strip()
    if not clean_url:
        return None
    if clean_url.startswith(("http://", "https://")):
        return clean_url
    if clean_url.startswith("/"):
        return f"{_exist_base_url(base_url)}{clean_url}"
    return f"{_exist_base_url(base_url)}/{clean_url}"


def _exist_pid_from_url(url: str | None) -> str | None:
    parsed = urlsplit(str(url or ""))
    for key, value in parse_qsl(parsed.query):
        if key.lower() == "pid" and value:
            return value
    return None


class _ExistCatalogCandidateParser(HTMLParser):
    def __init__(self, *, base_url: str | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.candidates: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None
        self._anchor_depth = 0
        self._brand_depth = 0
        self._name_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "a" and self._current is None:
            href = attr_map.get("href", "")
            if "/Price/" in href and "pid=" in href:
                self._current = {"href": href, "text": [], "brand": [], "name": []}
                self._anchor_depth = 1
            return
        if self._current is None:
            return
        if tag.lower() == "a":
            self._anchor_depth += 1
        if tag.lower() in {"b", "strong"}:
            self._brand_depth += 1
        if tag.lower() == "dd":
            self._name_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return
        if tag.lower() in {"b", "strong"}:
            self._brand_depth = max(0, self._brand_depth - 1)
        if tag.lower() == "dd":
            self._name_depth = max(0, self._name_depth - 1)
        if tag.lower() == "a":
            self._anchor_depth -= 1
            if self._anchor_depth <= 0:
                self._finish_current()

    def handle_data(self, data: str) -> None:
        if self._current is None:
            return
        self._current["text"].append(data)
        if self._brand_depth:
            self._current["brand"].append(data)
        if self._name_depth:
            self._current["name"].append(data)

    def _finish_current(self) -> None:
        current = self._current or {}
        href = str(current.get("href") or "")
        brand = _clean_exist_text(" ".join(current.get("brand") or [])) or None
        name = _clean_exist_text(" ".join(current.get("name") or [])) or None
        all_text = _clean_exist_text(" ".join(current.get("text") or []))
        part_number = all_text
        for value in (brand, name):
            if value:
                part_number = re.sub(re.escape(value), " ", part_number, count=1, flags=re.IGNORECASE)
        part_number = _clean_exist_text(part_number)
        pid = _exist_pid_from_url(href)
        if pid:
            self.candidates.append(
                {
                    "brand": brand,
                    "part_number": part_number or None,
                    "name": name,
                    "pid": pid,
                    "url": _exist_absolute_url(href, base_url=self.base_url),
                }
            )
        self._current = None
        self._anchor_depth = 0
        self._brand_depth = 0
        self._name_depth = 0


class _ExistTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.titles: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if key.lower() == "title" and value:
                self.titles.append(_clean_exist_text(value))

    def handle_data(self, data: str) -> None:
        self.text_parts.append(data)

    @property
    def text(self) -> str:
        return _clean_exist_text(" ".join(self.text_parts))


class _ExistInputParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "input":
            return
        attr_map = {key.lower(): value or "" for key, value in attrs}
        key = attr_map.get("id") or attr_map.get("name")
        if key:
            self.values[key] = attr_map.get("value", "")


def _exist_html_text(fragment: Any) -> str | None:
    if fragment in (None, ""):
        return None
    parser = _ExistTextParser()
    parser.feed(str(fragment))
    return parser.text or None


def _exist_first_html_title(fragment: Any) -> str | None:
    if fragment in (None, ""):
        return None
    parser = _ExistTextParser()
    parser.feed(str(fragment))
    return parser.titles[0] if parser.titles else None


def _extract_exist_data_array_text(html_text: str) -> str | None:
    match = re.search(r"\bvar\s+_data\s*=", html_text)
    if not match:
        return None
    start = html_text.find("[", match.end())
    if start == -1:
        return None
    depth = 0
    in_string = False
    quote_char = ""
    escaped = False
    for index in range(start, len(html_text)):
        char = html_text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote_char:
                in_string = False
            continue
        if char in {'"', "'"}:
            in_string = True
            quote_char = char
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return html_text[start : index + 1]
    return None


def _exist_hidden_fields(html_text: str) -> dict[str, str]:
    parser = _ExistInputParser()
    parser.feed(html_text)
    return {
        key: parser.values[key]
        for key in ("hdnPid", "hfPidHash", "hfSrcId")
        if parser.values.get(key) not in (None, "")
    }


def _exist_total_offers(html_text: str) -> int | None:
    match = re.search(r"Нашлось\s+предложений\s*:\s*(\d+)", html_text, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _exist_warehouse_hint(raw_offer: dict[str, Any]) -> str | None:
    color = str(raw_offer.get("highlightColor") or raw_offer.get("HighlightColor") or "").strip().upper()
    hints = {
        "D3E8CF": "central_exist_stock",
        "E9E9E9": "verified_original_supplier",
        "FFE6ED": "selected_office_stock",
    }
    return hints.get(color)


def _exist_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return None
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "да"}:
        return True
    if normalized in {"false", "0", "no", "нет"}:
        return False
    return None


def _exist_offer(raw_offer: dict[str, Any], *, offer_type: str) -> dict[str, Any]:
    statistic_html = raw_offer.get("StatisticHTML") or raw_offer.get("statisticHTML") or raw_offer.get("deliveryHTML")
    availability_html = (
        raw_offer.get("availString") or raw_offer.get("AvailString") or raw_offer.get("availabilityHTML")
    )
    price_label = (
        raw_offer.get("priceString")
        or raw_offer.get("PriceString")
        or raw_offer.get("priceLabel")
        or raw_offer.get("PriceLabel")
    )
    lead_time_label = (
        _exist_html_text(statistic_html)
        or _clean_exist_text(raw_offer.get("deliveryString") or raw_offer.get("DeliveryString"))
        or None
    )
    availability_label = (
        _exist_first_html_title(availability_html)
        or _exist_html_text(availability_html)
        or _clean_exist_text(raw_offer.get("availability") or raw_offer.get("Availability"))
        or None
    )
    return {
        "offer_type": offer_type,
        "price_rub": _exist_number(raw_offer.get("price") or raw_offer.get("Price") or raw_offer.get("priceRub")),
        "price_label": _clean_exist_text(price_label) or None,
        "lead_time_minutes": _exist_int(
            raw_offer.get("minutes") or raw_offer.get("Minutes") or raw_offer.get("deliveryMinutes")
        ),
        "lead_time_label": lead_time_label,
        "lead_time_date": _exist_first_html_title(statistic_html),
        "availability_label": availability_label,
        "pack": raw_offer.get("pack")
        or raw_offer.get("Pack")
        or raw_offer.get("lotQuantity")
        or raw_offer.get("LotQuantity"),
        "not_return": _exist_bool(
            raw_offer.get("notReturn") if "notReturn" in raw_offer else raw_offer.get("NotReturn")
        ),
        "warehouse_hint": _exist_warehouse_hint(raw_offer),
    }


def _exist_raw_offer_list(raw_value: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_value, list):
        return []
    return [item for item in raw_value if isinstance(item, dict)]


def _exist_item(raw_item: dict[str, Any], *, max_offers: int = 10, base_url: str | None = None) -> dict[str, Any]:
    pid = (
        raw_item.get("ProductIdEnc")
        or raw_item.get("productIdEnc")
        or raw_item.get("ProductID")
        or raw_item.get("ProductId")
        or raw_item.get("pid")
    )
    aggregated = _exist_raw_offer_list(raw_item.get("AggregatedParts") or raw_item.get("aggregatedParts"))
    direct = _exist_raw_offer_list(raw_item.get("DirectOffers") or raw_item.get("directOffers"))
    offers = [
        *[_exist_offer(offer, offer_type="aggregated") for offer in aggregated],
        *[_exist_offer(offer, offer_type="direct") for offer in direct],
    ][: _clamp_page_size(max_offers, default=10, maximum=50)]
    is_original = _exist_bool(raw_item.get("IsOriginal") if "IsOriginal" in raw_item else raw_item.get("isOriginal"))
    return {
        "brand": raw_item.get("CatalogName")
        or raw_item.get("catalogName")
        or raw_item.get("Brand")
        or raw_item.get("brand"),
        "part_number": raw_item.get("PartNumber") or raw_item.get("partNumber"),
        "name": raw_item.get("PartName")
        or raw_item.get("Name")
        or raw_item.get("Description")
        or raw_item.get("partName"),
        "pid": str(pid) if pid not in (None, "") else None,
        "product_url": f"{_exist_base_url(base_url)}/Price/?{urlencode({'pid': str(pid)})}"
        if pid not in (None, "")
        else None,
        "is_original": is_original,
        "block_text": raw_item.get("BlockName")
        or raw_item.get("BlockText")
        or raw_item.get("Block")
        or raw_item.get("blockName"),
        "block_type_id": _exist_int(raw_item.get("BlockTypeId") or raw_item.get("blockTypeId")),
        "price_count": _exist_int(raw_item.get("PriceCount") or raw_item.get("priceCount")),
        "min_price_rub": _exist_number(
            raw_item.get("MinPrice") or raw_item.get("MinPriceString") or raw_item.get("minPriceString")
        ),
        "min_price_label": _clean_exist_text(raw_item.get("MinPriceString") or raw_item.get("minPriceString")) or None,
        "min_delivery_label": _clean_exist_text(
            raw_item.get("MinDeliveryDaysString")
            or raw_item.get("DeliveryDaysString")
            or raw_item.get("minDeliveryDaysString")
        )
        or None,
        "offers": offers,
    }


def parse_exist_catalog_candidates(
    html_text: str,
    *,
    base_url: str | None = None,
    max_candidates: int = 5,
) -> dict[str, Any]:
    parser = _ExistCatalogCandidateParser(base_url=base_url)
    parser.feed(html_text)
    candidates = parser.candidates[: _clamp_page_size(max_candidates, default=5, maximum=50)]
    return {"ok": True, "candidates": candidates, "candidate_count": len(parser.candidates)}


def parse_exist_price_page(
    html_text: str,
    *,
    base_url: str | None = None,
    max_offers: int = 10,
) -> dict[str, Any]:
    array_text = _extract_exist_data_array_text(html_text)
    if array_text is None:
        return {
            "ok": False,
            "items": [],
            "hidden_fields": _exist_hidden_fields(html_text),
            "total_offers": _exist_total_offers(html_text),
            "error": "Exist _data array not found.",
        }
    raw_items = json.loads(array_text)
    if not isinstance(raw_items, list):
        raw_items = []
    return {
        "ok": True,
        "items": [
            _exist_item(item, max_offers=max_offers, base_url=base_url) for item in raw_items if isinstance(item, dict)
        ],
        "hidden_fields": _exist_hidden_fields(html_text),
        "total_offers": _exist_total_offers(html_text),
    }


def build_exist_price_lookup_request(
    *,
    part_number: str | None = None,
    brand: str | None = None,
    pid: str | None = None,
    office_id: int | str = EXIST_DEFAULT_OFFICE_ID,
    max_candidates: int = 5,
    max_offers: int = 10,
    include_more_offers: bool = False,
    base_url: str | None = None,
) -> dict[str, Any]:
    clean_part = str(part_number or "").strip()
    clean_brand = str(brand or "").strip() or None
    clean_pid = str(pid or "").strip()
    actual_base_url = _exist_base_url(base_url)
    actual_office_id = _exist_office_id(office_id)
    search_url = f"{actual_base_url}/Api/Parts/Search?{urlencode({'searchString': clean_part})}" if clean_part else None
    pcode_url = f"{actual_base_url}/Price/?{urlencode({'pcode': clean_part})}" if clean_part else None
    pid_url = f"{actual_base_url}/Price/?{urlencode({'pid': clean_pid})}" if clean_pid else None
    return {
        "ok": bool(clean_part or clean_pid),
        "provider": "exist",
        "method": "GET",
        "office_id": actual_office_id,
        "office_cookie": f"_go={actual_office_id}",
        "base_url": actual_base_url,
        "search_url": search_url,
        "pcode_url": pcode_url,
        "pid_url": pid_url,
        "more_offers_endpoint": f"{actual_base_url}/Price/Default.aspx/GetQuery",
        "params": {
            "part_number": clean_part or None,
            "brand": clean_brand,
            "pid": clean_pid or None,
            "max_candidates": _clamp_page_size(max_candidates, default=5, maximum=50),
            "max_offers": _clamp_page_size(max_offers, default=10, maximum=50),
            "include_more_offers": bool(include_more_offers),
        },
        "access_mode": "public_site_read_only",
        "secret_exposed": False,
    }


def _exist_headers(*, office_id: int, accept: str) -> dict[str, str]:
    return {
        "User-Agent": "AutostopManager/0.1",
        "Accept": accept,
        "Cookie": f"_go={office_id}",
    }


def _exist_read_text(url: str, *, office_id: int, timeout: float) -> str:
    request = Request(url, headers=_exist_headers(office_id=office_id, accept="text/html,application/xhtml+xml"))
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _exist_read_json(url: str, *, office_id: int, timeout: float) -> Any:
    request = Request(url, headers=_exist_headers(office_id=office_id, accept="application/json"))
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _exist_search_suggestions(payload: Any, *, base_url: str | None = None) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    suggestions = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        suggestions.append(
            {
                "header_text": item.get("HeaderText"),
                "name": item.get("Name"),
                "input_text": item.get("InputText"),
                "navigate_url": _exist_absolute_url(item.get("NavigateUrl"), base_url=base_url),
                "relevance": item.get("Relevance"),
            }
        )
    return suggestions


def _exist_brand_key(value: Any) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", str(value or "").strip().lower())


def _select_exist_candidate(candidates: list[dict[str, Any]], *, brand: str | None) -> dict[str, Any] | None:
    if not candidates:
        return None
    if not brand:
        return candidates[0] if len(candidates) == 1 else None
    requested = _exist_brand_key(brand)
    for candidate in candidates:
        if _exist_brand_key(candidate.get("brand")) == requested:
            return candidate
    for candidate in candidates:
        candidate_key = _exist_brand_key(candidate.get("brand"))
        if requested and (requested in candidate_key or candidate_key in requested):
            return candidate
    return None


def _exist_more_offers(
    *,
    request_plan: dict[str, Any],
    pid: str,
    hidden_fields: dict[str, str],
    office_id: int,
    max_offers: int,
    timeout: float,
) -> dict[str, Any]:
    text_value = hidden_fields.get("hfPidHash")
    src_id = hidden_fields.get("hfSrcId") or "RawPartNumber"
    actual_pid = hidden_fields.get("hdnPid") or pid
    if not text_value or not actual_pid:
        return {"ok": False, "offers": [], "error": "Exist more-offers hidden fields are missing."}
    payload = json.dumps(
        {
            "ProductID": actual_pid,
            "OriginalProductID": pid,
            "textValue": text_value,
            "srcId": src_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        request_plan["more_offers_endpoint"],
        data=payload,
        headers={
            **_exist_headers(office_id=office_id, accept="application/json"),
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        wrapper = json.loads(response.read().decode("utf-8", errors="replace"))
    raw_offers = json.loads(wrapper.get("d") or "[]") if isinstance(wrapper, dict) else []
    offers = [_exist_offer(offer, offer_type="direct") for offer in _exist_raw_offer_list(raw_offers)][
        : _clamp_page_size(max_offers, default=10, maximum=50)
    ]
    return {"ok": True, "offers": offers, "offer_count": len(_exist_raw_offer_list(raw_offers))}


def exist_price_lookup(
    *,
    part_number: str | None = None,
    brand: str | None = None,
    pid: str | None = None,
    office_id: int | str = EXIST_DEFAULT_OFFICE_ID,
    max_candidates: int = 5,
    max_offers: int = 10,
    include_more_offers: bool = False,
    timeout: float = 20.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    request_plan = build_exist_price_lookup_request(
        part_number=part_number,
        brand=brand,
        pid=pid,
        office_id=office_id,
        max_candidates=max_candidates,
        max_offers=max_offers,
        include_more_offers=include_more_offers,
    )
    base = {
        "provider": "exist",
        "operation": "price_lookup",
        "docs_url": EXIST_OPEN_SEARCH_DOCS_URL,
        "role": "Public read-only Exist exact article catalog/price lookup for retail benchmark, lead time, and analog visibility.",
        "office": {
            "id": request_plan["office_id"],
            "name": EXIST_DEFAULT_OFFICE_NAME if request_plan["office_id"] == EXIST_DEFAULT_OFFICE_ID else None,
        },
        "benchmark_kind": "public_retail_reference",
        "requires_confirmation": True,
        "request_plan": request_plan,
        "privacy": {"raw_identifier_is_sensitive": False, "secret_exposed": False, "returns_raw_html": False},
    }
    if not request_plan["ok"]:
        return {**base, "ok": False, "error": "part_number or pid is required."}
    if dry_run:
        return {**base, "ok": True, "dry_run": True}

    max_candidates_value = request_plan["params"]["max_candidates"]
    max_offers_value = request_plan["params"]["max_offers"]
    office = request_plan["office_id"]
    suggestions: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    selected_candidate: dict[str, Any] | None = None

    try:
        price_url = request_plan["pid_url"]
        if not price_url:
            search_payload = _exist_read_json(request_plan["search_url"], office_id=office, timeout=timeout)
            suggestions = _exist_search_suggestions(search_payload, base_url=request_plan["base_url"])
            pcode_html = _exist_read_text(request_plan["pcode_url"], office_id=office, timeout=timeout)
            candidate_result = parse_exist_catalog_candidates(
                pcode_html,
                base_url=request_plan["base_url"],
                max_candidates=max_candidates_value,
            )
            candidates = candidate_result["candidates"]
            if candidates:
                selected_candidate = _select_exist_candidate(candidates, brand=brand)
                if selected_candidate is None:
                    return {
                        **base,
                        "ok": True,
                        "search_suggestions": suggestions,
                        "candidates": candidates,
                        "candidate_count": candidate_result["candidate_count"],
                        "needs_disambiguation": True,
                        "selected_item": None,
                        "error": "Multiple Exist catalog candidates found; pass --brand to select one.",
                    }
                price_url = selected_candidate["url"]
            else:
                parsed_pcode = parse_exist_price_page(
                    pcode_html, base_url=request_plan["base_url"], max_offers=max_offers_value
                )
                selected_item = parsed_pcode["items"][0] if parsed_pcode.get("items") else None
                return {
                    **base,
                    "ok": parsed_pcode["ok"],
                    "search_suggestions": suggestions,
                    "candidates": [],
                    "needs_disambiguation": False,
                    "selected_item": selected_item,
                    "items": parsed_pcode.get("items", []),
                    "total_offers": parsed_pcode.get("total_offers"),
                    "error": parsed_pcode.get("error"),
                }

        price_html = _exist_read_text(price_url, office_id=office, timeout=timeout)
        parsed_price = parse_exist_price_page(
            price_html, base_url=request_plan["base_url"], max_offers=max_offers_value
        )
        selected_item = parsed_price["items"][0] if parsed_price.get("items") else None
        if selected_item and selected_candidate:
            selected_item["catalog_candidate"] = selected_candidate
        if selected_item and include_more_offers and selected_item.get("pid"):
            more_offers = _exist_more_offers(
                request_plan=request_plan,
                pid=str(selected_item["pid"]),
                hidden_fields=parsed_price.get("hidden_fields") or {},
                office_id=office,
                max_offers=max_offers_value,
                timeout=timeout,
            )
            selected_item["more_offers_loaded"] = more_offers.get("ok") is True
            selected_item["more_offers_count"] = more_offers.get("offer_count", 0)
            if more_offers.get("ok"):
                selected_item["offers"] = more_offers["offers"]
            else:
                selected_item["more_offers_error"] = more_offers.get("error")
        return {
            **base,
            "ok": parsed_price["ok"],
            "search_suggestions": suggestions,
            "candidates": candidates,
            "needs_disambiguation": False,
            "selected_item": selected_item,
            "items": parsed_price.get("items", []),
            "total_offers": parsed_price.get("total_offers"),
            "error": parsed_price.get("error"),
        }
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        return {**base, "ok": False, "search_suggestions": suggestions, "candidates": candidates, "error": str(exc)}


def _vin17_credentials() -> dict[str, Any]:
    load_runtime_env()
    user = os.getenv("VIN17_ACCOUNT") or ""
    secret = os.getenv("VIN17_SECRET") or ""
    missing = [name for name, value in {"VIN17_ACCOUNT": user, "VIN17_SECRET": secret}.items() if not value]
    return {"configured": not missing, "user": user, "secret": secret, "missing_env_names": missing}


def _partsapi_credentials() -> dict[str, Any]:
    load_runtime_env()
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


def _partsapi_method_key(method: str) -> tuple[str, str | None]:
    env_name = PARTSAPI_METHOD_KEY_ENV_NAMES.get(method)
    return (os.getenv(env_name) or "", env_name) if env_name else ("", None)


def _parts_catalogs_credentials() -> dict[str, Any]:
    load_runtime_env()
    key = os.getenv("PARTS_CATALOGS_API_KEY") or ""
    base_url = os.getenv("PARTS_CATALOGS_BASE_URL") or ""
    missing = [
        name
        for name, value in {"PARTS_CATALOGS_API_KEY": key, "PARTS_CATALOGS_BASE_URL": base_url}.items()
        if not value
    ]
    return {"configured": not missing, "key": key, "base_url": base_url, "missing_env_names": missing}


def _emex_credentials() -> dict[str, Any]:
    load_runtime_env()
    login = os.getenv("EMEX_LOGIN") or ""
    password = os.getenv("EMEX_PASSWORD") or ""
    service_url = os.getenv("EMEX_SERVICE_URL") or os.getenv("EMEX_SEARCH_SERVICE_URL") or EMEX_SEARCH_SERVICE_URL
    missing = [name for name, value in {"EMEX_LOGIN": login, "EMEX_PASSWORD": password}.items() if not value]
    return {
        "configured": not missing,
        "login": login,
        "password": password,
        "service_url": service_url,
        "missing_env_names": missing,
    }


def _redact_account(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        return ""
    if len(clean) <= 4:
        return f"{clean[:1]}***"
    return f"{clean[:2]}***{clean[-2:]}"


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


def extract_oem_candidates(
    *, provider: str, payload: dict[str, Any], operation: str | None = None
) -> list[dict[str, Any]]:
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
            "raw_keys": sorted(str(key) for key in item),
        }
        identity = (normalized["part_number"], normalized["brand"], normalized["name"])
        if identity in seen:
            continue
        seen.add(identity)
        candidates.append(normalized)
    return candidates


_PARTSAPI_PROFILE_FIELDS: dict[str, tuple[str, ...]] = {
    "make": ("manuName", "manuShortName", "brand", "brend"),
    "catalog": ("catalog", "katalog"),
    "model": ("modelName", "model", "modely"),
    "engine": ("motorCodes", "motorType", "engine", "dvigately"),
    "modification": ("typeName", "modification", "modifikacii"),
    "market": ("market", "rynok"),
    "production_date": ("date", "data_vypuska"),
    "options": ("options", "opcii"),
    "body": ("bodyStyle", "bodystyle", "kuzov", "kuzova"),
    "grade": ("grade", "komplektaciya"),
    "transmission": ("kp", "kpp"),
    "tecdoc_car_id": ("carId", "typeNumber"),
    "tecdoc_external_id": ("TecDocExternalId",),
    "tecrmi_external_id": ("TecRmiExternalId",),
    "model_year_from": ("yearOfConstrFrom", "modelyearfrom"),
    "model_year_to": ("yearOfConstrTo", "modelyearto"),
    "plant": ("plant",),
    "frame_color": ("framecolor", "cvet_kuzova"),
    "trim_color": ("trimcolor", "cvet_salona"),
    "paint_type": ("painttype",),
    "fuel_type": ("fuelType",),
    "brake_system": ("brakeSystem", "brakeType"),
    "displacement_cc": ("cylinderCapacityCcm", "ccmTech"),
    "power_hp_from": ("powerHpFrom",),
    "power_hp_to": ("powerHpTo",),
    "power_kw_from": ("powerKwFrom",),
    "power_kw_to": ("powerKwTo",),
    "engine_id": ("ENG_ID", "engineId", "motorId"),
    "engine_code": ("ENG_CODE", "engineCode"),
    "engine_name": ("ENG_NAME", "engineName", "engineSalesName"),
    "cylinders": ("ENG_CYLINDERS", "cylinders"),
    "valves": ("ENG_VALVES", "valves"),
    "fuel_supply": ("ENG_FUEL_SUPPLY", "fuelSupply"),
}


def _partsapi_vehicle_profile_from_item(item: dict[str, Any], *, operation: str | None = None) -> dict[str, Any]:
    profile = {
        "provider": "partsapi_ru",
        "source_operation": operation,
        "raw_keys": sorted(str(key) for key in item),
    }
    for normalized_key, source_keys in _PARTSAPI_PROFILE_FIELDS.items():
        value = _first_value(item, source_keys)
        if value not in (None, ""):
            profile[normalized_key] = value

    identifier = _first_value(item, ("vin", "VIN", "frame", "FRAME"))
    if identifier not in (None, ""):
        profile["redacted_identifier"] = _redact_identifier(str(identifier))
    return profile


def _partsapi_plate_vin_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [record for item in payload for record in _partsapi_plate_vin_records(item)]
    if not isinstance(payload, dict):
        return []
    if _first_value(payload, ("vin", "VIN")) not in (None, ""):
        return [payload]
    return [
        record
        for key in ("data", "result", "array", "items")
        for record in _partsapi_plate_vin_records(payload.get(key))
    ]


def extract_partsapi_vehicle_profiles(*, payload: dict[str, Any], operation: str | None = None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    items: list[dict[str, Any]] = []
    if operation == "vin_decode":
        result = payload.get("result")
        if isinstance(result, dict):
            items.extend(item for item in result.values() if isinstance(item, dict))
        elif isinstance(result, list):
            items.extend(item for item in result if isinstance(item, dict))
    elif operation == "vin_decode_oe":
        data = payload.get("data")
        array = data.get("array") if isinstance(data, dict) else None
        if isinstance(array, dict):
            items.append(array)
        elif isinstance(array, list):
            items.extend(item for item in array if isinstance(item, dict))
    elif operation == "engine_info":
        for key in ("data", "result", "array"):
            value = payload.get(key)
            if isinstance(value, dict):
                nested = value.get("array") if isinstance(value.get("array"), (dict, list)) else value
                if isinstance(nested, dict):
                    items.append(nested)
                elif isinstance(nested, list):
                    items.extend(item for item in nested if isinstance(item, dict))
            elif isinstance(value, list):
                items.extend(item for item in value if isinstance(item, dict))
    elif operation == "plate_to_vin":
        items.extend(_partsapi_plate_vin_records(payload))

    return [_partsapi_vehicle_profile_from_item(item, operation=operation) for item in items]


_AUTONORMS_FIELDS: dict[str, tuple[str, ...]] = {
    "norms_makes": ("makeName", "makeNameSEO"),
    "norms_models": ("makeName", "model", "kuzov", "modelId", "fuel", "years"),
    "norms_motors": (
        "engineCode",
        "engineSalesName",
        "fuel",
        "kuzov",
        "makeName",
        "model",
        "modification_name",
        "motorId",
        "year",
    ),
    "norms_times": (
        "SubCat",
        "TopCat",
        "motorId",
        "parent",
        "SubCatId",
        "TopCatId",
        "workId",
        "workName",
        "workPrice",
        "workTarget",
        "workTime",
    ),
}

_FILL_VOLUME_FIELDS = ("fillVolume", "fillUnit", "fillType", "fillTitle", "fillInfo")


def _partsapi_autonorms_records(payload: Any, *, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    if any(field in payload for field in fields):
        return [payload]
    records: list[dict[str, Any]] = []
    for key in ("result", "data", "array", "items"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            records.extend(_partsapi_autonorms_records(nested, fields=fields))
        elif isinstance(nested, list):
            records.extend(item for item in nested if isinstance(item, dict))
    return records


def extract_partsapi_autonorms_rows(*, payload: Any, operation: str) -> list[dict[str, Any]]:
    """Return a compact, provider-neutral AUTONORMS row set without pricing it."""

    fields = _AUTONORMS_FIELDS.get(operation, ())
    if not fields:
        return []
    rows: list[dict[str, Any]] = []
    for item in _partsapi_autonorms_records(payload, fields=fields):
        row = {
            "provider": "partsapi_ru",
            "source_operation": operation,
            "raw_keys": sorted(str(key) for key in item),
        }
        row.update({field: item[field] for field in fields if item.get(field) not in (None, "")})
        if len(row) > 3:
            rows.append(row)
    return rows


def extract_partsapi_fill_volumes(*, payload: Any) -> list[dict[str, Any]]:
    """Return compact fluid-volume evidence without selecting a product or approval."""

    rows: list[dict[str, Any]] = []
    for item in _partsapi_autonorms_records(payload, fields=_FILL_VOLUME_FIELDS):
        row = {
            "provider": "partsapi_ru",
            "source_operation": "fill_volumes",
            "raw_keys": sorted(str(key) for key in item),
        }
        row.update({field: item[field] for field in _FILL_VOLUME_FIELDS if item.get(field) not in (None, "")})
        if len(row) > 3:
            rows.append(row)
    return rows


def _partsapi_parts_by_vin_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("parts"), str):
        return [payload]
    records: list[dict[str, Any]] = []
    for key in ("result", "data", "items"):
        nested = payload.get(key)
        if isinstance(nested, list):
            records.extend(item for item in nested if isinstance(item, dict))
        elif isinstance(nested, dict):
            records.extend(_partsapi_parts_by_vin_records(nested))
    return records


def extract_partsapi_parts_by_vin_candidates(
    *, payload: Any, operation: str | None = "parts_by_vin"
) -> list[dict[str, Any]]:
    candidates = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for item in _partsapi_parts_by_vin_records(payload):
        raw_parts = str(item.get("parts") or "").strip()
        if not raw_parts:
            continue
        tokens = [token.strip() for token in raw_parts.split("|") if token.strip()]
        if not tokens:
            continue

        pairs: list[tuple[str | None, str]] = []
        if len(tokens) == 1:
            pairs.append((None, tokens[0]))
        else:
            for index in range(0, len(tokens) - 1, 2):
                pairs.append((tokens[index], tokens[index + 1]))
            if len(tokens) % 2:
                pairs.append((None, tokens[-1]))

        evidence = {
            "group": item.get("group"),
            "category_name": item.get("name"),
            "shortname": item.get("shortname"),
            "is_fit_for_this_vin": True,
        }
        evidence = {key: value for key, value in evidence.items() if value not in (None, "")}
        for brand, part_number in pairs:
            normalized_part_number = str(part_number or "").strip()
            if not normalized_part_number:
                continue
            identity = (normalized_part_number, brand, item.get("shortname") or item.get("name"))
            if identity in seen:
                continue
            seen.add(identity)
            candidates.append(
                {
                    "provider": "partsapi_ru",
                    "part_number": normalized_part_number,
                    "brand": brand,
                    "name": item.get("shortname") or item.get("name"),
                    "source_operation": operation,
                    "fitment_evidence": evidence,
                    "confidence": _candidate_confidence({"is_fit_for_this_vin": True}, evidence),
                    "raw_keys": sorted(str(key) for key in item),
                }
            )
    return candidates


def _partsapi_cross_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    if any(key in payload for key in ("crossBrand", "crossNumber", "partNumber")):
        return [payload]
    records: list[dict[str, Any]] = []
    for key in ("result", "data", "items", "crosses"):
        nested = payload.get(key)
        if isinstance(nested, list):
            records.extend(item for item in nested if isinstance(item, dict))
        elif isinstance(nested, dict):
            records.extend(_partsapi_cross_records(nested))
    return records


def extract_partsapi_cross_candidates(*, payload: Any, operation: str | None = None) -> list[dict[str, Any]]:
    candidates = []
    seen: set[tuple[str | None, str, str | None, str | None]] = set()
    for item in _partsapi_cross_records(payload):
        cross_number = _first_value(item, ("crossNumber", "cross_number", "replacementNumber", "replacement_number"))
        if cross_number in (None, ""):
            continue
        source_brand = _first_value(item, ("brand", "partBrand", "sourceBrand"))
        source_part_number = _first_value(item, ("partNumber", "part_number", "number", "sourceNumber"))
        cross_brand = _first_value(
            item, ("crossBrand", "cross_brand", "replacementBrand", "replacement_brand", "brandName")
        )
        part_name = _first_value(item, ("partname", "partName", "title", "name", "shortname"))
        normalized_cross_number = str(cross_number).strip()
        identity = (cross_brand, normalized_cross_number, source_brand, source_part_number)
        if identity in seen:
            continue
        seen.add(identity)
        fitment_evidence = {
            "source": "CROSSBASE.RU",
            "fitment_confirmed": False,
        }
        if part_name not in (None, ""):
            fitment_evidence["partname"] = part_name
        candidates.append(
            {
                "provider": "partsapi_ru",
                "source_operation": operation,
                "relationship": "cross",
                "brand": cross_brand,
                "part_number": normalized_cross_number,
                "name": part_name,
                "source_brand": source_brand,
                "source_part_number": source_part_number,
                "fitment_evidence": fitment_evidence,
                "confidence": 0.6,
                "raw_keys": sorted(str(key) for key in item),
            }
        )
    return candidates


def _partsapi_article_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    if any(
        key in payload
        for key in (
            "ART_ID",
            "ART_ARTICLE_NR",
            "ART_SUP_BRAND",
            "ARL_ART_ID",
            "ARL_DISPLAY_NR",
            "ARL_BRA_BRAND",
            "partname",
        )
    ):
        return [payload]
    records: list[dict[str, Any]] = []
    for key in ("result", "data", "items", "articles"):
        nested = payload.get(key)
        if isinstance(nested, list):
            records.extend(item for item in nested if isinstance(item, dict))
        elif isinstance(nested, dict):
            records.extend(_partsapi_article_records(nested))
    return records


def extract_partsapi_article_candidates(
    *, payload: Any, operation: str | None = "search_articles"
) -> list[dict[str, Any]]:
    candidates = []
    seen: set[tuple[str | None, str | None, str | None]] = set()
    for item in _partsapi_article_records(payload):
        part_number = _first_value(
            item, ("ART_ARTICLE_NR", "ARL_DISPLAY_NR", "articleNumber", "article_number", "number")
        )
        article_id = _first_value(item, ("ART_ID", "ARL_ART_ID", "articleId", "article_id"))
        brand = _first_value(item, ("ART_SUP_BRAND", "ARL_BRA_BRAND", "brand", "supplierBrand"))
        if part_number in (None, "") and article_id in (None, ""):
            continue
        identity = (
            str(article_id) if article_id not in (None, "") else None,
            str(part_number) if part_number not in (None, "") else None,
            str(brand) if brand not in (None, "") else None,
        )
        if identity in seen:
            continue
        seen.add(identity)
        candidates.append(
            {
                "provider": "partsapi_ru",
                "source_operation": operation,
                "article_id": article_id,
                "part_number": str(part_number).strip() if part_number not in (None, "") else None,
                "brand": brand,
                "product_name": _first_value(
                    item, ("ART_PRODUCT_NAME", "productName", "product_name", "partname", "partName", "name")
                ),
                "found_via": _first_value(item, ("FOUND_VIA", "foundVia", "found_via")),
                "fitment_evidence": {"fitment_confirmed": False},
                "confidence": 0.5,
                "raw_keys": sorted(str(key) for key in item),
            }
        )
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
    url = (
        f"{actual_base_url}{path}?{query}"
        if actual_base_url and query
        else f"{actual_base_url}{path}"
        if actual_base_url
        else None
    )
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
        "redacted_url": _redact_sensitive_query_text(url) if url else None,
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
        return {
            **base,
            "ok": False,
            "missing_params": missing_params,
            "error": "Required Parts-Catalogs parameters are missing.",
        }
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
    method_key, method_key_env_name = _partsapi_method_key(method)
    actual_key = key if key is not None else method_key or credentials["key"]
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
        "method_key_env_name": method_key_env_name if method_key else None,
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
    article_id: str | int | None = None,
    brand: str | None = None,
    part_type: str | None = None,
    category: str | None = None,
    vehicle_type: str | None = None,
    type_id: str | None = None,
    lang: str | None = None,
    lang_id: int | None = None,
    registration_number: str | None = None,
    make_name_seo: str | None = None,
    model_id: str | int | None = None,
    motor_id: str | int | None = None,
    top_category_id: str | int | None = None,
    sub_category_id: str | int | None = None,
    car_id: str | int | None = None,
) -> dict[str, Any]:
    spec = PARTSAPI_OPERATIONS[operation]
    values = dict(spec.get("defaults", {}))
    values.update(
        {
            key: value
            for key, value in {
                "identifier": identifier,
                "part_number": part_number,
                "article_id": article_id,
                "brand": brand,
                "part_type": part_type,
                "category": category,
                "vehicle_type": vehicle_type,
                "type_id": type_id,
                "lang": lang,
                "lang_id": lang_id,
                "registration_number": registration_number,
                "make_name_seo": make_name_seo,
                "model_id": model_id,
                "motor_id": motor_id,
                "top_category_id": top_category_id,
                "sub_category_id": sub_category_id,
                "car_id": car_id,
            }.items()
            if value not in (None, "")
        }
    )
    if operation == "parts_by_vin" and str(part_type or "").strip().lower() in PARTSAPI_OMIT_PART_TYPE_VALUES:
        values["part_type"] = None
    params = {}
    for api_param, source_name in spec["params"].items():
        params[api_param] = values.get(source_name)
    return params


def partsapi_catalog_lookup(
    *,
    operation: str,
    identifier: str | None = None,
    part_number: str | None = None,
    article_id: str | int | None = None,
    brand: str | None = None,
    part_type: str | None = None,
    category: str | None = None,
    vehicle_type: str | None = None,
    type_id: str | None = None,
    lang: str | None = None,
    lang_id: int | None = None,
    registration_number: str | None = None,
    make_name_seo: str | None = None,
    model_id: str | int | None = None,
    motor_id: str | int | None = None,
    top_category_id: str | int | None = None,
    sub_category_id: str | int | None = None,
    car_id: str | int | None = None,
    timeout: float = 20.0,
    max_attempts: int = 1,
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
                "article_id": article_id,
                "brand": brand,
                "part_type": part_type,
                "category": category,
                "vehicle_type": vehicle_type,
                "type_id": type_id,
                "lang": lang,
                "lang_id": lang_id,
                "registration_number": registration_number,
                "make_name_seo": make_name_seo,
                "model_id": model_id,
                "motor_id": motor_id,
                "top_category_id": top_category_id,
                "sub_category_id": sub_category_id,
                "car_id": car_id,
            }.items()
            if value not in (None, "")
        }
    )
    missing_params = [name for name in spec["required"] if input_values.get(name) in (None, "")]
    params = _partsapi_operation_params(
        operation,
        identifier=identifier,
        part_number=part_number,
        article_id=article_id,
        brand=brand,
        part_type=part_type,
        category=category,
        vehicle_type=vehicle_type,
        type_id=type_id,
        lang=lang,
        lang_id=lang_id,
        registration_number=registration_number,
        make_name_seo=make_name_seo,
        model_id=model_id,
        motor_id=motor_id,
        top_category_id=top_category_id,
        sub_category_id=sub_category_id,
        car_id=car_id,
    )
    request_plan = build_partsapi_request(method=spec["method"], params=params)
    base = {
        "provider": "partsapi_ru",
        "operation": operation,
        "partsapi_method": spec["method"],
        "docs_url": spec["docs_url"],
        "role": spec["role"],
        "quota_cost_estimate": 1,
        "request_plan": _safe_request_plan(request_plan, omit={"url"}),
        "redacted_identifier": _redact_identifier(identifier or "") if identifier else None,
        "redacted_registration_number": _redact_identifier(registration_number or "") if registration_number else None,
        "privacy": {
            "raw_identifier_is_sensitive": bool(identifier or registration_number),
            "secret_exposed": False,
        },
    }
    if missing_params:
        return {
            **base,
            "ok": False,
            "missing_params": missing_params,
            "error": "Required PartsAPI parameters are missing.",
        }
    if not request_plan["configured"]:
        return {
            **base,
            "ok": False,
            "missing_env_names": request_plan["missing_env_names"],
            "error": "PARTSAPI_BASE_URL plus PARTSAPI_KEY or the method-specific PartsAPI key are required for live requests.",
        }
    if dry_run:
        return {
            **base,
            "ok": True,
            "dry_run": True,
            "attempt_count": 0,
            "max_attempts": max(1, int(max_attempts or 1)),
            "attempts": [],
        }

    request = Request(request_plan["url"], headers={"User-Agent": "AutostopManager/0.1"})
    attempts: list[dict[str, Any]] = []
    payload: Any = None
    attempt_count = max(1, int(max_attempts or 1))
    for attempt in range(1, attempt_count + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            attempts.append({"attempt": attempt, "ok": True})
            break
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            attempts.append({"attempt": attempt, "ok": False, "error": str(exc)})
    else:
        return {
            **base,
            "ok": False,
            "error": attempts[-1]["error"] if attempts else "PartsAPI request failed.",
            "attempt_count": len(attempts),
            "max_attempts": attempt_count,
            "attempts": attempts,
        }

    cross_candidates = (
        extract_partsapi_cross_candidates(payload=payload, operation=operation)
        if operation in {"crosses", "crosses_with_brand", "crosses_title"}
        else []
    )
    article_candidates = (
        extract_partsapi_article_candidates(payload=payload, operation=operation)
        if operation
        in {
            "search_articles",
            "article_crosses",
            "articles",
            "article",
            "article_criteria",
            "part_name_by_brand_number",
        }
        else []
    )
    oem_candidates = (
        []
        if operation
        in {
            "crosses",
            "crosses_with_brand",
            "crosses_title",
            "article_crosses",
            "search_articles",
            "articles",
            "article",
            "article_criteria",
            "part_name_by_brand_number",
        }
        else extract_oem_candidates(provider="partsapi_ru", payload=payload, operation=operation)
    )
    if operation == "parts_by_vin":
        oem_candidates.extend(extract_partsapi_parts_by_vin_candidates(payload=payload, operation=operation))

    return {
        **base,
        "ok": True,
        "attempt_count": len(attempts),
        "max_attempts": attempt_count,
        "attempts": attempts,
        "payload": payload,
        "empty_payload": payload in (None, [], {}),
        "vehicle_profiles": extract_partsapi_vehicle_profiles(payload=payload, operation=operation),
        "oem_candidates": oem_candidates,
        "cross_candidates": cross_candidates,
        "article_candidates": article_candidates,
        "autonorms_rows": extract_partsapi_autonorms_rows(payload=payload, operation=operation),
        "fill_volumes": extract_partsapi_fill_volumes(payload=payload) if operation == "fill_volumes" else [],
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
    token = (
        build_17vin_token(user=actual_user, secret=actual_secret, url_parameters=url_parameters) if not missing else ""
    )
    signed_query = (
        f"{query}&user={quote(actual_user)}&token={token}" if query else f"user={quote(actual_user)}&token={token}"
    )
    signed_url = f"{VIN17_BASE_URL}{clean_path}?{signed_query}"

    return {
        "ok": not missing,
        "provider": "vin17_api",
        "configured": not missing,
        "missing_env_names": missing,
        "method": "GET",
        "url_parameters": url_parameters,
        "signed_url": signed_url if not missing else None,
        "redacted_url": _without_secret_query(signed_url, {"token"}, account_param_names={"user"}),
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
        "oem_candidates": extract_oem_candidates(
            provider="vin17_api", payload=payload, operation="search_std_part_name"
        ),
    }


def _result_oem_candidates(result: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(result.get("oem_candidates"), list):
        return [candidate for candidate in result["oem_candidates"] if isinstance(candidate, dict)]
    payload = result.get("payload")
    if isinstance(payload, dict):
        return extract_oem_candidates(
            provider=str(result.get("provider") or "unknown"), payload=payload, operation=result.get("operation")
        )
    return []


def _compact_provider_result(result: dict[str, Any]) -> dict[str, Any]:
    compact = {key: value for key, value in result.items() if key not in {"payload"}}
    for bucket in ("oem_candidates", "cross_candidates", "article_candidates", "vehicle_profiles"):
        if bucket in compact:
            count_name = f"{bucket.removesuffix('s')}_count"
            compact[count_name] = len(compact[bucket]) if isinstance(compact[bucket], list) else 0
            compact.pop(bucket, None)
    return compact


def resolve_partsapi_category(
    requested_part: str | None,
    explicit_category: str | None = None,
    *,
    category_index_path: str | None = None,
) -> dict[str, Any]:
    part_profile = normalize_part_intent(requested_part)
    raw_candidates = [
        str(value).strip() for value in part_profile.get("partsapi_cat_candidates", []) if str(value).strip()
    ]
    numeric_candidates = [value for value in raw_candidates if value.isdigit()]
    text_candidates = [value for value in raw_candidates if not value.isdigit()]
    index_result = search_partsapi_category_index(
        requested_part,
        intent_id=str(part_profile.get("intent_id") or "") if part_profile.get("recognized") else None,
        path=category_index_path,
        limit=5,
    )
    index_numeric_candidates = [
        str(row.get("cat_id") or "").strip()
        for row in index_result.get("matches", [])
        if str(row.get("cat_id") or "").strip().isdigit()
    ]

    def _profile_digest() -> dict[str, Any]:
        return {
            "recognized": bool(part_profile.get("recognized")),
            "intent_id": part_profile.get("intent_id"),
            "canonical_name_ru": part_profile.get("canonical_name_ru"),
        }

    explicit = str(explicit_category or "").strip()
    if explicit:
        kind = "numeric_id" if explicit.isdigit() else "text_candidate"
        return {
            "category": explicit,
            "category_kind": kind,
            "category_unresolved": kind != "numeric_id",
            "source": "explicit",
            "numeric_candidates": numeric_candidates,
            "index_numeric_candidates": index_numeric_candidates,
            "text_candidates": text_candidates,
            "part_intent": _profile_digest(),
            "index_matches": index_result.get("matches", []),
        }

    if index_numeric_candidates:
        selected = index_result["matches"][0]
        return {
            "category": index_numeric_candidates[0],
            "category_kind": "numeric_id",
            "category_unresolved": False,
            "source": "partsapi_category_index",
            "numeric_candidates": list(dict.fromkeys(numeric_candidates + index_numeric_candidates)),
            "index_numeric_candidates": index_numeric_candidates,
            "text_candidates": text_candidates,
            "part_intent": _profile_digest(),
            "index_matches": index_result.get("matches", []),
            "selected_index_match": selected,
            "validation_required": bool(selected.get("validation_required")),
        }

    if numeric_candidates:
        return {
            "category": numeric_candidates[0],
            "category_kind": "numeric_id",
            "category_unresolved": False,
            "source": "parts_intent_numeric_candidate",
            "numeric_candidates": numeric_candidates,
            "index_numeric_candidates": index_numeric_candidates,
            "text_candidates": text_candidates,
            "part_intent": _profile_digest(),
            "index_matches": index_result.get("matches", []),
        }

    if text_candidates:
        return {
            "category": text_candidates[0],
            "category_kind": "text_candidate",
            "category_unresolved": True,
            "source": "parts_intent_text_candidate",
            "numeric_candidates": numeric_candidates,
            "index_numeric_candidates": index_numeric_candidates,
            "text_candidates": text_candidates,
            "part_intent": _profile_digest(),
            "index_matches": index_result.get("matches", []),
        }

    return {
        "category": None,
        "category_kind": "unresolved",
        "category_unresolved": True,
        "source": "none",
        "numeric_candidates": [],
        "index_numeric_candidates": index_numeric_candidates,
        "text_candidates": [],
        "part_intent": _profile_digest(),
        "index_matches": index_result.get("matches", []),
    }


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
    partsapi_part_type: str = "oem",
    partsapi_category: str | None = None,
    partsapi_category_index_path: str | None = None,
    timeout: float = 20.0,
    max_attempts: int = 1,
    dry_run: bool = False,
) -> dict[str, Any]:
    part_profile = normalize_part_intent(requested_part)
    terms = [str(term).strip() for term in part_profile.get("catalog_search_terms", []) if str(term).strip()]
    primary_term = terms[0] if terms else requested_part
    partsapi_category_resolution = resolve_partsapi_category(
        requested_part,
        explicit_category=partsapi_category,
        category_index_path=partsapi_category_index_path,
    )
    category = partsapi_category_resolution.get("category")
    part_clarification_required = bool(part_profile.get("clarification_required"))

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

    if category and (
        dry_run
        or (partsapi_category_resolution.get("category_kind") == "numeric_id" and not part_clarification_required)
    ):
        provider_results.append(
            partsapi_catalog_lookup(
                operation="parts_by_vin",
                identifier=identifier,
                part_type=partsapi_part_type,
                category=category,
                timeout=timeout,
                max_attempts=max_attempts,
                dry_run=dry_run,
            )
        )
    else:
        provider_results.append(
            partsapi_catalog_lookup(
                operation="vin_decode_oe",
                identifier=identifier,
                timeout=timeout,
                max_attempts=max_attempts,
                dry_run=dry_run,
            )
        )
        blockers.append(
            {
                "provider": "partsapi_ru",
                "operation": "parts_by_vin",
                "missing_params": ["cat"] if not category else [],
                "clarification_fields": part_profile.get("clarification_fields", [])
                if part_clarification_required
                else [],
                "category_resolution": partsapi_category_resolution,
                "fallback_operation": "vin_decode_oe",
                "error": (
                    "PartsAPI getPartsbyVIN live lookup requires explicit part position/side/axis before catalog search."
                    if part_clarification_required
                    else "PartsAPI getPartsbyVIN live lookup requires a numeric cat id; VINdecodeOE fallback is attempted for vehicle/OE catalog identity."
                ),
            }
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

    has_successful_provider = any(result.get("ok") is True for result in provider_results)
    return {
        "ok": has_successful_provider,
        "status": "completed" if oem_candidates else "inconclusive",
        "provider": "multi_oem_catalog_lookup",
        "identifier": {"redacted": _redact_identifier(identifier), "raw_identifier_is_sensitive": True},
        "requested_part": requested_part,
        "part_profile": part_profile,
        "provider_count": len(provider_results),
        "candidate_count": len(oem_candidates),
        "has_successful_provider": has_successful_provider,
        "provider_results": [_compact_provider_result(result) for result in provider_results],
        "partsapi_category_resolution": partsapi_category_resolution,
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
