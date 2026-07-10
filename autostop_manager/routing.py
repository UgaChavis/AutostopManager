from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence


MIN_ROUTE_MARGIN = 8


@dataclass(frozen=True)
class QuerySemantics:
    """Small, deterministic intent model used before lexical knowledge search."""

    intents: frozenset[str]
    objects: frozenset[str]
    actions: frozenset[str]
    sources: frozenset[str]
    outputs: frozenset[str]
    access_mode: str
    risk_level: str
    broad_project_request: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "intents": sorted(self.intents),
            "objects": sorted(self.objects),
            "actions": sorted(self.actions),
            "sources": sorted(self.sources),
            "outputs": sorted(self.outputs),
            "access_mode": self.access_mode,
            "risk_level": self.risk_level,
            "broad_project_request": self.broad_project_request,
        }


@dataclass(frozen=True)
class DomainAssessment:
    domain: str
    score: int
    applicable: bool
    evidence: tuple[str, ...]
    negative_evidence: tuple[str, ...]


_SEMANTIC_PATTERNS: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    "objects": {
        "project": (
            "autostop manager",
            "autostopmanager",
            "проект",
            "репозитор",
            "кодовая база",
            "архитектур",
            "техническ debt",
            "техдолг",
        ),
        "router": ("маршрутиз", "routing", "router", "route selection"),
        "mcp": ("mcp", "model context protocol", "инструмент"),
        "documentation": (
            "документац",
            "playbook",
            "readme",
            "runbook",
            "инструкц",
            "индексац",
            "аннотац",
            "база знаний",
            "качество знаний",
        ),
        "memory": ("sqlite", "памят", "memory", "миграц", "retention", "журнал операций"),
        "server": ("сервер", "server", "контейнер", "container", "docker", "systemd", "runtime"),
        "github": ("github", "pull request", " pr ", "репозиторий github", "ветк", "коммит"),
        "crm": ("crm", "карточк", "заказ-наряд", "касс", "клиент", "доск"),
        "board": ("доск", "board", "входящие карточки", "активных карточ"),
        "gmail": ("gmail", "почт", "письм", "email", "ярлык", "черновик", "вложен"),
        "vehicle_identity": (" vin", "vin ", "vin-", "номер кузова", "номер рамы", "frame", "chassis", "wmi"),
        "parts": (
            "запчаст",
            "детал",
            "oem",
            "каталожн",
            "аналог",
            "кросс",
            "свеч",
            "колод",
            "фильтр",
            "рейк",
            "поставщик",
            "оригинальный номер",
            "заменитель",
        ),
        "automotive_repair": ("ремонт", "диагност", "dtc", "ошибк", "wiring", "tsb", "recall"),
        "transmission": ("dsg", "dq200", "dq250", "dq381", "короб", "акпп", "трансмис", "мехатрон"),
        "fluids": ("масло", "жидк", "fluid", "oil", "заправочн"),
        "ecu": ("ecu", "эбу", "a2l", "odx", "j2534", "kombi", "приборк", "прошив"),
        "bmw": ("bmw", "бмв", "ista", "xdrive", "n63", "f15", "x5"),
        "gr_yaris": ("gr yaris", "yaris gr", "gxpa16", "g16e", "gr-four", "ярис"),
        "business_document": ("счёт", "счет", "акт", "коммерческ", "docx", "invoice", "тендер"),
        "business_identity": ("реквизит", "карточка предприятия", "егрип", "инн", "огрнип"),
        "cad": ("3d", "3д", "stl", "cad", "openscad", "anycubic", "kobra"),
        "labor_pricing": ("стоимость работ", "цена работ", "нормочас", "трудоемк", "labor pricing"),
        "remote_workstation": ("home-pc", "домашн", "reverse ssh", "удаленн", "remote workstation"),
    },
    "actions": {
        "orient": ("изучи", "ознаком", "почитай", "подготовь", "как устро", "обзор"),
        "audit": ("аудит", "обслед", "проверь", "инвентар", "диагностик", "ревью", "review"),
        "refactor": ("рефактор", "перестрой", "архитектурн", "устрани дубли", "техдолг"),
        "fix": ("исправ", "почин", "устрани", "доведи", "приведи", "улучш"),
        "test": ("тест", "coverage", "покрыт", "линт", "ruff", "mypy", "проверк качества"),
        "document": ("документир", "обнови документац", "почисти документац", "инструкц"),
        "deploy": ("разверн", "deploy", "деплой", "перезапуст", "обнови сервер"),
        "publish": (
            "push",
            "опублику",
            "pull request",
            "отправь изменен",
            "отправь в github",
            "отправь на github",
        ),
        "read": ("прочитай", "покажи", "найди", "получи", "проверь", "список", "отчёт", "отчет"),
        "write": ("запиши", "измени", "обнови", "создай", "добавь", "сохрани", "примени", "установи"),
        "delete": ("удали", "архивируй", "очисти", "убери"),
        "route": ("маршрутиз", "route", "направ", "playbook"),
        "search": ("найди", "поиск", "подбери", "lookup", "сверь"),
        "decode": ("расшифр", "decode", "определи машину", "что за машин", "какая машина"),
        "price": ("оцени", "посчитай", "цена", "стоимость", "смет"),
        "triage": ("разбери входящие", "triage", "готовые без оплаты", "просрочен"),
        "resume": ("продолж", "повторн", "после останов", "восстанов"),
    },
    "sources": {
        "local_repo": ("репозитор", "код", "локальн", "readme", "docs/", "pyproject"),
        "crm": ("crm", "карточк", "заказ-наряд", "касс", "доск"),
        "gmail": ("gmail", "почт", "email", "письм"),
        "server": ("сервер", "server", "docker", "контейнер", "runtime", "systemd"),
        "web": ("интернет", "web", "браузер", "сайт", "форум", "публичн"),
        "catalog": ("каталог", "epc", "oem", "drom", "zzap", "авито", "поставщик"),
    },
    "outputs": {
        "report": ("отчёт", "отчет", "результат", "вывод", "заключение"),
        "code_change": ("исправ", "рефактор", "реализ", "внеси измен", "код"),
        "deployment": ("разверн", "deploy", "деплой", "серверн commit"),
        "pull_request": ("pull request", " pr ", "github", "ветк", "коммит"),
        "list": ("список", "перечень", "инвентар"),
        "document": ("документ", "pdf", "docx", "счёт", "счет", "акт"),
        "price": ("цена", "стоимость", "смет"),
        "identity": ("расшифр", "идентичност", "модель по vin"),
    },
}


