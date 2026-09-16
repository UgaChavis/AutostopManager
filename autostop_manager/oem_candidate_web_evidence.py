"""Bounded public-web corroboration for already-found OEM candidates.

This module deliberately has no VIN input.  It can only corroborate a part
number with public search snippets and legal source routes; it never turns a
snippet into VIN-specific fitment confirmation.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from functools import lru_cache
import re
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from .source_catalog import load_source_catalog, recommend_automotive_sources
from .vin_lookup import normalize_part_number
from .web_research_gateway import redact_vin_like_text, research_part_public_evidence
from .work_pricing_research import PUBLIC_RESEARCH_TIMEOUT_SECONDS


_MAX_CANDIDATES = 3
_MAX_RESULTS_PER_CANDIDATE = 3
_MAX_TIMEOUT_SECONDS = 6
_SAFE_PART_NUMBER = re.compile(r"^[A-Z0-9][A-Z0-9 .\-/]{2,47}$")
_FITMENT_SCOPES = {"vin_specific", "vin_specific_position_unconfirmed", "not_vin_specific"}
_POSITION_MATCHES = {"matched", "not_required", "conflict", "ambiguous", "not_proved"}
_CONFIDENCE_LABELS = {"high", "medium", "low", "blocked"}
_APPLICABILITY_STATUSES = {"catalog_evidence_found", "not_checked", "check_failed", "not_found", "rejected"}
_NONPUBLIC_ACCESS_TOKENS = ("login", "registration", "subscription", "paid", "mixed")
_E8_AUTHORIZED_PART_SOURCES: dict[tuple[str, str], tuple[str, ...]] = {
    ("partsouq_catalog", "oem_catalog"): ("partsouq.com",),
    ("amayama_catalog", "oem_catalog"): ("amayama.com",),
    ("emex_public", "price_catalog"): ("emex.ru",),
    ("exist", "price_catalog"): ("exist.ru",),
}
_SAFE_DOMAIN = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.I)


def _bounded(value: int, *, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return minimum


def _bounded_timeout(value: float) -> int:
    try:
        return max(1, min(int(float(value)), _MAX_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        return PUBLIC_RESEARCH_TIMEOUT_SECONDS


def _compact(value: Any, *, limit: int = 128) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _known_value(value: Any, allowed: set[str]) -> str:
    text = str(value or "").strip()
    return text if text in allowed else ""


def _strip_vin_like_text(value: Any, *, limit: int = 128) -> tuple[str, int]:
    sanitized, removed = redact_vin_like_text(value, limit=limit)
    return sanitized.upper(), removed


def _looks_like_full_vin(value: str) -> bool:
    compact = re.sub(r"[\s._/\\-]+", "", str(value or "").upper())
    return bool(re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", compact))


def _url_contains_vin_like_token(parsed: Any) -> bool:
    """Detect a complete VIN in URL path/query fragments without matching words."""

    decoded = unquote(f"{parsed.path}?{parsed.query}#{parsed.fragment}")
    for fragment in re.split(r"[?&#=]", decoded):
        pieces = [piece for piece in fragment.split("/") if piece]
        for start in range(len(pieces)):
            compact = ""
            for piece in pieces[start : start + 4]:
                compact += re.sub(r"[\s._\\-]+", "", piece).upper()
                if _looks_like_full_vin(compact):
                    return True
                if len(compact) > 17:
                    break
    return any(_looks_like_full_vin(label) for label in str(parsed.hostname or "").split("."))


def _safe_https_url(value: Any, *, _allow_ddg_unwrap: bool = True) -> tuple[str | None, bool]:
    raw = str(value or "").strip()
    if not raw:
        return None, False
    try:
        parsed = urlparse(raw)
        if not parsed.scheme and raw.startswith("//"):
            parsed = urlparse("https:" + raw)
        if (
            _allow_ddg_unwrap
            and parsed.hostname
            and parsed.hostname.casefold() in {"duckduckgo.com", "www.duckduckgo.com"}
        ):
            wrapped_urls = parse_qs(parsed.query).get("uddg", [])
            if parsed.path.startswith("/l/") and len(wrapped_urls) == 1:
                return _safe_https_url(wrapped_urls[0], _allow_ddg_unwrap=False)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return None, False
        if _url_contains_vin_like_token(parsed):
            return None, True
    except ValueError:
        return None, False
    return raw, False


def _safe_catalog_evidence(value: Any) -> tuple[list[dict[str, str]], int]:
    """Keep only explicit, linkable VIN-specific catalog provenance.

    A caller's confidence labels are insufficient to make a candidate
    confirmed.  This evidence is deliberately separate from public snippets
    and must carry both a source label and a safe HTTPS link.
    """

    rows = value if isinstance(value, list) else [value]
    accepted: list[dict[str, str]] = []
    removed = 0
    for row in rows[:_MAX_RESULTS_PER_CANDIDATE]:
        if not isinstance(row, Mapping):
            continue
        source, source_removed = _strip_vin_like_text(row.get("source"), limit=100)
        url, url_removed = _safe_https_url(row.get("url"))
        scope = _compact(row.get("evidence_scope") or row.get("scope"), limit=80).casefold()
        removed += source_removed + int(url_removed)
        if not source or not url or scope != "vin_specific_catalog":
            continue
        accepted.append(
            {
                "source": source,
                "url": url,
                "evidence_scope": "vin_specific_catalog",
            }
        )
    return accepted, removed


def _safe_candidate(item: Any, index: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None, int]:
    if not isinstance(item, Mapping):
        return None, {"candidate_index": index, "code": "candidate_not_object"}, 0
    raw_number = _compact(item.get("part_number"), limit=48)
    number, removed = _strip_vin_like_text(raw_number, limit=48)
    if removed or _looks_like_full_vin(raw_number):
        return None, {"candidate_index": index, "code": "vin_like_part_number_rejected"}, max(1, removed)
    if not _SAFE_PART_NUMBER.fullmatch(number):
        return None, {"candidate_index": index, "code": "invalid_part_number"}, removed
    normalized = normalize_part_number(number)
    if len(normalized) < 3:
        return None, {"candidate_index": index, "code": "invalid_part_number"}, removed

    brand, brand_removed = _strip_vin_like_text(item.get("brand"), limit=80)
    name, name_removed = _strip_vin_like_text(item.get("name"), limit=160)
    catalog_sources, evidence_removed = _safe_catalog_evidence(item.get("catalog_evidence"))
    return (
        {
            "candidate_index": index,
            "part_number": number,
            "normalized_part_number": normalized,
            "brand": brand,
            "name": name,
            "fitment_scope": _known_value(item.get("fitment_scope"), _FITMENT_SCOPES),
            "position_match": _known_value(item.get("position_match"), _POSITION_MATCHES),
            "confidence_label": _known_value(item.get("confidence_label"), _CONFIDENCE_LABELS),
            "applicability_status": _known_value(item.get("applicability_status"), _APPLICABILITY_STATUSES),
            "catalog_sources": catalog_sources,
            "manual_review_required": bool(item.get("manual_review_required")),
        },
        None,
        removed + brand_removed + name_removed + evidence_removed,
    )


def _safe_context(
    *,
    requested_part: str | None,
    make: str | None,
    model: str | None,
    model_year: int | str | None,
    engine: str | None,
    axle: str | None,
    side: str | None,
    position: str | None,
) -> tuple[dict[str, Any], int]:
    requested, requested_removed = _strip_vin_like_text(requested_part, limit=160)
    safe_make, make_removed = _strip_vin_like_text(make, limit=80)
    safe_model, model_removed = _strip_vin_like_text(model, limit=100)
    safe_engine, engine_removed = _strip_vin_like_text(engine, limit=100)
    safe_axle, axle_removed = _strip_vin_like_text(axle, limit=40)
    safe_side, side_removed = _strip_vin_like_text(side, limit=40)
    safe_position, position_removed = _strip_vin_like_text(position, limit=80)
    try:
        year = int(model_year) if model_year not in (None, "") else None
    except (TypeError, ValueError):
        year = None
    if year is not None and not 1886 <= year <= datetime.now(UTC).year + 1:
        year = None
    return (
        {
            "requested_part": requested or None,
            "make": safe_make or None,
            "model": safe_model or None,
            "model_year": year,
            "engine": safe_engine or None,
            "axle": safe_axle or None,
            "side": safe_side or None,
            "position": safe_position or None,
        },
        requested_removed
        + make_removed
        + model_removed
        + engine_removed
        + axle_removed
        + side_removed
        + position_removed,
    )


def _search_query(candidate: Mapping[str, Any], context: Mapping[str, Any]) -> str:
    return " ".join(
        value
        for value in (
            str(candidate.get("brand") or "").strip(),
            str(candidate.get("part_number") or "").strip(),
            str(context.get("requested_part") or "").strip(),
            str(context.get("make") or "").strip(),
            str(context.get("model") or "").strip(),
            str(context.get("model_year") or "").strip(),
            str(context.get("engine") or "").strip(),
            str(context.get("axle") or "").strip(),
            str(context.get("side") or "").strip(),
            str(context.get("position") or "").strip(),
        )
        if value
    )


def _search_url(query: str) -> str:
    return "https://html.duckduckgo.com/html/?q=" + quote_plus(query)


def _candidate_conflicts(candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    common = {"candidate_index": candidate["candidate_index"], "part_number": candidate["part_number"]}
    if candidate.get("position_match") == "conflict":
        conflicts.append(
            {
                **common,
                "code": "candidate_position_conflict",
                "severity": "high",
                "message": "Позиция кандидата противоречит запрошенной детали.",
            }
        )
    if candidate.get("fitment_scope") and candidate.get("fitment_scope") != "vin_specific":
        conflicts.append(
            {
                **common,
                "code": "fitment_not_vin_specific",
                "severity": "warning",
                "message": "Кандидат не подтверждён VIN-специфичным источником.",
            }
        )
    if candidate.get("confidence_label") == "low":
        conflicts.append(
            {
                **common,
                "code": "low_catalog_confidence",
                "severity": "warning",
                "message": "Исходный каталог оценил кандидата с низкой уверенностью.",
            }
        )
    if candidate.get("applicability_status") == "rejected":
        conflicts.append(
            {
                **common,
                "code": "catalog_applicability_rejected",
                "severity": "high",
                "message": "Исходная каталожная проверка отклонила применимость кандидата.",
            }
        )
    elif candidate.get("applicability_status") in {"not_found", "check_failed"}:
        conflicts.append(
            {
                **common,
                "code": "catalog_applicability_not_confirmed",
                "severity": "warning",
                "message": "Исходная каталожная проверка не подтвердила применимость кандидата.",
            }
        )
    return conflicts


def _registry_routes(make: str | None) -> tuple[list[dict[str, Any]], list[str]]:
    routes: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for data_type in ("vehicle_data", "parts_catalog"):
        recommendation = recommend_automotive_sources(
            brand=make or None,
            data_type=data_type,
            include_licensed=False,
            limit=5,
        )
        warnings.extend(str(item) for item in recommendation.get("warnings") or [] if item)
        for source in recommendation.get("sources") or []:
            if not isinstance(source, Mapping):
                continue
            source_id = str(source.get("source_id") or "").strip()
            access = _compact(source.get("access"), limit=80)
            if (
                not source_id
                or source_id in seen
                or bool(source.get("requires_license"))
                or any(token in access.casefold() for token in _NONPUBLIC_ACCESS_TOKENS)
                or (make and not bool(source.get("brand_match")))
            ):
                continue
            url, _ = _safe_https_url(source.get("url"))
            if not url:
                continue
            seen.add(source_id)
            routes.append(
                {
                    "source_id": source_id,
                    "name": _compact(source.get("name"), limit=160),
                    "source_type": _compact(source.get("category"), limit=100),
                    "url": url,
                    "access": access,
                    "license_status": _compact(source.get("legal_ingestion_status"), limit=120),
                    "requires_license": bool(source.get("requires_license")),
                    "status": "route_only",
                }
            )
    return routes, list(dict.fromkeys(warnings))


@lru_cache(maxsize=1)
def _public_source_allowlist() -> tuple[dict[str, str], ...]:
    """Return local-registry public/free hosts allowed for DDG snippets."""

    allowed: list[dict[str, str]] = []
    for source in load_source_catalog().get("sources") or []:
        if not isinstance(source, Mapping) or bool(source.get("forum_or_unofficial")):
            continue
        access = _compact(source.get("access"), limit=80)
        if (
            not access
            or not any(token in access.casefold() for token in ("free", "public", "open"))
            or any(token in access.casefold() for token in _NONPUBLIC_ACCESS_TOKENS)
        ):
            continue
        url, _ = _safe_https_url(source.get("url"))
        if not url:
            continue
        hostname = str(urlparse(url).hostname or "").casefold()
        source_id = _compact(source.get("id") or source.get("source_id"), limit=100)
        source_name = _compact(source.get("name"), limit=160)
        source_type = _compact(source.get("category"), limit=100)
        if hostname and source_id and source_name:
            allowed.append(
                {
                    "host": hostname,
                    "source_id": source_id,
                    "source": source_name,
                    "source_type": source_type or "public_reference",
                }
            )
    return tuple(allowed)


def _allowlisted_source_for_url(url: str) -> dict[str, str] | None:
    hostname = str(urlparse(url).hostname or "").casefold()
    for source in _public_source_allowlist():
        allowed_host = source["host"]
        if hostname == allowed_host or hostname.endswith("." + allowed_host):
            return source
    return None


def _candidate_status(
    *, candidate: Mapping[str, Any], search_status: str, contradictions: list[dict[str, Any]]
) -> tuple[str, str]:
    has_high_conflict = any(item.get("severity") == "high" for item in contradictions)
    if (
        has_high_conflict
        or candidate.get("applicability_status") in {"rejected", "not_found", "check_failed"}
        or search_status in {"public_reference_not_found", "search_failed"}
    ):
        return "не подтверждён", "Публичный поиск не дал достаточного основания для подтверждения; требуется EPC."
    if candidate.get("catalog_sources"):
        return (
            "кандидат",
            "Переданная каталожная ссылка показана для ручной сверки; E7 не принимает её как доказательство применимости.",
        )
    if candidate.get("applicability_status") == "catalog_evidence_found":
        return (
            "кандидат",
            "Есть заявленный каталожный результат, но E7 не может подтвердить его без доверенной EPC-проверки.",
        )
    return "кандидат", "Публичные ссылки могут поддержать поиск, но не подтверждают применимость."


def _candidate_next_manual_step(status: str, contradictions: list[dict[str, Any]], live_search: bool) -> dict[str, Any]:
    if contradictions:
        return {
            "code": "resolve_candidate_conflicts",
            "message": "Устранить противоречия позиции и применимости в VIN-специфичном EPC.",
        }
    if not live_search:
        return {
            "code": "run_optional_public_search_or_epc",
            "message": "При необходимости включить ограниченный публичный поиск или перейти сразу к VIN-специфичному EPC.",
        }
    return {
        "code": "confirm_in_vin_specific_epc",
        "message": "Подтвердить номер, замену, количество и условия применимости в VIN-специфичном EPC.",
    }


def _registered_domain(value: str, domains: tuple[str, ...]) -> bool:
    return any(value == domain or value.endswith("." + domain) for domain in domains)


def _gateway_authorized_source(row: Mapping[str, Any], url: str) -> dict[str, str] | None:
    """Accept E8 metadata only when it intersects the local static registry."""

    if row.get("source_authorized") is not True:
        return None
    source_id, source_id_removed = _strip_vin_like_text(row.get("source_id"), limit=80)
    source_type, source_type_removed = _strip_vin_like_text(row.get("source_type"), limit=40)
    source_id = source_id.casefold()
    source_type = source_type.casefold()
    declared_domain, domain_removed = _strip_vin_like_text(row.get("domain"), limit=253)
    if source_id_removed or source_type_removed or domain_removed:
        return None
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,79}", source_id):
        return None
    registered_domains = _E8_AUTHORIZED_PART_SOURCES.get((source_id, source_type))
    if registered_domains is None:
        return None
    hostname = str(urlparse(url).hostname or "").casefold().rstrip(".")
    declared_domain = declared_domain.casefold().rstrip(".")
    if not hostname or not _SAFE_DOMAIN.fullmatch(declared_domain):
        return None
    if not _registered_domain(hostname, registered_domains) or not _registered_domain(
        declared_domain, registered_domains
    ):
        return None
    if (
        hostname != declared_domain
        and not hostname.endswith("." + declared_domain)
        and not declared_domain.endswith("." + hostname)
    ):
        return None
    return {
        "source": hostname,
        "source_id": source_id,
        "source_type": source_type,
    }


def _safe_search_result(row: Any, *, candidate: Mapping[str, Any]) -> tuple[dict[str, Any] | None, int]:
    if not isinstance(row, Mapping):
        return None, 0
    url, url_removed = _safe_https_url(row.get("url"))
    title, title_removed = _strip_vin_like_text(row.get("title"), limit=140)
    snippet, snippet_removed = _strip_vin_like_text(row.get("snippet"), limit=240)
    source, source_removed = _strip_vin_like_text(row.get("source"), limit=100)
    removed = int(url_removed) + title_removed + snippet_removed + source_removed
    if not url:
        return None, removed
    allowed_source = _gateway_authorized_source(row, url) or _allowlisted_source_for_url(url)
    if allowed_source is None:
        return None, removed
    reference_text = normalize_part_number(" ".join((title, snippet, url)))
    return (
        {
            "source": allowed_source["source"],
            "source_id": allowed_source["source_id"],
            "source_type": allowed_source["source_type"],
            "url": url,
            "title": title,
            "snippet": snippet,
            "search_result_source": source or urlparse(url).hostname or "public_web_search",
            "part_number_reference_found": str(candidate["normalized_part_number"]) in reference_text,
            "evidence_scope": "public_search_snippet",
        },
        removed,
    )


def _manual_actions(
    *,
    candidate_evidence: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    live_search: bool,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if not live_search:
        actions.append(
            {
                "code": "run_optional_public_search",
                "priority": 2,
                "message": "При необходимости включить ограниченный публичный поиск по номеру детали без VIN.",
            }
        )
    if any(item.get("evidence_status") == "public_reference_found" for item in candidate_evidence):
        actions.append(
            {
                "code": "confirm_in_vin_specific_epc",
                "priority": 1,
                "message": "Сверить номер, замену, количество и условия применимости в VIN-специфичном EPC.",
            }
        )
    if any(item.get("evidence_status") == "public_reference_not_found" for item in candidate_evidence):
        actions.append(
            {
                "code": "manual_epc_fallback",
                "priority": 1,
                "message": "Отсутствие ссылки в публичном поиске не означает отсутствия детали; проверить брендовый EPC.",
            }
        )
    if any(item.get("evidence_status") == "search_failed" for item in candidate_evidence):
        actions.append(
            {
                "code": "retry_or_use_registry_route",
                "priority": 2,
                "message": "Повторять только при временной ошибке; затем использовать легальный маршрут из реестра источников.",
            }
        )
    if conflicts:
        actions.append(
            {
                "code": "resolve_candidate_conflicts",
                "priority": 1,
                "message": "Устранить конфликты позиции и применимости до проценки или заказа.",
            }
        )
    if not actions:
        actions.append(
            {
                "code": "confirm_in_vin_specific_epc",
                "priority": 1,
                "message": "Подтвердить OEM-кандидат в VIN-специфичном EPC перед использованием.",
            }
        )
    return actions


def verify_oem_candidates_web(
    *,
    candidates: list[dict[str, Any]] | None = None,
    requested_part: str | None = None,
    make: str | None = None,
    model: str | None = None,
    model_year: int | str | None = None,
    engine: str | None = None,
    axle: str | None = None,
    side: str | None = None,
    position: str | None = None,
    live_search: bool = False,
    max_candidates: int = _MAX_CANDIDATES,
    max_results_per_candidate: int = _MAX_RESULTS_PER_CANDIDATE,
    timeout: float = PUBLIC_RESEARCH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Corroborate OEM-number candidates through safe public-web snippets.

    ``candidates`` is expected to be the already-redacted output of the OEM
    resolver.  There is intentionally no VIN parameter: full VIN values are
    removed from every queryable field and rejected when supplied as a part
    number. ``live_search`` is opt-in, bounded and does not write anything.
    """

    limit = _bounded(max_candidates, minimum=1, maximum=_MAX_CANDIDATES)
    result_limit = _bounded(max_results_per_candidate, minimum=1, maximum=_MAX_RESULTS_PER_CANDIDATE)
    timeout_seconds = _bounded_timeout(timeout)
    context, redaction_count = _safe_context(
        requested_part=requested_part,
        make=make,
        model=model,
        model_year=model_year,
        engine=engine,
        axle=axle,
        side=side,
        position=position,
    )
    raw_candidates = candidates if isinstance(candidates, list) else []
    prepared: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, raw_candidate in enumerate(raw_candidates[:limit], start=1):
        candidate, rejection, removed = _safe_candidate(raw_candidate, index)
        redaction_count += removed
        if candidate is not None:
            prepared.append(candidate)
        elif rejection is not None:
            rejected.append(rejection)

    routes, route_warnings = _registry_routes(context.get("make"))
    if context.get("make") and not routes:
        route_warnings.append(
            "Для марки нет подходящего публичного бесплатного маршрута; VIN-специфичный EPC проверяется вручную по лицензии."
        )
    if not prepared:
        return {
            "ok": False,
            "schema": "OemCandidatePublicEvidenceV1",
            "mode": "read_only_public_web_verification",
            "status": "needs_valid_oem_candidate",
            "input_context": context,
            "source_routes": routes,
            "candidate_evidence": [],
            "conflicts": rejected,
            "manual_actions": [
                {
                    "code": "provide_oem_candidate",
                    "priority": 1,
                    "message": "Передать корректный OEM-номер из каталожного кандидата; VIN не принимается этим инструментом.",
                }
            ],
            "warnings": list(dict.fromkeys(route_warnings)),
            "privacy": {
                "vin_input_supported": False,
                "full_vin_in_search_queries": False,
                "vin_like_input_removed_or_rejected_count": redaction_count,
                "external_calls_by_default": False,
                "writes_crm": False,
            },
        }

    conflicts: list[dict[str, Any]] = [*rejected]
    candidate_conflicts: dict[int, list[dict[str, Any]]] = {}
    by_number: dict[str, set[str]] = {}
    for candidate in prepared:
        candidate_index = int(candidate["candidate_index"])
        own_conflicts = _candidate_conflicts(candidate)
        candidate_conflicts[candidate_index] = own_conflicts
        conflicts.extend(own_conflicts)
        by_number.setdefault(candidate["normalized_part_number"], set()).add(candidate["brand"] or "unknown")
    for normalized, brands in by_number.items():
        if len(brands) <= 1:
            continue
        affected = [item for item in prepared if item["normalized_part_number"] == normalized]
        conflict = {
            "code": "same_number_multiple_brands",
            "severity": "warning",
            "normalized_part_number": normalized,
            "candidate_indices": [item["candidate_index"] for item in affected],
            "message": "Один номер получен с несколькими брендами; требуется ручная сверка бренда.",
        }
        conflicts.append(conflict)
        for candidate in affected:
            candidate_conflicts[int(candidate["candidate_index"])].append(
                {**conflict, "candidate_index": candidate["candidate_index"], "part_number": candidate["part_number"]}
            )

    candidate_evidence: list[dict[str, Any]] = []
    for candidate in prepared:
        query = _search_query(candidate, context)
        query, query_removed = _strip_vin_like_text(query, limit=360)
        redaction_count += query_removed
        query_url = _search_url(query)
        item: dict[str, Any] = {
            "candidate_index": candidate["candidate_index"],
            "oem_number": candidate["part_number"],
            "part_number": candidate["part_number"],
            "brand": candidate["brand"] or None,
            "name": candidate["name"] or None,
            "query": query,
            "search_url": query_url,
            "evidence_status": "not_checked",
            "evidence": [],
            "sources": list(candidate["catalog_sources"]),
            "applicability_conditions": {
                "axle": context["axle"],
                "side": context["side"],
                "position": context["position"],
                "catalog_fitment_scope": candidate["fitment_scope"] or None,
                "catalog_position_match": candidate["position_match"] or None,
                "catalog_applicability_status": candidate["applicability_status"] or None,
                "catalog_provenance_available": bool(candidate["catalog_sources"]),
            },
            "contradictions": candidate_conflicts[int(candidate["candidate_index"])],
            "fitment_confirmed": False,
            "warnings": ["Публичный поиск не подтверждает VIN-специфичную применимость."],
        }
        if live_search:
            gateway = research_part_public_evidence(
                query=query,
                limit=result_limit,
                max_pages=1,
                timeout_seconds=timeout_seconds,
            )
            item["research_gateway"] = {
                "schema": _compact(gateway.get("schema"), limit=80),
                "capability": _compact(gateway.get("capability"), limit=80),
                "adapter": _compact(gateway.get("adapter"), limit=80),
                "provider_order": [
                    _compact(provider, limit=40)
                    for provider in gateway.get("provider_order") or []
                    if _compact(provider, limit=40)
                ][:5],
                "fallback_used": bool(gateway.get("fallback_used")),
            }
            if not gateway.get("ok"):
                item["evidence_status"] = "search_failed"
                item["warnings"].append("Публичный поиск временно недоступен; это не означает отсутствия детали.")
            else:
                safe_results: list[dict[str, Any]] = []
                for row in (gateway.get("results") or [])[:result_limit]:
                    safe_row, removed = _safe_search_result(row, candidate=candidate)
                    redaction_count += removed
                    if safe_row is not None:
                        safe_results.append(safe_row)
                item["evidence"] = safe_results
                item["sources"].extend(
                    {
                        "source": row["source"],
                        "source_id": row["source_id"],
                        "source_type": row["source_type"],
                        "url": row["url"],
                    }
                    for row in safe_results
                )
                item["evidence_status"] = (
                    "public_reference_found"
                    if any(row["part_number_reference_found"] for row in safe_results)
                    else "public_reference_not_found"
                )
        candidate_status, status_reason = _candidate_status(
            candidate=candidate,
            search_status=str(item["evidence_status"]),
            contradictions=item["contradictions"],
        )
        item["status"] = candidate_status
        item["status_reason"] = status_reason
        item["fitment_confirmed"] = False
        item["next_manual_step"] = _candidate_next_manual_step(
            candidate_status,
            item["contradictions"],
            live_search,
        )
        candidate_evidence.append(item)

    if conflicts:
        status = "conflicts_need_manual_review"
    elif live_search and any(item["evidence_status"] == "public_reference_found" for item in candidate_evidence):
        status = "public_reference_found_needs_epc_confirmation"
    elif live_search and all(item["evidence_status"] == "search_failed" for item in candidate_evidence):
        status = "public_search_provider_failed"
    elif live_search:
        status = "public_reference_not_found_needs_epc"
    else:
        status = "prepared_no_network"
    return {
        "ok": not (live_search and all(item["evidence_status"] == "search_failed" for item in candidate_evidence)),
        "schema": "OemCandidatePublicEvidenceV1",
        "mode": "read_only_public_web_verification",
        "status": status,
        "input_context": context,
        "source_routes": routes,
        "candidate_evidence": candidate_evidence,
        "conflicts": conflicts,
        "manual_actions": _manual_actions(
            candidate_evidence=candidate_evidence,
            conflicts=conflicts,
            live_search=live_search,
        ),
        "warnings": list(dict.fromkeys(route_warnings)),
        "rules": [
            "Публичные поисковые сниппеты — слабое справочное свидетельство, а не подтверждение применимости.",
            "E7 не выдаёт статус «подтверждён»: переданные ссылки и веб-сниппеты остаются кандидатами до доверенной EPC-проверки применимости.",
            "Перед проценкой, заказом или записью в CRM подтвердить номер, замену, количество и условия в VIN-специфичном EPC.",
            "Инструмент не создаёт заказов, не обращается к CRM и не принимает VIN.",
        ],
        "privacy": {
            "vin_input_supported": False,
            "full_vin_in_search_queries": False,
            "vin_like_input_removed_or_rejected_count": redaction_count,
            "external_calls_by_default": False,
            "live_search_requested": bool(live_search),
            "writes_crm": False,
            "creates_orders": False,
        },
    }
