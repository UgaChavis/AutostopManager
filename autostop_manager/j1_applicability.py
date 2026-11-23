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
_ABSENT_DTC = re.compile(
    r"\b(?:without|без|при\s+отсутствии)\s+(?:the\s+)?(?:\b(?:DTC|код\w*)\b|(?=[PBCU][0-3A-F][0-9A-F]{3}\b))",
    re.I,
)
_EXCEPTION_TO_EXCLUSION = re.compile(r"[,;]\s*(?:not|but\s+not|а\s+не)\b", re.I)
_CONDITIONAL_SCOPE = re.compile(
    r"\b(?:may|might|could)\s+apply|\b(?:subject\s+to|only\s+(?:if|when)|provided\s+that|"
    r"может\s+применяться|при\s+условии|только\s+при)\b",
    re.I,
)
_EXCLUSIVE_SCOPE = re.compile(r"\b(?:only|solely|exclusively|только|исключительно)\b", re.I)
_CAMPAIGN = re.compile(r"\b(?:recall|campaign|отзывн\w*\s+кампани\w*)\b", re.I)
_CAMPAIGN_SUBJECT = re.compile(
    r"^(?:(?:this|the)\s+)?(?:(?:service|safety)\s+)?(?:recall|campaign|отзывн\w*\s+кампани\w*)"
    r"(?:\s+[A-Z0-9-]*[0-9][A-Z0-9-]*)?\s+(?:does|is|excludes|не|исключает)\b",
    re.I,
)
_MODEL_YEAR = re.compile(
    r"\b(?:model\s+years?|my|модельн\w*\s+год\w*)\s*[: ]\s*(?P<year>[0-9]{4})\b"
    r"(?:\s*(?:[-‐‑‒–—]|to\b|through\b|по\b)\s*(?P<last_year>[0-9]{4})\b)?"
    r"|\b(?P<leading_year>[0-9]{4})\s+model\s+years?\b",
    re.I,
)
_CALENDAR_YEAR = re.compile(
    r"\b(?:calendar\s+(?:production\s+)?year|production\s+year|published|copyright|"
    r"(?:built|produced|manufactured)\s+(?:in|on)|(?:календарн\w*\s+год\w*|год\s+выпуска))\b",
    re.I,
)
_DATE_OP = r"on\s+or\s+before|on\s+or\s+after|before|after|between|on|до|после|с"
_DATE_CUE = re.compile(
    rf"\b(?:built|produced|manufactured|(?:production|build|manufacture)\s+date|date\s+of\s+manufacture|"
    rf"выпущен\w*|произвед[её]н\w*|дат\w*\s+(?:выпуска|производства)|выпуска)"
    rf"\s*:?\s+(?P<op>{_DATE_OP})\b",
    re.I,
)
_DATE_VALUE = r"(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{2}\.[0-9]{2}\.[0-9]{4})"
_EXPLICIT_DATE = re.compile(
    rf"""(?P<op>{_DATE_OP})\s+["']?(?P<first>{_DATE_VALUE})["']?"""
    rf"""(?:\s+and\s+["']?(?P<last>{_DATE_VALUE})["']?)?(?!\w)""",
    re.I,
)
_ENGINE_CODE = re.compile(
    r"\b(?:engines?|двигател\w*)(?:\s+(?:codes?|код\w*))?\s*(?::|=|\s)\s*"
    r"(?P<code>[A-Z0-9.\-‐‑‒–—]*[0-9][A-Z0-9.\-‐‑‒–—]*)",
    re.I,
)
_MODEL_CONSTRAINT = re.compile(r"(?i:\b(?:models?|модел\w*)\s*(?::\s*|\s+))(?!(?i:years?|год\w*)\b)[A-ZА-ЯЁ0-9][\w-]*")
_MARKET_LABEL = r"Europe(?:an)?|North America(?:n)?|United States|US(?:A)?|Canad(?:a|ian)|Japan(?:ese)?|Европ\w*|США|Канад\w*|Япони\w*"
_MARKET_NAME = re.compile(rf"\b(?:{_MARKET_LABEL})\b", re.I)
_MARKET_HEADING = re.compile(rf"(?:(?:market|рынок)\s*[:=]?\s+)?(?:{_MARKET_LABEL})(?:\s+(?:market|рынок))?", re.I)
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
_DTC_MENTION = re.compile(r"(?<!\w)[PBCU][0-3A-F][0-9A-F]{3}(?:\s*[-‐‑‒–—−]\s*[0-9A-F]{2,})?(?!\w)", re.I)
_BARE_DTC_HEADING = re.compile(
    r"(?:DTC|code|код)\s*[:#]?\s*[PBCU][0-3A-F][0-9A-F]{3}(?:\s*[-‐‑–]\s*[0-9A-F]{2})?", re.I
)
_PART_MENTION = re.compile(
    r"\b(?:part\s+(?:number|no\.?)|(?:OE|OEM)\s+(?:number|no\.?)|артикул\w*|номер\s+детали)"
    r"\s*[:#]?\s*(?:[A-Z]{1,8}[ \t‐‑‒–—-]+)?[A-Z0-9-]*[0-9]",
    re.I,
)
_POSITION = (
    (re.compile(r"\b(?:front|передн\w*)\b", re.I), re.compile(r"\b(?:rear|задн\w*)\b", re.I)),
    (re.compile(r"\b(?:left|лев\w*)\b", re.I), re.compile(r"\b(?:right|прав[а-яё]*)\b", re.I)),
)
_BARE_SCOPE_HEADING = re.compile(
    r"^(?:model\s+years?|[0-9]{4}\s+model\s+years?|my\b|engines?\b|"
    r"модельн\w*\s+год\w*|(?:код\s+)?двигател\w*\b)",
    re.I,
)
_LOCAL_EXCLUSION = re.compile(r"\b(?:this|the)\s+(?:section|table|row)\b|\b(?:этом|данном)\s+разделе\b", re.I)
_DOCUMENT_EXCLUSION = re.compile(r"\bthis\s+(?:bulletin|document|manual)\b|\b(?:весь|всём)\s+бюллетен\w*\b", re.I)
_COHORT_JOIN = re.compile(r";|\b(?:and|or|respectively|whereas|и|или|либо|соответственно)\b", re.I)
_COHORT_PAIRING = re.compile(r"\b(?:respectively|соответственно)\b", re.I)
CLAUSE_BREAK = re.compile(r";|\b(?:whereas|while|в\s+то\s+время\s+как)\b", re.I)
_BARE_YEAR = re.compile(r"(?<!\w)(?:19|20)[0-9]{2}(?!\w|[-/]\d)")
_MAX_SCOPE_QUOTE = 400

