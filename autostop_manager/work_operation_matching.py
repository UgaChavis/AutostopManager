"""Conservative operation identity before display-name canonicalization."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_WORD_GROUPS = {
    "замена": "замена замены замене замену заменить поменять поменял",
    "ремонт": "ремонт ремонта ремонту ремонтировать отремонтировать",
    "проверка": "проверка проверки проверку проверить проверять",
    "диагностика": "диагностика диагностики диагностику диагностировать",
    "снятие": "снятие снятия снять демонтаж демонтировать",
    "установка": "установка установки установку установить монтаж монтировать",
    "перепрессовка": "перепрессовка перепрессовки перепрессовку перепрессовать",
    "передний": "передний передняя переднее передние передних переднего переднюю передней спереди",
    "задний": "задний задняя заднее задние задних заднего заднюю задней сзади",
    "левый": "левый левая левое левые левых левого левую левой слева",
    "правый": "правый правая правое правые правых правого правую правой справа",
    "рулевой": "рулевой рулевая рулевую рулевых рулевого",
    "рейка": "рейка рейки рейку рейке",
    "опорный": "опорный опорная опорные опорных опорного",
    "подшипник": "подшипник подшипника подшипники подшипников",
    "стойка": "стойка стойки стойку стойке стоек",
    "мехатроник": "мехатроник мехатроника мехатронику",
    "пыльник": "пыльник пыльника пыльники пыльников",
    "шрус": "шрус шруса шрусы шрусов",
    "тормозной": "тормозной тормозная тормозные тормозных тормозного",
    "колодка": "колодка колодки колодок колодку",
    "диск": "диск диска диски дисков",
    "масло": "масло масла маслу",
    "фильтр": "фильтр фильтра фильтры фильтров фильтром",
    "двигатель": "двигатель двигателя двигателю",
    "насос": "насос насоса насосы насосов",
}
_WORDS = {form: canonical for canonical, forms in _WORD_GROUPS.items() for form in forms.split()}
_ACTIONS = frozenset({"замена", "ремонт", "проверка", "диагностика", "снятие", "установка", "перепрессовка"})
_QUALIFIERS = {"передний": "axle", "задний": "axle", "левый": "side", "правый": "side"}
_GEARBOX_QUALIFIERS = {
    "dsg6": "gears",
    "dsg7": "gears",
    **dict.fromkeys(("0am", "0cw", "02e", "0d9", "0gc"), "gearbox"),
}
_SIMPLE_OBJECTS = {
    ("рейка",),
    ("опорный_подшипник",),
    ("мехатроник", "dsg"),
    ("мехатроник", "кпп"),
    ("пыльник", "шрус"),
    ("тормозной", "колодка"),
    ("тормозной", "диск"),
    ("колодка",),
    ("диск",),
    ("стойка",),
    ("подшипник",),
    ("насос",),
    ("шрус",),
    ("масло",),
    ("фильтр",),
    ("двигатель",),
}


def _tokens(value: str) -> tuple[str, ...]:
    value = re.sub(r"[,/+&]", " и ", value.casefold().replace("ё", "е"))
    return tuple(_WORDS.get(word, word) for word in re.findall(r"[^\W_]+", value))


@dataclass(frozen=True)
class _Operation:
    identity: tuple[str, ...]
    core: tuple[str, ...]
    qualifiers: frozenset[tuple[str, str]]
    simple: bool


class OperationMatcher:
    """Keep scope, actions and unknown words; allow only known optional qualifiers.

    Profiles are cached for one estimate, never across requests. Matching allows
    a generic axle/side on a single known operation, so it is deliberately not
    used as equality when deduplicating observations.
    """

    def __init__(self, vehicle_context: dict[str, Any] | None = None) -> None:
        context = vehicle_context or {}
        # Engine/transmission descriptions identify work scope, not neutral padding.
        vehicle = _tokens(str(context.get("vehicle") or ""))
        make = _tokens(str(context.get("make") or ""))
        model = _tokens(str(context.get("model") or ""))
        year = _tokens(str(context.get("year") or ""))
        self._vehicles = sorted(
            {
                tokens
                for tokens in (vehicle + year, make + model + year, vehicle, make + model, make, model, year)
                if tokens and not any(token in {"акпп", "кпп", "dsg", *_GEARBOX_QUALIFIERS} for token in tokens)
            },
            key=lambda tokens: (-len(tokens), tokens),
        )
        self._profiles: dict[str, _Operation] = {}

    def _profile(self, value: str) -> _Operation:
        cached = self._profiles.get(value)
        if cached is not None:
            return cached
        tokens = _tokens(value)
        # These phrases describe a labor-only price, not an excluded operation.
        text = " ".join(tokens)
        text = re.sub(r"\bбез (?:запчастей|деталей)\b|\bтолько работа\b", "", text)
        text = re.sub(r"\bрулевой рейка\b", "рейка", text)
        text = re.sub(
            r"\bопорный подшипник(?: ((?:передний|задний|левый|правый)(?: (?:левый|правый))?))? стойка\b",
            lambda match: "опорный_подшипник" + (" " + match[1] if match[1] else ""),
            text,
        )
        text = re.sub(r"\bопорный подшипник\b", "опорный_подшипник", text)
        text = re.sub(r"\bмехатроник s tronic\b", "мехатроник dsg", text)
        text = re.sub(r"\bмехатроник dsg ?([67])\b", r"мехатроник dsg dsg\1", text)
        text = re.sub(r"\bмехатроник (?=(?:0am|0cw|02e|0d9|0gc)\b)", "мехатроник dsg ", text)
        tokens = tuple(text.split())
        # A known car name may qualify a work item only at its boundaries.
        for vehicle in self._vehicles:
            for prefix in (("на",), ("для",), ()):
                suffix = prefix + vehicle
                if len(tokens) > len(suffix) and tokens[-len(suffix) :] == suffix:
                    tokens = tokens[: -len(suffix)]
            if len(tokens) > len(vehicle) and tokens[: len(vehicle)] == vehicle:
                tokens = tokens[len(vehicle) :]
        qualifier_groups = dict(_QUALIFIERS)
        if "мехатроник" in tokens and "dsg" in tokens:
            qualifier_groups.update(_GEARBOX_QUALIFIERS)
        qualifiers = frozenset((qualifier_groups[token], token) for token in tokens if token in qualifier_groups)
        core = tuple(token for token in tokens if token not in qualifier_groups)
        simple = (
            len(core) > 1
            and core[0] in _ACTIONS
            and core[1:] in _SIMPLE_OBJECTS
            and all(sum(group == axis for group, _ in qualifiers) <= 1 for axis in set(qualifier_groups.values()))
        )
        profile = _Operation(tokens, core, qualifiers, simple)
        self._profiles[value] = profile
        return profile

    def identity(self, value: str) -> str:
        """Strict equality key retaining explicit qualifiers and complete scope."""
        profile = self._profile(value)
        return repr((profile.core, tuple(sorted(profile.qualifiers))) if profile.simple else profile.identity)

    def matches(self, requested: str, observed: str) -> bool:
        left, right = self._profile(requested), self._profile(observed)
        if not left.identity or not right.identity:
            return False
        if left.identity == right.identity:
            return True
        if not left.simple or not right.simple or left.core != right.core:
            return False
        left_axes, right_axes = dict(left.qualifiers), dict(right.qualifiers)
        return all(left_axes[axis] == right_axes[axis] for axis in left_axes.keys() & right_axes.keys())

    def matches_known_qualification(self, requested: object, observed: object) -> bool:
        """Extend exact-name consumers only for a strictly more qualified simple work."""
        if not isinstance(requested, str) or not isinstance(observed, str):
            return False
        left, right = self._profile(requested), self._profile(observed)
        return (
            left.simple
            and right.simple
            and left.core == right.core
            and (left.qualifiers < right.qualifiers or right.qualifiers < left.qualifiers)
        )