_DOMAIN_FEATURES: Mapping[str, Mapping[str, frozenset[str]]] = {
    "project_maintenance": {
        "objects": frozenset({"project", "router", "mcp", "documentation", "memory", "server", "github"}),
        "actions": frozenset({"orient", "audit", "refactor", "fix", "test", "document", "deploy", "publish", "resume"}),
        "sources": frozenset({"local_repo", "server"}),
        "outputs": frozenset({"report", "code_change", "deployment", "pull_request", "list"}),
    },
    "startup_and_identity": {
        "objects": frozenset({"project", "documentation", "memory", "router"}),
        "actions": frozenset({"orient", "read", "audit", "resume"}),
        "sources": frozenset({"local_repo"}),
        "outputs": frozenset({"report"}),
    },
    "knowledge_intake": {
        "objects": frozenset({"documentation"}),
        "actions": frozenset({"document", "write", "delete", "fix", "audit", "test"}),
        "sources": frozenset({"local_repo"}),
        "outputs": frozenset({"code_change", "list"}),
    },
    "deployment": {
        "objects": frozenset({"server", "github", "project"}),
        "actions": frozenset({"deploy", "publish", "audit", "read"}),
        "sources": frozenset({"server", "local_repo"}),
        "outputs": frozenset({"deployment", "pull_request"}),
    },
    "service_management": {
        "objects": frozenset({"crm", "board"}),
        "actions": frozenset({"audit", "read", "write", "triage"}),
        "sources": frozenset({"crm"}),
        "outputs": frozenset({"report", "list"}),
    },
    "board_cleanup_autopilot": {
        "objects": frozenset({"crm", "board"}),
        "actions": frozenset({"triage", "write", "delete"}),
        "sources": frozenset({"crm"}),
        "outputs": frozenset({"code_change"}),
    },
    "crm_card_description_standard": {
        "objects": frozenset({"crm"}),
        "actions": frozenset({"write", "document"}),
        "sources": frozenset({"crm"}),
        "outputs": frozenset({"code_change"}),
    },
    "gmail_operations": {
        "objects": frozenset({"gmail"}),
        "actions": frozenset({"read", "search", "write", "delete", "triage"}),
        "sources": frozenset({"gmail"}),
        "outputs": frozenset({"report", "list"}),
    },
    "vehicle_identity_and_oem": {
        "objects": frozenset({"vehicle_identity", "parts"}),
        "actions": frozenset({"decode", "search", "read"}),
        "sources": frozenset({"catalog", "crm", "web"}),
        "outputs": frozenset({"identity", "list"}),
    },
    "crm_vin_oem_parts_lookup": {
        "objects": frozenset({"crm", "vehicle_identity", "parts"}),
        "actions": frozenset({"decode", "search", "write", "price"}),
        "sources": frozenset({"crm", "catalog", "web"}),
        "outputs": frozenset({"list", "price", "code_change"}),
    },
    "parts_sourcing": {
        "objects": frozenset({"parts"}),
        "actions": frozenset({"search", "price", "read"}),
        "sources": frozenset({"catalog", "web", "crm"}),
        "outputs": frozenset({"list", "price"}),
    },
    "automotive_repair": {
        "objects": frozenset({"automotive_repair"}),
        "actions": frozenset({"search", "read", "audit"}),
        "sources": frozenset({"web", "local_repo"}),
        "outputs": frozenset({"report", "list"}),
    },
    "transmission": {
        "objects": frozenset({"transmission", "automotive_repair"}),
        "actions": frozenset({"search", "read", "audit"}),
        "sources": frozenset({"web", "local_repo"}),
        "outputs": frozenset({"report", "list"}),
    },
    "fluids": {
        "objects": frozenset({"fluids"}),
        "actions": frozenset({"search", "read"}),
        "sources": frozenset({"web", "local_repo", "crm"}),
        "outputs": frozenset({"report", "list"}),
    },
    "ecu_calibration_programming": {
        "objects": frozenset({"ecu"}),
        "actions": frozenset({"search", "read", "audit"}),
        "sources": frozenset({"web", "local_repo"}),
        "outputs": frozenset({"report", "list"}),
    },
    "bmw_repair": {
        "objects": frozenset({"bmw", "automotive_repair", "transmission", "fluids"}),
        "actions": frozenset({"search", "read", "audit"}),
        "sources": frozenset({"web", "local_repo"}),
        "outputs": frozenset({"report", "list"}),
    },
    "bmw_f15_n63": {
        "objects": frozenset({"bmw", "automotive_repair"}),
        "actions": frozenset({"search", "read", "audit"}),
        "sources": frozenset({"web", "local_repo"}),
        "outputs": frozenset({"report", "list"}),
    },
    "toyota_gr_yaris": {
        "objects": frozenset({"gr_yaris", "automotive_repair", "parts", "transmission", "fluids"}),
        "actions": frozenset({"search", "read", "audit"}),
        "sources": frozenset({"web", "local_repo", "catalog"}),
        "outputs": frozenset({"report", "list"}),
    },
    "business_documents": {
        "objects": frozenset({"business_document"}),
        "actions": frozenset({"write", "document", "read"}),
        "sources": frozenset({"crm", "local_repo"}),
        "outputs": frozenset({"document"}),
    },
    "business_identity": {
        "objects": frozenset({"business_identity"}),
        "actions": frozenset({"read", "write"}),
        "sources": frozenset({"local_repo"}),
        "outputs": frozenset({"report", "document"}),
    },
    "3d_printing_cad": {
        "objects": frozenset({"cad"}),
        "actions": frozenset({"write", "document"}),
        "sources": frozenset({"local_repo"}),
        "outputs": frozenset({"code_change"}),
    },
    "work_labor_pricing": {
        "objects": frozenset({"labor_pricing", "automotive_repair"}),
        "actions": frozenset({"price", "search", "read"}),
        "sources": frozenset({"web", "crm"}),
        "outputs": frozenset({"price", "report"}),
    },
    "remote_codex_access": {
        "objects": frozenset({"remote_workstation", "server"}),
        "actions": frozenset({"read", "write", "audit"}),
        "sources": frozenset({"server"}),
        "outputs": frozenset({"report"}),
    },
}


