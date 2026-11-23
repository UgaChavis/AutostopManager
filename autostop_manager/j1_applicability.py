"""Conservative, citation-backed applicability for public research sources."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
import re
from typing import Literal, NamedTuple, TypedDict

from . import j1_identifiers
from .j1_fetch import contains_sensitive

_VEHICLE_FIELDS = {"make", "model", "year", "engine", "market", "production_date", "position"}
_SCOPE_STATEMENT = re.compile(
    r"\b(?:applies\s+to|applicable\s+to|covers|for\s+use\s+(?:on|with)|"
    r"model\s+years?\s+(?:19|20)[0-9]{2}\b.{0,80}\bvehicles?|"
    r"примен(?:яется|имо)\s+(?:для|к)|распространяется\s+на)\b",
    re.IGNORECASE,
)
_VEHICLE_SCOPE_FIELD = re.compile(r"\b(?:vehicles?|engines?|model\s+year|автомобил\w*|двигател\w*)\b", re.I)
_TECHNICAL_DESCRIPTION = re.compile(
    r"\b(?:technical\s+(?:bulletin|reference)|diagnostic\s+(?:reference|procedure)|"
    r"(?:service|workshop)\s+(?:manual|bulletin|supplement)|"
    r"(?:техническ\w*|сервисн\w*)\s+(?:бюллетен\w*|руководств\w*))\b",
    re.IGNORECASE,
)
_EXCLUSION = re.compile(
    r"\b(?:does\s+not\s+(?:apply\s+to|cover)|(?:explicitly\s+)?excludes|"
    r"do\s+not\s+(?:use|apply|install|perform)|must\s+not\s+be\s+(?:used|applied)|"
    r"not\s+(?:applicable\s+to|for)|не\s+(?:применяется\s+(?:к|для)|для|"
    r"использовать|применять)|запрещается\s+(?:использовать|применять)|исключает)\b",
    re.IGNORECASE,
)
_NEGATED_SUBJECT = re.compile(
    r"\b(?:is|are)\s+not\s+(?:covered|supported|included|addressed)|"
    r"\bне\s+(?:рассматривается|поддерживается|охватывается)\b",
    re.IGNORECASE,
)
_EXCEPTION = re.compile(r"\b(?:unless|except(?:\s+for)?|excluding|excepting|за\s+исключением|кроме|если\s+не)\b", re.I)
_EXCEPTION_TO_EXCLUSION = re.compile(r"[,;]\s*(?:not|but\s+not|а\s+не)\b", re.I)
_CONDITIONAL_SCOPE = re.compile(r"\b(?:may|might|could)\s+apply|\bsubject\s+to\b|\bможет\s+применяться\b", re.I)
_CAMPAIGN = re.compile(r"\b(?:recall|campaign|отзывн\w*\s+кампани\w*)\b", re.I)
_MODEL_YEAR = re.compile(
    r"\b(?:model\s+years?|my|модельн\w*\s+год\w*)\s*[: ]\s*(?P<year>[0-9]{4})\b"
    r"|\b(?P<leading_year>[0-9]{4})\s+model\s+years?\b",
    re.I,
)
_CALENDAR_YEAR = re.compile(
    r"\b(?:calendar\s+(?:production\s+)?year|production\s+year|published|copyright|"
    r"(?:built|produced|manufactured)\s+(?:in|on)|(?:календарн\w*\s+год\w*|год\s+выпуска))\b",
    re.I,
)
_DATE_OP = r"on\s+or\s+before|on\s+or\s+after|before|after|between|on|до|после|с"
_DATE_CUE = re.compile(rf"\b(?:built|produced|manufactured|выпущен\w*|произведен\w*)\s+(?P<op>{_DATE_OP})\b", re.I)
_ISO_DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
_EXPLICIT_DATE = re.compile(
    rf"""(?P<op>{_DATE_OP})\s+["']?(?P<first>{_ISO_DATE})["']?"""
    rf"""(?:\s+and\s+["']?(?P<last>{_ISO_DATE})["']?)?(?!\w)""",
    re.I,
)
_ENGINE_CODE = re.compile(r"\b(?:engines?|двигател\w*)\s+(?:codes?\s+)?[:=]?\s*\w*[0-9]", re.I)
_MODEL_CONSTRAINT = re.compile(r"(?i:\b(?:models?|модел\w*)\s*(?::\s*|\s+))(?!(?i:years?|год\w*)\b)[A-ZА-ЯЁ0-9][\w-]*")
_MARKET_CONSTRAINT = re.compile(
    r"\b(?:markets?|regions?|рын\w*)\b|\b(?:European|North American|US|Canadian|Japanese)\s+"
    r"(?:vehicles?|model|specification)\b",
    re.I,
)
_PRODUCT_CONTRAST = re.compile(r"\b(?:rather\s+than|unlike|not\s+for|не\s+для)\b", re.I)
_SUBJECT_REFERENCE = re.compile(
    r"\b(?:see\s+also|refer\s+to\s+(?:another|separate)|"
    r"related\s+(?:codes?|bulletins?)|см\.?\s+также|связанные\s+коды)\b",
    re.I,
)
_SUBJECT_DESCRIPTION = re.compile(r"\b(?:means|indicates|означает|расшифровывается)\b", re.I)
_DTC_MENTION = re.compile(r"(?<!\w)[PBCU][0-3A-F][0-9A-F]{3}(?:\s*[-‐‑–]\s*[0-9A-F]{2})?(?!\w)", re.I)
_MAX_SCOPE_QUOTE = 400

