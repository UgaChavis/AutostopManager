"""Read-only Baza.Drom parts search through Webbee's task API.

This adapter creates one bounded Webbee task per explicit search request. The
Webbee API's ``uid`` protects a single task start from duplicate starts, but
task creation itself has no documented idempotency key; callers must treat a
start request as non-idempotent and reconcile an uncertain response by task
ID/UID before trying again.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from http.client import HTTPException
import json
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from . import j1_fetch
from .config import load_runtime_env


WEBBEE_API_BASE_URL = "https://analytics.webbee-ai.ru"
BASE_PARTS_SEARCH_URL = "https://baza.drom.ru/{region}/sell_spare_parts/?{query}"
WEBBEE_API_TOKEN_ENV = "WEBBEE_API_TOKEN"
WEBBEE_DROM_ROBOT_ALIAS_ENV = "WEBBEE_DROM_ROBOT_ALIAS"
REQUEST_TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 8_000_000
MAX_QUERY_CHARS = 128
MAX_REGION_CHARS = 64
MAX_LIMIT = 1_000
MAX_PAGE_LIMIT = 10
MAX_LISTINGS = 1_000
_REGION_RE = re.compile(r"[a-z0-9-]{1,64}\Z")
_ROBOT_ALIAS_RE = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_UID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_NUMERIC_RE = re.compile(r"\d[\d\s\u00a0\u202f]*(?:[,.]\d{1,2})?")
_URL_RE = re.compile(r"(?i)(?:\b[a-z][a-z0-9+.-]*://|\bwww\.)")


class _NoRedirect(HTTPRedirectHandler):
    """Keep the API token on the fixed Webbee host only."""

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _failure(
    code: str,
    *,
    status: str = "failed",
    task_id: int | None = None,
    uid: str | None = None,
    outcome_uncertain: bool = False,
) -> dict[str, Any]:
    return {
        "ok": False,
        "source": "drom",
        "status": status,
        "error": code,
        "task_id": task_id,
        "uid": uid,
        "outcome_uncertain": outcome_uncertain,
    }


def _load_api_config() -> tuple[str, str] | None:
    load_runtime_env()
    token = os.environ.get(WEBBEE_API_TOKEN_ENV, "").strip()
    robot_alias = os.environ.get(WEBBEE_DROM_ROBOT_ALIAS_ENV, "").strip()
    if not token or len(token) > 4_096 or "\r" in token or "\n" in token or not _ROBOT_ALIAS_RE.fullmatch(robot_alias):
        return None
    return token, robot_alias


def _api_request(
    method: str,
    path: str,
    *,
    token: str,
    payload: Mapping[str, Any] | None = None,
    query: Mapping[str, str | int] | None = None,
) -> tuple[int, Any]:
    """Make one bounded request to the fixed Webbee origin.

    Response and exception bodies are kept internal. Public results expose only
    stable error codes so provider messages cannot leak tokens or ad content.
    """

    if not path.startswith("/webbee-api/v1.0/") or ".." in path:
        raise ValueError("webbee_path_invalid")
    url = f"{WEBBEE_API_BASE_URL}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {
        "Accept": "application/json",
        "Webbee-Api-Token": token,
        "User-Agent": "AutoStopManager/1.0",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method.upper())
    opener = build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status_code = int(response.status)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        try:
            status_code = int(exc.code)
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
        finally:
            exc.close()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("webbee_response_too_large")
    if not raw:
        return status_code, None
    try:
        return status_code, json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError):
        return status_code, None


def _normalise_query(query: Any) -> str | None:
    if not isinstance(query, str):
        return None
    clean = re.sub(r"\s+", " ", query).strip()
    if not clean or len(clean) > MAX_QUERY_CHARS or j1_fetch.contains_sensitive(clean):
        return None
    if j1_fetch.contains_unsafe_url(clean) or _URL_RE.search(clean):
        return None
    return clean


def _normalise_region(region: Any) -> str | None:
    if not isinstance(region, str):
        return None
    clean = region.strip().casefold()
    if len(clean) > MAX_REGION_CHARS or not _REGION_RE.fullmatch(clean):
        return None
    return clean


def _normalise_limit(limit: Any) -> int | None:
    if isinstance(limit, bool):
        return None
    try:
        value = int(limit)
    except (TypeError, ValueError, OverflowError):
        return None
    if value < 1 or value > MAX_LIMIT or str(value) != str(limit).strip():
        return None
    return value


def _build_search_url(query: str, region: str) -> str:
    return BASE_PARTS_SEARCH_URL.format(region=region, query=urlencode({"query": query}))


def _task_id(payload: Any) -> int | None:
    if not isinstance(payload, Mapping):
        return None
    raw_id = payload.get("id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
        return None
    try:
        value = int(raw_id)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value > 0 else None


def _task_list(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    rows = payload.get("items")
    return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def _task_matches(row: Mapping[str, Any], *, name: str, search_url: str) -> bool:
    if row.get("name") != name:
        return False
    urls = row.get("urls")
    if not isinstance(urls, str):
        return False
    return search_url in [value.strip() for value in urls.splitlines()]


def _reconcile_created_task(token: str, *, name: str, search_url: str) -> int | None:
    try:
        status_code, payload = _api_request("GET", "/webbee-api/v1.0/tasks", token=token)
    except (HTTPException, OSError, TimeoutError, URLError, ValueError):
        return None
    if status_code != 200:
        return None
    matches = [row for row in _task_list(payload) if _task_matches(row, name=name, search_url=search_url)]
    if len(matches) != 1:
        return None
    return _task_id(matches[0])


def _status_from_payload(payload: Any) -> tuple[str, dict[str, int], str | None]:
    if not isinstance(payload, Mapping):
        return "failed", {}, "webbee_status_incomplete"
    progress = payload.get("progress")
    progress_out: dict[str, int] = {}
    if isinstance(progress, Mapping):
        for key in ("notFound", "processed", "success", "withError", "collectedItemsCount", "total"):
            value = progress.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                progress_out[key] = value

    raw_status = str(payload.get("status") or "").strip().casefold()
    if raw_status in {"failed", "error", "stopped", "canceled", "cancelled"}:
        return "failed", progress_out, "webbee_task_failed"
    if raw_status in {"queued", "pending", "waiting", "created"}:
        return "queued", progress_out, None
    if raw_status in {"running", "processing", "started"}:
        return "running", progress_out, None

    completed_at = payload.get("completedAt")
    started_at = payload.get("startedAt")
    if completed_at:
        if progress_out.get("withError", 0) > 0 and progress_out.get("success", 0) == 0:
            return "failed", progress_out, "webbee_task_failed"
        return "complete", progress_out, None
    if started_at:
        return "running", progress_out, None
    # Swagger documents startedAt=null while a run is still queued. Requiring
    # the field distinguishes that confirmed queue state from a malformed body.
    if "startedAt" in payload and started_at is None:
        return "queued", progress_out, None
    return "failed", progress_out, "webbee_status_incomplete"


def _read_error_code(status_code: int, *, stage: str) -> str:
    if status_code == 401:
        return "webbee_auth_failed"
    if status_code == 403:
        return "webbee_access_denied"
    if status_code == 429:
        return "webbee_quota_or_rate_limited"
    if status_code == 404:
        return "webbee_task_not_found" if stage == "status" else "webbee_result_not_found"
    if status_code == 423:
        return "webbee_robot_blocked"
    return "webbee_status_unavailable" if stage == "status" else "webbee_result_unavailable"


def drom_start_parts_search(
    query: str,
    region: str = "krasnoyarsk",
    limit: int = 50,
    dry_run: bool = False,
    page_limit: int = 3,
) -> dict[str, Any]:
    """Queue a bounded Baza.Drom spare-parts search in Webbee.

    This operation is non-idempotent as a whole: Webbee has no documented
    idempotency key for task creation. If creation is uncertain, the adapter
    reconciles by its unique task name and URL and never repeats the POST. The
    start call carries a unique ``uid``; on an uncertain start it reads status
    once rather than issuing another paid start.
    """

    clean_query = _normalise_query(query)
    clean_region = _normalise_region(region)
    clean_limit = _normalise_limit(limit)
    clean_page_limit = _normalise_limit(page_limit)
    if clean_query is None:
        return _failure("search_query_invalid_or_sensitive")
    if clean_region is None:
        return _failure("region_invalid")
    if clean_limit is None:
        return _failure("limit_out_of_range")
    if clean_page_limit is None or clean_page_limit > MAX_PAGE_LIMIT:
        return _failure("page_limit_out_of_range")

    search_url = _build_search_url(clean_query, clean_region)
    if dry_run:
        return {
            "ok": True,
            "source": "drom",
            "status": "dry_run",
            "search_url": search_url,
            "limit": clean_limit,
            "page_limit": clean_page_limit,
            "task_id": None,
            "uid": None,
            "outcome_uncertain": False,
        }

    config = _load_api_config()
    if config is None:
        return _failure("webbee_not_configured")
    token, robot_alias = config
    operation_id = uuid4().hex
    task_name = f"autostop-drom-{operation_id}"
    uid = f"autostop-{operation_id}"
    task_payload = {
        "robot": robot_alias,
        "name": task_name,
        "urls": search_url,
        "settings": {"elementCountLimit": clean_limit, "pageCountLimit": clean_page_limit},
    }

    create_uncertain = False
    try:
        response_code, task_payload_response = _api_request(
            "POST", "/webbee-api/v1.0/tasks", token=token, payload=task_payload
        )
    except (HTTPException, OSError, TimeoutError, URLError, ValueError):
        response_code, task_payload_response = 0, None
        create_uncertain = True

    task_id = _task_id(task_payload_response) if response_code in {200, 201} else None
    if task_id is None and (
        create_uncertain or response_code == 0 or response_code in {200, 201, 408} or response_code >= 500
    ):
        task_id = _reconcile_created_task(token, name=task_name, search_url=search_url)
        if task_id is None:
            return _failure("webbee_task_creation_uncertain", status="uncertain", uid=uid, outcome_uncertain=True)
    elif task_id is None:
        code = {
            401: "webbee_auth_failed",
            403: "webbee_access_denied",
            409: "webbee_task_limit_or_conflict",
            423: "webbee_robot_blocked",
            429: "webbee_quota_or_rate_limited",
        }.get(response_code, "webbee_task_creation_failed")
        uncertain = response_code in {408, 429} or response_code >= 500
        return _failure(code, status="uncertain" if uncertain else "failed", uid=uid, outcome_uncertain=uncertain)

    start_path = f"/webbee-api/v1.0/tasks/{task_id}/start"
    try:
        start_code, _ = _api_request("PATCH", start_path, token=token, query={"uid": uid})
    except (HTTPException, OSError, TimeoutError, URLError, ValueError):
        start_code = 0

    if 200 <= start_code < 300:
        return {
            "ok": True,
            "source": "drom",
            "status": "queued",
            "task_id": task_id,
            "uid": uid,
            "search_url": search_url,
            "limit": clean_limit,
            "page_limit": clean_page_limit,
            "outcome_uncertain": False,
            "idempotent": False,
        }

    # A start response may be lost after the provider accepted the run. Query
    # the UID once to reconcile it; never issue a second start in this call.
    try:
        status_code, status_payload = _api_request(
            "GET", f"/webbee-api/v1.0/tasks/{task_id}/status", token=token, query={"uid": uid}
        )
    except (HTTPException, OSError, TimeoutError, URLError, ValueError):
        status_code, status_payload = 0, None
    if status_code == 200:
        task_status, progress, status_error = _status_from_payload(status_payload)
        if status_error is None:
            return {
                "ok": True,
                "source": "drom",
                "status": task_status,
                "task_id": task_id,
                "uid": uid,
                "search_url": search_url,
                "limit": clean_limit,
                "page_limit": clean_page_limit,
                "progress": progress,
                "outcome_uncertain": False,
                "idempotent": False,
            }
    if start_code == 423:
        return _failure("webbee_robot_blocked", task_id=task_id, uid=uid)
    if start_code == 404:
        return _failure("webbee_task_not_found", task_id=task_id, uid=uid)
    return _failure(
        "webbee_start_outcome_uncertain",
        status="uncertain",
        task_id=task_id,
        uid=uid,
        outcome_uncertain=True,
    )


def _compact_text(value: Any, *, limit: int = 2_000) -> str | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    clean = re.sub(r"\s+", " ", str(value)).strip()
    if not clean:
        return None
    return j1_fetch.redact_sensitive(clean, limit=limit)


def _normalise_key(value: Any) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", str(value or "").casefold())


def _field(row: Mapping[str, Any], *aliases: str) -> Any:
    by_key = {_normalise_key(key): value for key, value in row.items()}
    for alias in aliases:
        normalised = _normalise_key(alias)
        if normalised in by_key:
            return by_key[normalised]
    return None


def _listing_url(value: Any) -> str | None:
    raw = _compact_text(value, limit=2_048)
    if not raw:
        return None
    candidate = urljoin("https://baza.drom.ru/", raw)
    safe = j1_fetch.public_url(candidate)
    if not safe:
        return None
    parsed = urlsplit(safe)
    if parsed.hostname != "baza.drom.ru" or not parsed.path.strip("/"):
        return None
    return safe


def _parse_price(value: Any) -> tuple[int | float | None, str | None, str | None]:
    price_text = _compact_text(value, limit=160)
    if not price_text:
        return None, None, None
    folded = price_text.casefold()
    qualifier: str | None = None
    if "договор" in folded or "negotiable" in folded:
        qualifier = "negotiable"
    elif re.search(r"\bот\b|\bfrom\b", folded):
        qualifier = "from"
    elif re.search(r"\bдо\b|\bup to\b", folded):
        qualifier = "up_to"
    elif "за шт" in folded or "за штуку" in folded or "per item" in folded:
        qualifier = "per_item"
    match = _NUMERIC_RE.search(price_text)
    if not match:
        return None, price_text, qualifier
    number = re.sub(r"[\s\u00a0\u202f]", "", match.group()).replace(",", ".")
    try:
        parsed = float(number) if "." in number else int(number)
    except ValueError:
        parsed = None
    return parsed, price_text, qualifier


def _normalise_listing(row: Mapping[str, Any], *, observed_at: str) -> dict[str, Any] | None:
    url = _listing_url(
        _field(row, "Ссылка на объект", "productLink", "listingUrl", "objectUrl", "url", "link", "Ссылка")
    )
    title = _compact_text(_field(row, "Название", "title", "name"), limit=1_000)
    if not url or not title:
        return None

    raw_id = _compact_text(_field(row, "ID", "listing_id", "listingId", "objectId", "ad_id"), limit=80)
    if not raw_id:
        path = urlsplit(url).path
        provider_id_match = re.search(r"-g(\d+)\.html/?$", path)
        path_match = re.search(r"(?:^|/)(\d+)(?:\.html)?/?$", path)
        if provider_id_match:
            raw_id = f"-{provider_id_match.group(1)}"
        elif path_match:
            raw_id = path_match.group(1)
    # Webbee's public Baza.Drom sample exports some listing IDs as negative
    # numbers; their source URLs encode the same number as a "-gN.html" suffix.
    if raw_id and not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", raw_id):
        raw_id = None
    price_rub, price_text, price_qualifier = _parse_price(_field(row, "Цена", "price", "price_rub"))
    seller_name = _compact_text(_field(row, "Название продавца", "seller", "sellerName", "seller_name"), limit=240)
    delivery_label = _compact_text(
        _field(row, "Информация о доставке", "delivery", "deliveryInfo", "shipping", "delivery_cost"), limit=1_000
    )
    return {
        "source": "drom",
        "listing_id": raw_id,
        "url": url,
        "title": title,
        "description": _compact_text(_field(row, "Описание", "description", "annotation"), limit=4_000),
        "price_rub": price_rub,
        "price_text": price_text,
        "price_qualifier": price_qualifier,
        "city": _compact_text(_field(row, "Название города", "Город", "city", "address"), limit=160),
        "condition": _compact_text(_field(row, "Состояние", "condition", "itemCondition"), limit=160),
        "seller": (
            {"name": seller_name, "type": None, "rating": None, "reviews_count": None, "reviews": None}
            if seller_name is not None
            else None
        ),
        "delivery": {"available": None, "label": delivery_label} if delivery_label is not None else None,
        "availability": _compact_text(_field(row, "Наличие", "availability", "in_stock", "stock"), limit=160),
        "published_at": _compact_text(
            _field(row, "Дата создания", "published_at", "publishedAt", "created_at", "createdAt"), limit=80
        ),
        "observed_at": _compact_text(
            _field(row, "Время сбора данных", "observed_at", "observedAt", "scraped_at", "scrapedAt"), limit=80
        )
        or observed_at,
        "status": "lead",
        "fitment_confirmed": False,
        "availability_confirmed": False,
    }


def _listing_rows(payload: Any) -> list[Mapping[str, Any]] | None:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    if not isinstance(payload, Mapping):
        return None
    for key in ("items", "listings", "results", "rows", "data"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, Mapping)]
        if isinstance(rows, Mapping):
            nested = _listing_rows(rows)
            if nested is not None:
                return nested
    # Some exports wrap the only record under a named data member.
    if _field(payload, "Ссылка на объект", "productLink", "listingUrl", "url", "link"):
        return [payload]
    return None


def drom_get_parts_search(task_id: int, uid: str) -> dict[str, Any]:
    """Read one Webbee run and normalize Baza.Drom part listings as leads."""

    if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0:
        return _failure("task_id_invalid")
    if not isinstance(uid, str) or not _UID_RE.fullmatch(uid) or j1_fetch.contains_sensitive(uid):
        return _failure("run_uid_invalid", task_id=task_id)
    config = _load_api_config()
    if config is None:
        return _failure("webbee_not_configured", task_id=task_id, uid=uid)
    token, _robot_alias = config

    try:
        status_code, status_payload = _api_request(
            "GET", f"/webbee-api/v1.0/tasks/{task_id}/status", token=token, query={"uid": uid}
        )
    except (HTTPException, OSError, TimeoutError, URLError, ValueError):
        return _failure("webbee_status_unavailable", task_id=task_id, uid=uid, outcome_uncertain=True)
    if status_code != 200:
        code = _read_error_code(status_code, stage="status")
        return _failure(code, task_id=task_id, uid=uid, outcome_uncertain=status_code >= 500 or status_code == 0)

    task_status, progress, status_error = _status_from_payload(status_payload)
    if status_error:
        return {
            **_failure(status_error, task_id=task_id, uid=uid),
            "provider_status": task_status,
            "progress": progress,
        }
    if task_status in {"queued", "running"}:
        return {
            "ok": True,
            "source": "drom",
            "status": task_status,
            "task_id": task_id,
            "uid": uid,
            "listings": [],
            "count": 0,
            "progress": progress,
            "fitment_confirmed": False,
            "availability_confirmed": False,
        }
    if task_status == "failed":
        return {
            **_failure("webbee_task_failed", task_id=task_id, uid=uid),
            "progress": progress,
            "listings": [],
            "count": 0,
        }

    try:
        result_code, result_payload = _api_request("GET", f"/webbee-api/v1.0/tasks/{task_id}/result/json", token=token)
    except (HTTPException, OSError, TimeoutError, URLError, ValueError):
        return {
            **_failure("webbee_result_unavailable", task_id=task_id, uid=uid, outcome_uncertain=True),
            "provider_status": "complete",
            "progress": progress,
        }
    if result_code != 200:
        return {
            **_failure(
                _read_error_code(result_code, stage="result"),
                task_id=task_id,
                uid=uid,
                outcome_uncertain=result_code >= 500,
            ),
            "provider_status": "complete",
            "progress": progress,
        }
    rows = _listing_rows(result_payload)
    if rows is None:
        return {
            **_failure("webbee_result_invalid", task_id=task_id, uid=uid),
            "provider_status": "complete",
            "progress": progress,
        }
    observed_at = _utc_now()
    listings = []
    skipped = 0
    duplicates = 0
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    for row in rows[:MAX_LISTINGS]:
        listing = _normalise_listing(row, observed_at=observed_at)
        if listing is None:
            skipped += 1
            continue
        listing_id = listing["listing_id"]
        listing_url = listing["url"]
        if listing_url in seen_urls or (listing_id and listing_id in seen_ids):
            duplicates += 1
            continue
        seen_urls.add(listing_url)
        if listing_id:
            seen_ids.add(listing_id)
        listings.append(listing)
    response: dict[str, Any] = {
        "ok": True,
        "source": "drom",
        "status": "complete",
        "task_id": task_id,
        "uid": uid,
        "listings": listings,
        "count": len(listings),
        "observed_at": observed_at,
        "progress": progress,
        "fitment_confirmed": False,
        "availability_confirmed": False,
    }
    if skipped:
        response["skipped_incomplete_listing_count"] = skipped
    if duplicates:
        response["duplicate_listing_count"] = duplicates
    if len(rows) > MAX_LISTINGS:
        response["truncated"] = True
    return response