_NARROW_REQUIRED_OBJECTS: Mapping[str, frozenset[str]] = {
    "automotive_repair": frozenset({"automotive_repair"}),
    "gmail_operations": frozenset({"gmail"}),
    "crm_vin_oem_parts_lookup": frozenset({"crm", "vehicle_identity", "parts"}),
    "parts_sourcing": frozenset({"parts"}),
    "vehicle_identity_and_oem": frozenset({"vehicle_identity"}),
    "fluids": frozenset({"fluids"}),
    "transmission": frozenset({"transmission"}),
    "ecu_calibration_programming": frozenset({"ecu"}),
    "bmw_repair": frozenset({"bmw"}),
    "bmw_f15_n63": frozenset({"bmw"}),
    "toyota_gr_yaris": frozenset({"gr_yaris"}),
    "business_documents": frozenset({"business_document"}),
    "business_identity": frozenset({"business_identity"}),
    "3d_printing_cad": frozenset({"cad"}),
    "work_labor_pricing": frozenset({"labor_pricing"}),
    "remote_codex_access": frozenset({"remote_workstation"}),
}


def classify_query(query: str) -> QuerySemantics:
    lowered = _normalized_text(query)
    objects = _extract(lowered, _SEMANTIC_PATTERNS["objects"])
    actions = _extract(lowered, _SEMANTIC_PATTERNS["actions"])
    sources = _extract(lowered, _SEMANTIC_PATTERNS["sources"])
    outputs = _extract(lowered, _SEMANTIC_PATTERNS["outputs"])
    if re.search(r"(?<![A-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Z0-9])", (query or "").upper()):
        objects.add("vehicle_identity")
    if "fault memory" in lowered and objects & {"bmw", "automotive_repair"}:
        objects.discard("memory")
    if "без карточки crm" in lowered or "without a crm card" in lowered:
        objects.discard("crm")
    if any(term in lowered for term in ("без записи", "только чтение", "read only", "read-only")):
        actions.difference_update({"write", "delete", "deploy", "publish"})

    intents: set[str] = set()
    if "project" in objects and actions & {"orient", "audit"}:
        intents.add("project_overview")
    if "project" in objects and actions & {"refactor", "fix", "test", "document", "deploy", "publish"}:
        intents.add("project_maintenance")
    if "documentation" in objects and actions & {"document", "write", "delete", "fix", "audit", "test"}:
        intents.add("documentation_hygiene")
        intents.add("knowledge_intake")
    if "vehicle_identity" in objects and "decode" in actions:
        intents.add("vehicle_identity_decode")
    if {"crm", "vehicle_identity", "parts"}.issubset(objects):
        intents.add("crm_vin_oem_parts_lookup")
    if "parts" in objects and actions & {"search", "price"}:
        intents.add("parts_sourcing")
    if "gmail" in objects:
        intents.add("gmail_operations")
    if objects & {"crm", "board"} and actions & {"triage", "audit"}:
        intents.add("service_management")
    if "server" in objects and actions & {"audit", "read", "deploy"}:
        intents.add("server_operations")
    if "memory" in objects and "project" not in objects and "documentation" not in objects:
        intents.add("memory_operations")
    if "orient" in actions and objects & {"project", "documentation", "router", "memory"}:
        intents.add("startup")

    mutating = bool(actions & {"write", "delete", "deploy", "publish", "refactor", "fix"})
    reading = bool(actions & {"read", "orient", "audit", "search", "decode", "price", "test"})
    access_mode = "mixed" if mutating and reading else "write" if mutating else "read"
    high_risk = bool(actions & {"delete", "deploy", "publish"}) or (
        "write" in actions and bool(objects & {"crm", "gmail", "server"})
    )
    risk_level = "high" if high_risk else "medium" if mutating else "low"

    broad_actions = actions & {"audit", "refactor", "fix", "test", "document", "deploy", "publish"}
    broad_components = objects & {"router", "mcp", "documentation", "memory", "server", "github"}
    broad_project_request = "project" in objects and (
        "project_overview" in intents or len(broad_actions | broad_components) >= 2
    )

    return QuerySemantics(
        intents=frozenset(intents),
        objects=frozenset(objects),
        actions=frozenset(actions),
        sources=frozenset(sources),
        outputs=frozenset(outputs),
        access_mode=access_mode,
        risk_level=risk_level,
        broad_project_request=broad_project_request,
    )