Subject = tuple[str, int]
Matchers = Mapping[str, tuple[j1_identifiers.IdentifierPattern, ...]]


class Passage(NamedTuple):
    text: str
    page: int | None
    source_url: str


class Citation(TypedDict):
    kind: str
    page: int | None
    source_url: str
    quote: str


class Applicability(NamedTuple):
    label: str
    fields: list[str]
    basis: str
    evidence: list[Citation]


class _PassageMatch(NamedTuple):
    passage: Passage
    fields: set[str]
    subjects: set[Subject]


class _Scope(NamedTuple):
    anchor: Passage
    evidence: list[Citation]
    subjects: set[Subject]


def _term_pattern(value: str, *, field: str) -> j1_identifiers.IdentifierPattern:
    if field in {"dtc", "part_number"}:
        mode: j1_identifiers.QueryMode = "dtc" if field == "dtc" else "part_number"
        code = j1_identifiers.normalize(value, mode)
        if code is not None:
            return j1_identifiers.match_pattern(code, mode)
    literal = re.escape(value)
    if field == "market" and value.casefold() in {"europe", "european"}:
        literal = "europe(?:an)?"
    if field in {"dtc", "part_number", "engine"}:
        return re.compile(rf"(?<!\w)(?<!\w[-‐‑‒–—−]){literal}(?!\w|[-‐‑‒–—−]\w)", re.I)
    if field == "year":
        return re.compile(rf"(?<!\w){literal}(?!\w|[-/]\d)", re.I)
    return re.compile(rf"(?<!\w){literal}(?!\w)", re.I)


def _citation(kind: str, passage: Passage) -> Citation:
    return {"kind": kind, "page": passage.page, "source_url": passage.source_url, "quote": passage.text}


def _model_years(text: str) -> set[str]:
    return {match["year"] or match["leading_year"] for match in _MODEL_YEAR.finditer(text)}


def _date_applies(text: str, value: str | None) -> bool | None:
    cue = _DATE_CUE.search(text)
    if cue is None:
        return True
    operands = _EXPLICIT_DATE.match(text, cue.start("op"))
    if not value or operands is None or re.search(_ISO_DATE, text[operands.end() :]):
        return None
    try:
        target, first = date.fromisoformat(value), date.fromisoformat(operands["first"])
        last = date.fromisoformat(operands["last"]) if operands["last"] else None
    except ValueError:
        return None
    operation = " ".join(operands["op"].casefold().split())
    if operation == "between":
        return first <= target <= last if last is not None else None
    return {
        "on": target == first,
        "before": target < first,
        "after": target > first,
        "on or before": target <= first,
        "on or after": target >= first,
        "до": target < first,
        "после": target > first,
        "с": target >= first,
    }[operation]


def _negative_clause(text: str) -> tuple[str | None, str, str | None] | None:
    if match := _NEGATED_SUBJECT.search(text):
        return None, text[: match.start()], None
    if match := _EXCLUSION.search(text):
        if re.search(r"\b(?:not|never)\s*$", text[: match.start()], re.I):
            return None
        tail = _EXCEPTION_TO_EXCLUSION.split(text[match.end() :], maxsplit=1)[0]
        if exception := _EXCEPTION.search(tail):
            return None, tail[: exception.start()], tail[exception.end() :]
        return None, tail, None
    if match := _EXCEPTION.search(text):
        return text[: match.start()], text[match.end() :], None
    return None