Subject = tuple[str, int]
Matchers = Mapping[str, tuple[j1_identifiers.IdentifierPattern, ...]]


class Passage(NamedTuple):
    text: str
    page: int | None
    source_url: str
    is_heading: bool = False


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
    summary_passages: frozenset[int] | None = None


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
    years: set[str] = set()
    for match in _MODEL_YEAR.finditer(text):
        first = match["year"] or match["leading_year"]
        last = match["last_year"]
        years.add(first)
        if last and 1886 <= int(first) <= int(last) <= 2100:
            years.update(str(value) for value in range(int(first), int(last) + 1))
    return years


def _source_date(value: str) -> date:
    if "." in value:
        day, month, year = value.split(".")
        return date(int(year), int(month), int(day))
    return date.fromisoformat(value)


def _date_applies(text: str, value: str | None) -> bool | None:
    cue = _DATE_CUE.search(text)
    if cue is None:
        return True
    operands = _EXPLICIT_DATE.match(text, cue.start("op"))
    if not value or operands is None or re.search(_DATE_VALUE, text[operands.end() :]):
        return None
    try:
        target, first = date.fromisoformat(value), _source_date(operands["first"])
        last = _source_date(operands["last"]) if operands["last"] else None
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
    if match := _ABSENT_DTC.search(text):
        return text[: match.start()], text[match.end() :], None
    return None


def _positions(text: str) -> tuple[frozenset[int], ...]:
    return tuple(frozenset(index for index, pattern in enumerate(axis) if pattern.search(text)) for axis in _POSITION)


def _position_applies(text: str, target: str) -> bool | None:
    for found, wanted in zip(_positions(text), _positions(target), strict=True):
        if found and wanted:
            if not found & wanted:
                return False
            if found != wanted:
                return None
    return True