def assess_domain(domain: str, semantics: QuerySemantics) -> DomainAssessment:
    profile = _DOMAIN_FEATURES.get(domain)
    if profile is None:
        return DomainAssessment(domain=domain, score=0, applicable=True, evidence=(), negative_evidence=())

    evidence: list[str] = []
    score = 0
    for dimension, weight in (("objects", 18), ("actions", 10), ("sources", 7), ("outputs", 5)):
        observed = getattr(semantics, dimension)
        matches = observed & profile.get(dimension, frozenset())
        if matches:
            score += min(weight * len(matches), weight * 3)
            evidence.extend(f"{dimension}:{item}" for item in sorted(matches))

    if domain in semantics.intents:
        score += 34
        evidence.append(f"intent:{domain}")
    if domain == "project_maintenance" and semantics.intents & {"project_overview", "project_maintenance"}:
        score += 42
        evidence.append("intent:broad_project")
    if domain == "startup_and_identity" and "startup" in semantics.intents:
        score += 30
        evidence.append("intent:startup")
    if domain == "startup_and_identity" and "memory_operations" in semantics.intents:
        score += 24
        evidence.append("intent:memory_operations")
    if domain == "deployment" and "server_operations" in semantics.intents:
        score += 28
        evidence.append("intent:server_operations")

    negative: list[str] = []
    required = _NARROW_REQUIRED_OBJECTS.get(domain)
    applicable = not required or required.issubset(semantics.objects)
    if not applicable:
        missing = required - semantics.objects if required else frozenset()
        negative.extend(f"missing_object:{item}" for item in sorted(missing))
        score -= 45

    if semantics.broad_project_request and domain not in {"project_maintenance", "startup_and_identity"}:
        explicit_object = bool(required and required.issubset(semantics.objects))
        if not explicit_object:
            score -= 70
            applicable = False
            negative.append("broad_project_without_narrow_target")
        else:
            score -= 20
            negative.append("broad_project_precedence")

    if domain == "deployment" and semantics.broad_project_request:
        score -= 55
        applicable = False
        negative.append("deployment_is_only_one_phase")
    if domain == "knowledge_intake" and semantics.broad_project_request:
        score -= 45
        applicable = False
        negative.append("documentation_is_only_one_phase")
    if domain == "vehicle_identity_and_oem" and "crm_vin_oem_parts_lookup" in semantics.intents:
        score -= 25
        negative.append("crm_writeback_route_more_specific")
    if domain == "parts_sourcing" and "crm_vin_oem_parts_lookup" in semantics.intents:
        score -= 20
        negative.append("crm_vin_route_more_specific")
    if domain == "crm_card_description_standard" and not semantics.actions & {"write", "document"}:
        score -= 55
        applicable = False
        negative.append("no_card_description_write_intent")
    if domain == "business_documents" and "business_document" in semantics.objects:
        score += 24
        evidence.append("intent:business_document")
    if domain == "crm_card_description_standard" and "business_document" in semantics.objects:
        score -= 45
        applicable = False
        negative.append("business_document_not_card_description")

    return DomainAssessment(
        domain=domain,
        score=score,
        applicable=applicable,
        evidence=tuple(dict.fromkeys(evidence)),
        negative_evidence=tuple(dict.fromkeys(negative)),
    )