def _selectors_apply(text: str, fields: set[str], context: Mapping[str, str]) -> bool | None:
    years = _model_years(text)
    if "year" in context and years and context["year"] not in years:
        return False
    if any(
        key in context and key not in fields and pattern.search(text)
        for key, pattern in (
            ("model", _MODEL_CONSTRAINT),
            ("engine", _ENGINE_CODE),
            ("market", _MARKET_CONSTRAINT),
            ("dtc", _DTC_MENTION),
        )
    ):
        return False
    return _date_applies(text, context.get("production_date"))


def _exclusion_status(
    prefix: str | None,
    restriction: str,
    relief: str | None,
    matchers: Matchers,
    context: Mapping[str, str],
    model_mentioned: bool,
) -> Literal["excluded", "unrelated", "unknown"]:
    def matched_fields(text: str) -> set[str]:
        return {key for key, patterns in matchers.items() if any(pattern.search(text) for pattern in patterns)}

    fields = matched_fields(restriction)
    selectors = _selectors_apply(restriction, fields, context)
    if selectors is False or not (model_mentioned or fields & {"model", "dtc", "part_number"}):
        return "unrelated"
    if prefix is not None:
        prefix_applies = _selectors_apply(prefix, matched_fields(prefix), context)
        if prefix_applies is False:
            return "unrelated"
        if prefix_applies is None:
            return "unknown"
    if selectors is None:
        return "unknown"
    if relief is not None:
        relief_fields = matched_fields(relief)
        relief_applies = _selectors_apply(relief, relief_fields, context)
        if relief_applies is True and (relief_fields - {"make", "system", "symptom"} or _DATE_CUE.search(relief)):
            return "unrelated"
        if relief_applies is not False:
            return "unknown"
    if fields - {"make", "system", "symptom"} or _DATE_CUE.search(restriction):
        return "excluded"
    return "unknown" if prefix is not None or _VEHICLE_SCOPE_FIELD.search(restriction) else "unrelated"


def _scope(
    entry: _PassageMatch,
    previous: _PassageMatch | None,
    required: set[str],
    context: Mapping[str, str],
) -> _Scope | None:
    passage, fields, subjects = entry
    text = passage.text
    if _CAMPAIGN.search(text):
        return None
    explicit_scope = bool(
        _SCOPE_STATEMENT.search(text) and (fields & _VEHICLE_FIELDS or _VEHICLE_SCOPE_FIELD.search(text))
    )
    technical_heading = "model" in fields and bool(fields & {"engine", "year"}) and _TECHNICAL_DESCRIPTION.search(text)
    if not explicit_scope and not technical_heading:
        return None
    if (negative := _negative_clause(text)) is not None:
        # A standalone exclusion can qualify an earlier scope but never starts
        # a second region for later subject evidence.
        return None if negative[0] is None else _Scope(passage, [], subjects)
    if "[redacted]" in text or _PRODUCT_CONTRAST.search(text) or _CONDITIONAL_SCOPE.search(text):
        return _Scope(passage, [], subjects)

    evidence: list[Citation] = []
    assessed = set(fields)
    if (
        previous is not None
        and previous.passage.page == passage.page
        and ({"make", "model"} & required) <= previous.fields
        and not previous.fields & {"year", "engine", "dtc", "part_number"}
        and len(previous.passage.text) <= 120
        and _TECHNICAL_DESCRIPTION.search(previous.passage.text)
        and _negative_clause(previous.passage.text) is None
        and not _PRODUCT_CONTRAST.search(previous.passage.text)
    ):
        if (required - assessed) & previous.fields:
            evidence.append(_citation("vehicle_heading", previous.passage))
        assessed.update(previous.fields)
    years = _model_years(text)
    if "year" in required and ((years and years != {context["year"]}) or (not years and _CALENDAR_YEAR.search(text))):
        assessed.discard("year")
    if _DATE_CUE.search(text):
        if _date_applies(text, context.get("production_date")) is not True:
            return _Scope(passage, [], subjects)
        assessed.add("production_date")
    if required <= assessed:
        evidence.append(_citation("vehicle_scope", passage))
        return _Scope(passage, evidence, subjects)
    return _Scope(passage, [], subjects)