def _joins_different_cohorts(text: str, years: set[str]) -> bool:
    different_claims = CLAUSE_BREAK.search(text)
    if not _COHORT_JOIN.search(text) and not different_claims:
        return False
    if _COHORT_PAIRING.search(text):
        return True
    selections = (
        years or set(_BARE_YEAR.findall(text)),
        {match["code"].rstrip(".").casefold() for match in _ENGINE_CODE.finditer(text)},
        {market.casefold() for market in _MARKET_NAME.findall(text)},
        {code.casefold() for code in _DTC_MENTION.findall(text)},
        {part.casefold() for part in _PART_MENTION.findall(text)},
    )
    # Multiple different values in different fields can describe distinct
    # variants. A literal match in one variant does not satisfy another.
    return sum(len(values) > 1 for values in selections) >= 2 or (
        different_claims is not None and any(len(values) > 1 for values in selections[-2:])
    )


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
            ("part_number", _PART_MENTION),
        )
    ):
        return False
    if "market" in context and "market" not in fields and _MARKET_HEADING.fullmatch(text):
        return False
    if _joins_different_cohorts(text, years):
        return None
    if (position := context.get("position")) and (position_applies := _position_applies(text, position)) is not True:
        return position_applies
    if _EXCLUSIVE_SCOPE.search(text) and (
        ("year" not in context and years)
        or ("engine" not in context and _ENGINE_CODE.search(text))
        or ("market" not in context and _MARKET_CONSTRAINT.search(text))
        or ("position" not in context and any(_positions(text)))
    ):
        return None
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
    selector_heading = bool(
        _MARKET_HEADING.fullmatch(text)
        or (
            (passage.is_heading or (len(text) <= 100 and _BARE_SCOPE_HEADING.match(text)))
            and (
                _model_years(text)
                or _ENGINE_CODE.search(text)
                or _DATE_CUE.search(text)
                or (passage.is_heading and _MARKET_NAME.search(text) and _MARKET_CONSTRAINT.search(text))
                or any(_positions(text))
                or (_PART_MENTION.search(text) and "part_number" in context and "part_number" not in fields)
            )
        )
    )
    if not explicit_scope and not technical_heading and not selector_heading:
        return None
    if (negative := _negative_clause(text)) is not None:
        # A standalone exclusion can qualify an earlier scope but never starts
        # a second region for later subject evidence.
        return None if negative[0] is None else _Scope(passage, [], subjects)
    if (
        not _quote_usable(text)
        or _PRODUCT_CONTRAST.search(text)
        or _CONDITIONAL_SCOPE.search(text)
        or _selectors_apply(text, fields, context) is not True
    ):
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
        and _quote_usable(previous.passage.text)
    ):
        if (required - assessed) & previous.fields:
            evidence.append(_citation("vehicle_heading", previous.passage))
        assessed.update(previous.fields)
    years = _model_years(text)
    if "year" in required:
        if context["year"] in years:
            assessed.add("year")
        elif _CALENDAR_YEAR.search(text):
            assessed.discard("year")
    if _DATE_CUE.search(text):
        assessed.add("production_date")
    if position := context.get("position"):
        wanted = _positions(position)
        if any(wanted) and all(not need or found == need for need, found in zip(wanted, _positions(text), strict=True)):
            assessed.add("position")
    if required <= assessed:
        evidence.append(_citation("vehicle_scope", passage))
        return _Scope(passage, evidence, subjects)
    return _Scope(passage, [], subjects)


def _quote_usable(text: str) -> bool:
    return len(text) <= _MAX_SCOPE_QUOTE and "[redacted]" not in text and not contains_sensitive(text)


def _topic_evidence(
    entries: Iterable[_PassageMatch], subjects: set[Subject], context: Mapping[str, str], *, scope: _Scope, start: int
) -> tuple[list[Citation], frozenset[int]]:
    subject_evidence: dict[Subject, Passage] = {}
    technical_evidence: Passage | None = None
    eligible: set[int] = set()
    topic_selectors: dict[str, bool] = {}
    for index, entry in enumerate(entries, start):
        text = entry.passage.text
        if entry.passage.is_heading or _BARE_DTC_HEADING.fullmatch(text):
            for key, pattern in (("dtc", _DTC_MENTION), ("part_number", _PART_MENTION)):
                if key in context and pattern.search(text):
                    topic_selectors[key] = any(subject[0] == key for subject in entry.subjects)
        if (
            not all(topic_selectors.values())
            or "[redacted]" in text
            or _negative_clause(text) is not None
            or _PRODUCT_CONTRAST.search(text)
            or _SUBJECT_REFERENCE.search(text)
            or _CONDITIONAL_SCOPE.search(text)
            or _selectors_apply(text, entry.fields, context) is not True
        ):
            continue
        eligible.add(index)
        if not _quote_usable(text):
            continue
        for subject in sorted(subjects & entry.subjects):
            prior = subject_evidence.get(subject)
            if prior is None or (_SUBJECT_DESCRIPTION.search(text) and not _SUBJECT_DESCRIPTION.search(prior.text)):
                subject_evidence[subject] = entry.passage
        if technical_evidence is None and entry.fields & {"system", "symptom"}:
            technical_evidence = entry.passage

    if not subjects:
        evidence = [_citation("subject", technical_evidence)] if technical_evidence is not None else []
        return evidence, frozenset(eligible)
    if not subjects <= subject_evidence.keys():
        return [], frozenset()
    # If a scope passage names a code, detached text about other codes cannot
    # extend that scope. Do not infer a cross-reference is an applicability grant.
    anchor_types = {
        key
        for key, pattern in (("dtc", _DTC_MENTION), ("part_number", _PART_MENTION))
        if pattern.search(scope.anchor.text)
    }
    if any(subject[0] in anchor_types for subject in subjects - scope.subjects):
        return [], frozenset()
    return [_citation("subject", passage) for passage in dict.fromkeys(subject_evidence.values())], frozenset(eligible)


