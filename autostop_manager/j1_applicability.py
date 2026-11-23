"""Conservative, citation-backed applicability for public research sources."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import re
from typing import NamedTuple, TypedDict

from . import j1_identifiers
from .j1_fetch import contains_sensitive

_VEHICLE_FIELDS = ("make", "model", "year", "engine", "market", "production_date", "position")
_SCOPE_STATEMENT = re.compile(
    r"\b(?:applies\s+to|applicable\s+to|covers|for\s+use\s+(?:on|with)|"
    r"model\s+years?\s+(?:19|20)[0-9]{2}\b.{0,80}\bvehicles?|"
    r"примен(?:яется|имо)\s+(?:для|к)|распространяется\s+на)\b",
    re.IGNORECASE,
)
_TECHNICAL_DESCRIPTION = re.compile(
    r"\b(?:technical\s+(?:bulletin|reference)|diagnostic\s+(?:reference|procedure)|"
    r"(?:service|workshop)\s+(?:manual|bulletin|supplement)|"
    r"(?:техническ\w*|сервисн\w*)\s+(?:бюллетен\w*|руководств\w*))\b",
    re.IGNORECASE,
)
_EXCLUSION = re.compile(
    r"\b(?:does\s+not\s+(?:apply\s+to|cover)|(?:explicitly\s+)?excludes|"
    r"not\s+applicable\s+to|не\s+применяется\s+(?:к|для)|исключает)\b",
    re.IGNORECASE,
)
_CONDITIONAL_SCOPE = re.compile(
    r"\b(?:may\s+apply|might\s+apply|could\s+apply|subject\s+to|"
    r"(?:recall|campaign)\b|может\s+применяться|отзывн\w*\s+кампани\w*)",
    re.IGNORECASE,
)
_MODEL_YEAR = re.compile(r"\b(?:model\s+years?|my|модельн\w*\s+год\w*)\s*[: ]\s*(?P<year>[0-9]{4})\b", re.I)
_CALENDAR_YEAR = re.compile(
    r"\b(?:calendar\s+(?:production\s+)?year|production\s+year|published|copyright|"
    r"(?:built|produced|manufactured)\s+(?:in|on)|(?:календарн\w*\s+год\w*|год\s+выпуска))\b",
    re.IGNORECASE,
)
_DATE_CONDITION = re.compile(
    r"\b(?:(?:built|produced|manufactured)\s+(?:before|after|between|on\s+or)|"
    r"(?:выпущен\w*|произведен\w*)\s+(?:до|после|с))\b",
    re.IGNORECASE,
)
_PRODUCT_CONTRAST = re.compile(
    r"\b(?:except|excluding|rather\s+than|unlike|excepting|not\s+for|"
    r"за\s+исключением|кроме|не\s+для)\b",
    re.IGNORECASE,
)
_SUBJECT_REFERENCE = re.compile(
    r"\b(?:see\s+also|refer\s+to\s+(?:another|separate)|"
    r"related\s+(?:codes?|bulletins?)|см\.?\s+также|связанные\s+коды)\b",
    re.IGNORECASE,
)
_SUBJECT_DESCRIPTION = re.compile(r"\b(?:means|indicates|означает|расшифровывается)\b", re.IGNORECASE)
_MAX_SCOPE_QUOTE = 400


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


def _term_pattern(value: str, *, field: str) -> re.Pattern[str]:
    if field in {"dtc", "part_number"}:
        mode: j1_identifiers.QueryMode = "dtc" if field == "dtc" else "part_number"
        code = j1_identifiers.normalize(value, mode)
        if code is not None:
            return j1_identifiers.match_pattern(code, mode)
    literal = re.escape(value)
    if field == "market" and value.casefold() in {"europe", "european"}:
        literal = "europe(?:an)?"
    if field in {"dtc", "part_number"}:
        return re.compile(rf"(?<![\w-]){literal}(?!\w|[-‐‑]\w)", re.IGNORECASE)
    if field == "year":
        return re.compile(rf"(?<!\w){literal}(?!\w|[-/]\d)", re.IGNORECASE)
    return re.compile(rf"(?<!\w){literal}(?!\w)", re.IGNORECASE)


def _citation(kind: str, passage: Passage) -> Citation:
    return {"kind": kind, "page": passage.page, "source_url": passage.source_url, "quote": passage.text}


def _excludes_target(
    text: str,
    matches: Mapping[str, tuple[re.Pattern[str], ...]],
    context: Mapping[str, str],
    target_has_model: bool,
) -> bool:
    exclusion = _EXCLUSION.search(text)
    if exclusion is None or re.search(r"\b(?:not|never)\s*$", text[: exclusion.start()], re.I):
        return False
    restriction = text[exclusion.end() :]
    model_years = {match["year"] for match in _MODEL_YEAR.finditer(restriction)}
    if ("year" in context and model_years and context["year"] not in model_years) or _DATE_CONDITION.search(
        restriction
    ):
        return False
    excluded = {key for key, patterns in matches.items() if any(pattern.search(restriction) for pattern in patterns)}
    # A bare occurrence of the make is not specific enough to be an exclusion.
    return bool(
        (excluded & {"model", "dtc", "part_number"} or target_has_model) and excluded - {"make", "system", "symptom"}
    )


def assess_applicability(context: Mapping[str, str], passages: Iterable[Passage]) -> Applicability:
    if not context:
        return Applicability("not_assessed", [], "no_automotive_context", [])
    matchers = {
        field: tuple(_term_pattern(term.strip(), field=field) for term in value.split(","))
        if field == "dtc"
        else (_term_pattern(value, field=field),)
        for field, value in context.items()
    }
    required_vehicle = set(context) & set(_VEHICLE_FIELDS)
    has_vehicle_identity = {"model", "engine"} <= required_vehicle or (
        {"make", "model"} <= required_vehicle and "part_number" in context
    )
    required_subjects = {(key, index) for key in ("dtc", "part_number") for index in range(len(matchers.get(key, ())))}

    source_fields: set[str] = set()
    subject_evidence: dict[tuple[str, int], Passage] = {}
    technical_evidence: Passage | None = None
    scope_evidence: list[Citation] = []
    exclusions: list[Passage] = []
    previous: Passage | None = None
    previous_fields: set[str] = set()
    for passage in passages:
        terms = {
            (key, index)
            for key, patterns in matchers.items()
            for index, pattern in enumerate(patterns)
            if pattern.search(passage.text)
        }
        fields = {key for key, _ in terms}
        source_fields.update(fields)
        if len(passage.text) > _MAX_SCOPE_QUOTE or (fields and contains_sensitive(passage.text)):
            previous, previous_fields = None, set()
            continue
        excluded = _excludes_target(passage.text, matchers, context, "model" in source_fields)
        if excluded and len(exclusions) < 3:
            exclusions.append(passage)
        if not _EXCLUSION.search(passage.text) and not _PRODUCT_CONTRAST.search(passage.text):
            if not _SUBJECT_REFERENCE.search(passage.text):
                for identifier in sorted(required_subjects & terms):
                    prior = subject_evidence.get(identifier)
                    if prior is None or (
                        _SUBJECT_DESCRIPTION.search(passage.text) and not _SUBJECT_DESCRIPTION.search(prior.text)
                    ):
                        subject_evidence[identifier] = passage
            if technical_evidence is None and fields & {"system", "symptom"}:
                technical_evidence = passage

            if (
                has_vehicle_identity
                and not scope_evidence
                and not _CONDITIONAL_SCOPE.search(passage.text)
                and not _DATE_CONDITION.search(passage.text)
            ):
                heading = None
                if (
                    previous is not None
                    and previous.page == passage.page
                    and ({"make", "model"} & required_vehicle) <= previous_fields
                    and not previous_fields & {"year", "engine", "dtc", "part_number"}
                    and len(previous.text) <= 120
                    and not _EXCLUSION.search(previous.text)
                    and not _PRODUCT_CONTRAST.search(previous.text)
                    and _TECHNICAL_DESCRIPTION.search(previous.text)
                ):
                    heading = previous
                assessed = fields | (previous_fields if heading is not None else set())
                model_years = {match["year"] for match in _MODEL_YEAR.finditer(passage.text)}
                if "year" in required_vehicle and (
                    (model_years and model_years != {context["year"]})
                    or (not model_years and _CALENDAR_YEAR.search(passage.text))
                ):
                    assessed.discard("year")
                if required_vehicle <= assessed and (
                    _SCOPE_STATEMENT.search(passage.text) or _TECHNICAL_DESCRIPTION.search(passage.text)
                ):
                    if heading is not None and not required_vehicle <= fields:
                        scope_evidence.append(_citation("vehicle_heading", heading))
                    scope_evidence.append(_citation("vehicle_scope", passage))
        previous, previous_fields = passage, fields

    fields_in_context_order = [key for key in context if key in source_fields]
    subject_proven = (
        required_subjects <= subject_evidence.keys() if required_subjects else technical_evidence is not None
    )
    if exclusions:
        citations = [_citation("exclusion", passage) for passage in exclusions]
        if scope_evidence and subject_proven:
            return Applicability(
                "conflict", fields_in_context_order, "conflicting_source_scope", scope_evidence + citations
            )
        return Applicability("excluded", fields_in_context_order, "explicit_source_exclusion", citations)
    if scope_evidence and subject_proven:
        evidence = list(scope_evidence)
        subjects: Iterable[Passage | None] = (
            dict.fromkeys(subject_evidence.values()) if required_subjects else [technical_evidence]
        )
        evidence += [_citation("subject", passage) for passage in subjects if passage is not None]
        return Applicability("exact", fields_in_context_order, "cited_vehicle_and_subject_scope", evidence)
    if source_fields & {"model", "engine", "part_number"}:
        return Applicability("analog", fields_in_context_order, "scope_not_established", [])
    if source_fields:
        return Applicability("general", fields_in_context_order, "context_mentions_only", [])
    return Applicability("unknown", [], "no_matching_scope_found", [])