def _topic_evidence(
    entries: Iterable[_PassageMatch], subjects: set[Subject], context: Mapping[str, str], *, scope: _Scope
) -> list[Citation]:
    subject_evidence: dict[Subject, Passage] = {}
    technical_evidence: Passage | None = None
    for entry in entries:
        text = entry.passage.text
        if (
            "[redacted]" in text
            or _negative_clause(text) is not None
            or _PRODUCT_CONTRAST.search(text)
            or _SUBJECT_REFERENCE.search(text)
            or _CONDITIONAL_SCOPE.search(text)
            or _selectors_apply(text, entry.fields, context) is not True
        ):
            continue
        for subject in sorted(subjects & entry.subjects):
            prior = subject_evidence.get(subject)
            if prior is None or (_SUBJECT_DESCRIPTION.search(text) and not _SUBJECT_DESCRIPTION.search(prior.text)):
                subject_evidence[subject] = entry.passage
        if technical_evidence is None and entry.fields & {"system", "symptom"}:
            technical_evidence = entry.passage

    if not subjects:
        return [_citation("subject", technical_evidence)] if technical_evidence is not None else []
    if not subjects <= subject_evidence.keys():
        return []
    # If a scope passage names a code, detached text about other codes cannot
    # extend that scope. Do not infer a cross-reference is an applicability grant.
    anchor_has_dtc = _DTC_MENTION.search(scope.anchor.text)
    if anchor_has_dtc and any(subject[0] == "dtc" for subject in subjects - scope.subjects):
        return []
    return [_citation("subject", passage) for passage in dict.fromkeys(subject_evidence.values())]


def assess_applicability(context: Mapping[str, str], passages: Iterable[Passage]) -> Applicability:
    if not context:
        return Applicability("not_assessed", [], "no_automotive_context", [])
    matchers = {
        field: tuple(_term_pattern(term.strip(), field=field) for term in value.split(","))
        if field == "dtc"
        else (_term_pattern(value, field=field),)
        for field, value in context.items()
    }
    required = context.keys() & _VEHICLE_FIELDS
    identity = {"model", "engine"} <= required or ({"make", "model"} <= required and "part_number" in context)
    subjects = {(key, index) for key in ("dtc", "part_number") for index in range(len(matchers.get(key, ())))}
    entries: list[_PassageMatch] = []
    fields: set[str] = set()
    for passage in passages:
        terms = {
            (key, index)
            for key, patterns in matchers.items()
            for index, pattern in enumerate(patterns)
            if pattern.search(passage.text)
        }
        matched = {key for key, _ in terms}
        fields.update(matched)
        if len(passage.text) <= _MAX_SCOPE_QUOTE and (not matched or not contains_sensitive(passage.text)):
            entries.append(_PassageMatch(passage, matched, terms & subjects))

    scopes: list[tuple[int, _Scope]] = []
    exclusions: list[Citation] = []
    uncertain_exclusions: list[Citation] = []
    for index, entry in enumerate(entries):
        text = entry.passage.text
        if not _CAMPAIGN.search(text) and (negative := _negative_clause(text)) is not None:
            status = _exclusion_status(*negative, matchers, context, "model" in fields)
            if status == "excluded" and len(exclusions) < 3:
                exclusions.append(_citation("exclusion", entry.passage))
            elif status == "unknown" and len(uncertain_exclusions) < 3:
                uncertain_exclusions.append(_citation("unresolved_exclusion", entry.passage))
        if identity and (scope := _scope(entry, entries[index - 1] if index else None, required, context)) is not None:
            scopes.append((index, scope))

    positive: list[Citation] = []
    for number, (index, scope) in enumerate(scopes):
        if not scope.evidence:
            continue
        start = 0 if len(scopes) == 1 else index
        end = scopes[number + 1][0] if number + 1 < len(scopes) else len(entries)
        if subject_evidence := _topic_evidence(entries[start:end], subjects, context, scope=scope):
            positive = scope.evidence + subject_evidence
            break

    matched_fields = [key for key in context if key in fields]
    if exclusions and positive:
        return Applicability("conflict", matched_fields, "conflicting_source_scope", positive + exclusions)
    if exclusions:
        return Applicability("excluded", matched_fields, "explicit_source_exclusion", exclusions)
    if uncertain_exclusions:
        return Applicability("analog", matched_fields, "unresolved_source_exclusion", uncertain_exclusions)
    if positive:
        return Applicability("exact", matched_fields, "cited_vehicle_and_subject_scope", positive)
    if fields & {"model", "engine", "part_number"}:
        return Applicability("analog", matched_fields, "scope_not_established", [])
    if fields:
        return Applicability("general", matched_fields, "context_mentions_only", [])
    return Applicability("unknown", [], "no_matching_scope_found", [])