def _exclusion_evidence(
    entry: _PassageMatch,
    anchor: _PassageMatch | None,
    matchers: Matchers,
    context: Mapping[str, str],
    model_mentioned: bool,
) -> tuple[Literal["excluded", "unrelated", "unknown"], Citation | None]:
    passage = entry.passage
    negative = _negative_clause(passage.text)
    if negative is None or _CAMPAIGN_SUBJECT.match(passage.text):
        return "unrelated", None
    restriction = negative[1]
    qualified = bool(
        _model_years(restriction)
        or _MODEL_CONSTRAINT.search(restriction)
        or _ENGINE_CODE.search(restriction)
        or _MARKET_CONSTRAINT.search(restriction)
        or _DATE_CUE.search(restriction)
        or any(_positions(restriction))
    )
    local = _LOCAL_EXCLUSION.search(passage.text) or (not qualified and not _DOCUMENT_EXCLUSION.search(passage.text))
    local_applies = (
        _selectors_apply(anchor.passage.text, anchor.fields, context) if anchor is not None and local else True
    )
    if local_applies is False:
        return "unrelated", None
    status = _exclusion_status(*negative, matchers, context, model_mentioned)
    if status == "unrelated":
        return status, None
    if local_applies is None:
        status = "unknown"
    quote = passage.text[:_MAX_SCOPE_QUOTE].rstrip()
    if not _quote_usable(quote):
        return "unknown", None
    if status == "excluded" and quote != passage.text:
        clipped = _negative_clause(quote)
        if clipped is None or _exclusion_status(*clipped, matchers, context, model_mentioned) != "excluded":
            status = "unknown"
    kind = "exclusion" if status == "excluded" else "unresolved_exclusion"
    return status, _citation(kind, passage._replace(text=quote))


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
        entries.append(_PassageMatch(passage, matched, terms & subjects))

    scopes: list[tuple[int, _Scope]] = []
    exclusions: list[Citation] = []
    uncertain_exclusions: list[Citation] = []
    unresolved = False
    current_scope: _PassageMatch | None = None
    for index, entry in enumerate(entries):
        status, citation = _exclusion_evidence(entry, current_scope, matchers, context, "model" in fields)
        if status == "excluded" and citation is not None and len(exclusions) < 3:
            exclusions.append(citation)
        elif status == "unknown":
            unresolved = True
            if citation is not None and len(uncertain_exclusions) < 3:
                uncertain_exclusions.append(citation)
        if identity and (scope := _scope(entry, entries[index - 1] if index else None, required, context)) is not None:
            scopes.append((index, scope))
            current_scope = entry

    positive: list[Citation] = []
    summary_passages: frozenset[int] | None = None
    for number, (index, scope) in enumerate(scopes):
        if not scope.evidence:
            continue
        start = 0 if len(scopes) == 1 else index
        end = scopes[number + 1][0] if number + 1 < len(scopes) else len(entries)
        subject_evidence, eligible = _topic_evidence(entries[start:end], subjects, context, scope=scope, start=start)
        if subject_evidence:
            positive = scope.evidence + subject_evidence
            summary_passages = eligible
            break

    matched_fields = [key for key in context if key in fields]
    if exclusions and positive:
        return Applicability("conflict", matched_fields, "conflicting_source_scope", positive + exclusions)
    if exclusions:
        return Applicability("excluded", matched_fields, "explicit_source_exclusion", exclusions)
    if unresolved:
        return Applicability("analog", matched_fields, "unresolved_source_exclusion", uncertain_exclusions)
    if positive:
        return Applicability("exact", matched_fields, "cited_vehicle_and_subject_scope", positive, summary_passages)
    if fields & {"model", "engine", "part_number"}:
        return Applicability("analog", matched_fields, "scope_not_established", [])
    if fields:
        return Applicability("general", matched_fields, "context_mentions_only", [])
    return Applicability("unknown", [], "no_matching_scope_found", [])