def route_confidence(
    *,
    score: int,
    margin: int,
    evidence_count: int,
    exact_command: bool = False,
    fallback: bool = False,
) -> float:
    if exact_command:
        return 0.99
    if fallback or score <= 0:
        return 0.2 if fallback else 0.0
    if evidence_count < 2:
        return round(min(0.4, 0.18 + max(score, 0) / 300), 2)
    confidence = 0.42 + min(evidence_count, 5) * 0.07 + min(max(score, 0), 120) / 600
    if margin < MIN_ROUTE_MARGIN:
        confidence -= (MIN_ROUTE_MARGIN - max(margin, 0)) * 0.025
    return round(max(0.0, min(0.96, confidence)), 2)


def specific_term_count(terms: Sequence[str]) -> int:
    generic = {
        "crm",
        "manager",
        "server",
        "сервис",
        "работа",
        "данные",
        "проект",
        "код",
        "документация",
        "проверить",
        "обновить",
    }
    return sum(1 for term in terms if term.casefold().strip() not in generic and len(term.strip()) >= 3)


def phrase_present(text: str, phrase: str) -> bool:
    normalized_phrase = _normalized_text(phrase)
    if not normalized_phrase:
        return False
    return normalized_phrase in text


def _extract(text: str, patterns: Mapping[str, Iterable[str]]) -> set[str]:
    result: set[str] = set()
    padded = f" {text} "
    for label, variants in patterns.items():
        if any(_pattern_present(padded, variant) for variant in variants):
            result.add(label)
    return result


def _pattern_present(text: str, variant: str) -> bool:
    normalized = _normalized_text(variant)
    if not normalized:
        return False
    if len(normalized) <= 3 and normalized.isalnum():
        return re.search(rf"(?<!\w){re.escape(normalized)}(?!\w)", text) is not None
    return normalized in text


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").casefold().replace("ё", "е")).strip()
