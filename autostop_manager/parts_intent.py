from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any


@dataclass(frozen=True)
class PartIntentRule:
    intent_id: str
    canonical_name_ru: str
    canonical_name_en: str
    patterns: tuple[str, ...]
    catalog_groups_ru: tuple[str, ...]
    catalog_groups_en: tuple[str, ...]
    positions: tuple[str, ...]
    critical_vehicle_fields: tuple[str, ...]
    quantity_basis: str
    partsapi_cat_candidates: tuple[str, ...] = ()
    confidence: float = 0.7
    clarification_fields: tuple[str, ...] = ()

    def matches(self, text: str) -> bool:
        return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in self.patterns)


PART_INTENT_RULES: tuple[PartIntentRule, ...] = (
    PartIntentRule(
        intent_id="brake_pads_unspecified_axle",
        canonical_name_ru="тормозные колодки",
        canonical_name_en="brake pads",
        patterns=(
            r"^(?!.*\b(?:передн|задн)\w*).*\bтормозн\w*\s+колод",
            r"^(?!.*\b(?:передн|задн)\w*).*\bколод\w*(?:\s+тормозн\w*)?",
            r"^(?!.*\b(?:front|rear)\b).*\bbrake\s+pads?\b",
        ),
        catalog_groups_ru=("тормозная система", "колодки тормозные"),
        catalog_groups_en=("brake system", "brake pads"),
        positions=("front_or_rear_required",),
        critical_vehicle_fields=(
            "axle",
            "market",
            "production_date",
            "grade/options",
            "brake_system",
            "wheel_size",
            "engine",
            "drivetrain",
        ),
        quantity_basis="axle_set_after_axle_confirmation",
        partsapi_cat_candidates=("brake pads", "front brake pads", "rear brake pads", "колодки тормозные"),
        confidence=0.74,
        clarification_fields=("axle",),
    ),
    PartIntentRule(
        intent_id="front_brake_pads",
        canonical_name_ru="передние тормозные колодки",
        canonical_name_en="front brake pads",
        patterns=(
            r"\bпередн\w*(?:\s+тормозн\w*)?\s+колод",
            r"\bколод\w*(?:\s+тормозн\w*)?\s+перед",
            r"front\s+brake\s+pads?",
        ),
        catalog_groups_ru=("тормозная система", "колодки тормозные", "передние тормоза"),
        catalog_groups_en=("brake system", "front brake pads", "disc brake front"),
        positions=("front_axle",),
        critical_vehicle_fields=(
            "market",
            "production_date",
            "grade/options",
            "brake_system",
            "wheel_size",
            "engine",
            "drivetrain",
        ),
        quantity_basis="axle_set",
        partsapi_cat_candidates=("brake pads", "front brake pads", "колодки тормозные передние"),
        confidence=0.9,
    ),
    PartIntentRule(
        intent_id="rear_brake_pads",
        canonical_name_ru="задние тормозные колодки",
        canonical_name_en="rear brake pads",
        patterns=(
            r"\bзадн\w*(?:\s+тормозн\w*)?\s+колод",
            r"\bколод\w*(?:\s+тормозн\w*)?\s+зад",
            r"rear\s+brake\s+pads?",
        ),
        catalog_groups_ru=("тормозная система", "колодки тормозные", "задние тормоза"),
        catalog_groups_en=("brake system", "rear brake pads", "disc/drum brake rear"),
        positions=("rear_axle",),
        critical_vehicle_fields=(
            "market",
            "production_date",
            "grade/options",
            "brake_system",
            "parking_brake_type",
            "drivetrain",
        ),
        quantity_basis="axle_set",
        partsapi_cat_candidates=("rear brake pads", "brake shoes", "колодки тормозные задние"),
        confidence=0.88,
    ),
    PartIntentRule(
        intent_id="brake_disc",
        canonical_name_ru="тормозной диск",
        canonical_name_en="brake disc",
        patterns=(r"\bдиск\w*\s+торм", r"\bтормозн\w*\s+диск", r"brake\s+disc", r"brake\s+rotor"),
        catalog_groups_ru=("тормозная система", "тормозные диски"),
        catalog_groups_en=("brake system", "brake disc", "brake rotor"),
        positions=("front_axle_or_rear_axle_required",),
        critical_vehicle_fields=(
            "axle",
            "market",
            "production_date",
            "brake_system",
            "diameter",
            "vented/solid",
            "wheel_size",
        ),
        quantity_basis="piece_or_pair_must_be_explicit",
        partsapi_cat_candidates=("brake disc", "brake rotor", "диск тормозной"),
        confidence=0.86,
    ),
    PartIntentRule(
        intent_id="stabilizer_link",
        canonical_name_ru="стойка стабилизатора",
        canonical_name_en="stabilizer link",
        patterns=(r"\bлинк", r"стойк\w*\s+стабил", r"stabilizer\s+link", r"sway\s+bar\s+link"),
        catalog_groups_ru=("подвеска", "стабилизатор поперечной устойчивости", "стойка стабилизатора"),
        catalog_groups_en=("suspension", "stabilizer", "sway bar link"),
        positions=("front_or_rear_required", "left_right_when_split"),
        critical_vehicle_fields=("axle", "side", "market", "production_date", "suspension_type", "drivetrain"),
        quantity_basis="piece; pair only when both sides are explicitly requested",
        partsapi_cat_candidates=("stabilizer link", "стойка стабилизатора"),
        confidence=0.88,
    ),
    PartIntentRule(
        intent_id="lower_control_arm",
        canonical_name_ru="нижний рычаг подвески",
        canonical_name_en="lower control arm",
        patterns=(r"нижн\w*\s+рыч", r"рыч\w*\s+нижн", r"lower\s+control\s+arm"),
        catalog_groups_ru=("подвеска", "рычаг подвески", "нижний рычаг"),
        catalog_groups_en=("suspension", "control arm", "lower arm"),
        positions=("front_or_rear_required", "left_right_required"),
        critical_vehicle_fields=("axle", "side", "production_date", "drivetrain", "suspension_type", "market"),
        quantity_basis="piece by side",
        partsapi_cat_candidates=("control arm", "lower arm", "рычаг подвески"),
        confidence=0.86,
    ),
    PartIntentRule(
        intent_id="outer_cv_joint",
        canonical_name_ru="наружный ШРУС",
        canonical_name_en="outer CV joint",
        patterns=(r"\bшрус", r"cv\s+joint", r"outer\s+joint"),
        catalog_groups_ru=("привод", "шрус", "приводной вал"),
        catalog_groups_en=("driveshaft", "cv joint", "outer joint"),
        positions=("front_or_rear_required", "left_right_required", "inner_outer_required"),
        critical_vehicle_fields=("side", "inner_outer", "drivetrain", "abs_ring", "transmission", "production_date"),
        quantity_basis="piece",
        partsapi_cat_candidates=("cv joint", "drive shaft joint", "шрус"),
        confidence=0.84,
    ),
    PartIntentRule(
        intent_id="drive_shaft",
        canonical_name_ru="приводной вал",
        canonical_name_en="drive shaft / axle shaft",
        patterns=(r"приводн\w*\s+вал", r"\bполуос", r"drive\s+shaft", r"axle\s+shaft"),
        catalog_groups_ru=("привод", "приводной вал", "полуось"),
        catalog_groups_en=("driveshaft", "drive shaft", "axle shaft"),
        positions=("front_or_rear_required", "left_right_required"),
        critical_vehicle_fields=("side", "axle", "drivetrain", "transmission", "abs_ring", "production_date"),
        quantity_basis="piece by side and axle",
        partsapi_cat_candidates=("drive shaft", "axle shaft", "приводной вал", "полуось"),
        confidence=0.78,
        clarification_fields=("side", "axle"),
    ),
    PartIntentRule(
        intent_id="spark_plug",
        canonical_name_ru="свеча зажигания",
        canonical_name_en="spark plug",
        patterns=(r"свеч\w*\s+зажиган", r"\bсвечи\b", r"spark\s+plugs?"),
        catalog_groups_ru=("система зажигания", "свечи зажигания"),
        catalog_groups_en=("ignition system", "spark plug"),
        positions=("per_cylinder_quantity_required",),
        critical_vehicle_fields=("engine", "engine_code", "production_date", "fuel_type", "market"),
        quantity_basis="set by cylinder count unless one plug is explicitly requested",
        partsapi_cat_candidates=("spark plug", "свеча зажигания"),
        confidence=0.86,
    ),
    PartIntentRule(
        intent_id="oil_filter",
        canonical_name_ru="масляный фильтр",
        canonical_name_en="oil filter",
        patterns=(r"маслян\w*\s+фильтр", r"фильтр\w*\s+масл", r"oil\s+filter"),
        catalog_groups_ru=("двигатель", "система смазки", "масляный фильтр"),
        catalog_groups_en=("engine", "lubrication", "oil filter"),
        positions=("engine_variant_required",),
        critical_vehicle_fields=("engine", "engine_code", "production_date", "market"),
        quantity_basis="piece",
        partsapi_cat_candidates=("oil filter", "масляный фильтр"),
        confidence=0.88,
    ),
    PartIntentRule(
        intent_id="air_filter",
        canonical_name_ru="воздушный фильтр",
        canonical_name_en="air filter",
        patterns=(r"воздушн\w*\s+фильтр", r"фильтр\w*\s+возд", r"air\s+filter"),
        catalog_groups_ru=("двигатель", "впуск", "воздушный фильтр"),
        catalog_groups_en=("engine", "intake", "air filter"),
        positions=("engine_variant_required",),
        critical_vehicle_fields=("engine", "market", "production_date", "body"),
        quantity_basis="piece",
        partsapi_cat_candidates=("air filter", "воздушный фильтр"),
        confidence=0.86,
    ),
    PartIntentRule(
        intent_id="cabin_filter",
        canonical_name_ru="салонный фильтр",
        canonical_name_en="cabin filter",
        patterns=(r"салонн\w*\s+фильтр", r"фильтр\w*\s+салон", r"cabin\s+filter", r"pollen\s+filter"),
        catalog_groups_ru=("отопление и кондиционер", "салонный фильтр"),
        catalog_groups_en=("hvac", "cabin filter", "pollen filter"),
        positions=("market_or_hvac_required",),
        critical_vehicle_fields=("market", "production_date", "hvac", "body"),
        quantity_basis="piece or kit if paired",
        partsapi_cat_candidates=("cabin filter", "pollen filter", "салонный фильтр"),
        confidence=0.84,
    ),
    PartIntentRule(
        intent_id="belt_tensioner_or_roller",
        canonical_name_ru="ремень или ролик навесного оборудования",
        canonical_name_en="belt/tensioner/roller",
        patterns=(
            r"ролик\w*",
            r"натяжител",
            r"ремень\w*\s+(генератор|привод|навес)",
            r"belt\s+(tensioner|roller|idler)",
            r"drive\s+belt",
        ),
        catalog_groups_ru=("двигатель", "ременный привод", "ролики и натяжители"),
        catalog_groups_en=("engine", "belt drive", "tensioner", "idler roller"),
        positions=("engine_variant_required",),
        critical_vehicle_fields=("engine", "engine_code", "production_date", "with_ac", "belt_route"),
        quantity_basis="piece or kit contents must be explicit",
        partsapi_cat_candidates=("drive belt", "belt tensioner", "idler roller", "ремень приводной", "ролик"),
        confidence=0.8,
        clarification_fields=("part_group",),
    ),
    PartIntentRule(
        intent_id="ac_compressor",
        canonical_name_ru="компрессор кондиционера",
        canonical_name_en="air conditioning compressor",
        patterns=(r"компрессор\w*\s+кондиц", r"кондиц\w*\s+компресс", r"\bac\s+compressor", r"a/c\s+compressor"),
        catalog_groups_ru=("кондиционер", "компрессор кондиционера"),
        catalog_groups_en=("air conditioning", "a/c compressor", "compressor"),
        positions=("engine_variant_required",),
        critical_vehicle_fields=(
            "engine",
            "production_date",
            "market",
            "pulley/clutch_type",
            "refrigerant",
            "mounting_type",
        ),
        quantity_basis="piece",
        partsapi_cat_candidates=("a/c compressor", "air conditioning compressor", "компрессор кондиционера"),
        confidence=0.84,
    ),
    PartIntentRule(
        intent_id="shock_absorber",
        canonical_name_ru="амортизатор",
        canonical_name_en="shock absorber / strut",
        patterns=(r"\bамортизатор", r"\bстойк\w*\s+аморт", r"shock\s+absorber", r"\bstrut\b"),
        catalog_groups_ru=("подвеска", "амортизатор", "стойка амортизатора"),
        catalog_groups_en=("suspension", "shock absorber", "strut"),
        positions=("front_or_rear_required", "left_right_when_split"),
        critical_vehicle_fields=("axle", "side", "production_date", "suspension_type", "drivetrain", "market"),
        quantity_basis="piece; pair only if both sides are explicitly requested",
        partsapi_cat_candidates=("shock absorber", "strut", "амортизатор"),
        confidence=0.84,
    ),
    PartIntentRule(
        intent_id="fuel_injector",
        canonical_name_ru="топливная форсунка",
        canonical_name_en="fuel injector",
        patterns=(r"топливн\w*\s+форс", r"форсунк\w*\s+топлив", r"fuel\s+injectors?", r"\binjectors?\b"),
        catalog_groups_ru=("топливная система", "форсунка", "топливная форсунка"),
        catalog_groups_en=("fuel system", "fuel injector", "injector"),
        positions=("per_cylinder_or_failed_unit_quantity_required",),
        critical_vehicle_fields=("engine", "engine_code", "fuel_system", "production_date", "market"),
        quantity_basis="piece or full set must be explicit",
        partsapi_cat_candidates=("fuel injector", "injector", "форсунка топливная"),
        confidence=0.86,
    ),
    PartIntentRule(
        intent_id="oxygen_sensor",
        canonical_name_ru="датчик кислорода",
        canonical_name_en="oxygen sensor / lambda sensor",
        patterns=(r"лямбд", r"датчик\w*\s+кислород", r"oxygen\s+sensor", r"lambda\s+sensor"),
        catalog_groups_ru=("выпуск", "датчики", "датчик кислорода"),
        catalog_groups_en=("exhaust", "sensors", "oxygen sensor", "lambda sensor"),
        positions=("upstream_downstream_required",),
        critical_vehicle_fields=("engine", "emissions_standard", "production_date", "bank", "before_after_cat"),
        quantity_basis="piece by bank and position",
        partsapi_cat_candidates=("oxygen sensor", "lambda sensor", "датчик кислорода"),
        confidence=0.82,
        clarification_fields=("position",),
    ),
    PartIntentRule(
        intent_id="timing_chain_kit",
        canonical_name_ru="комплект ГРМ",
        canonical_name_en="timing chain or belt kit",
        patterns=(r"\bгрм\b", r"цеп\w*\s+грм", r"ремн\w*\s+грм", r"timing\s+(chain|belt)"),
        catalog_groups_ru=("двигатель", "газораспределительный механизм", "комплект ГРМ"),
        catalog_groups_en=("engine", "timing chain", "timing belt", "timing kit"),
        positions=("engine_variant_required",),
        critical_vehicle_fields=(
            "engine",
            "engine_code",
            "production_date",
            "market",
            "camshaft_phasers",
            "chain_or_belt",
        ),
        quantity_basis="kit contents must be listed explicitly",
        partsapi_cat_candidates=("timing chain kit", "timing belt kit", "комплект грм"),
        confidence=0.82,
    ),
    PartIntentRule(
        intent_id="water_pump_or_thermostat",
        canonical_name_ru="помпа или термостат",
        canonical_name_en="water pump or thermostat",
        patterns=(r"\bпомп\w*\b", r"водян\w*\s+насос", r"термостат", r"water\s+pump", r"thermostat"),
        catalog_groups_ru=("система охлаждения", "помпа", "термостат"),
        catalog_groups_en=("cooling system", "water pump", "thermostat"),
        positions=("engine_variant_required",),
        critical_vehicle_fields=("engine", "engine_code", "production_date", "cooling_package", "market"),
        quantity_basis="piece or kit contents must be explicit",
        partsapi_cat_candidates=("water pump", "thermostat", "помпа", "термостат"),
        confidence=0.82,
    ),
    PartIntentRule(
        intent_id="clutch_kit",
        canonical_name_ru="комплект сцепления",
        canonical_name_en="clutch kit",
        patterns=(r"комплект\w*\s+сцеп", r"\bсцеплен", r"clutch\s+kit", r"clutch\s+disc"),
        catalog_groups_ru=("трансмиссия", "сцепление", "комплект сцепления"),
        catalog_groups_en=("transmission", "clutch", "clutch kit"),
        positions=("transmission_unit_required",),
        critical_vehicle_fields=("engine", "transmission", "drivetrain", "production_date", "flywheel_type"),
        quantity_basis="kit contents must be explicit",
        partsapi_cat_candidates=("clutch kit", "clutch", "комплект сцепления"),
        confidence=0.82,
    ),
    PartIntentRule(
        intent_id="starter",
        canonical_name_ru="стартер",
        canonical_name_en="starter motor",
        patterns=(r"\bстартер", r"starter\s+motor"),
        catalog_groups_ru=("электрооборудование", "стартер"),
        catalog_groups_en=("electrical", "starter motor"),
        positions=("engine_transmission_required",),
        critical_vehicle_fields=("engine", "transmission", "production_date", "start_stop", "market"),
        quantity_basis="piece",
        partsapi_cat_candidates=("starter motor", "стартер"),
        confidence=0.84,
    ),
    PartIntentRule(
        intent_id="alternator",
        canonical_name_ru="генератор",
        canonical_name_en="alternator",
        patterns=(r"\bгенератор", r"alternator"),
        catalog_groups_ru=("электрооборудование", "генератор"),
        catalog_groups_en=("electrical", "alternator"),
        positions=("engine_variant_required",),
        critical_vehicle_fields=("engine", "production_date", "amperage", "pulley_type", "start_stop"),
        quantity_basis="piece",
        partsapi_cat_candidates=("alternator", "генератор"),
        confidence=0.84,
    ),
    PartIntentRule(
        intent_id="engine_assembly",
        canonical_name_ru="двигатель в сборе",
        canonical_name_en="engine assembly",
        patterns=(
            r"^\s*двс\s*$",
            r"^\s*двигатель\s*$",
            r"двигател\w*\s+в\s+сбор",
            r"контрактн\w*\s+двигател",
            r"двигател\w*\s+контракт",
            r"engine\s+assembly",
            r"used\s+engine",
        ),
        catalog_groups_ru=("двигатель", "двигатель в сборе", "блок двигателя"),
        catalog_groups_en=("engine", "engine assembly", "long block", "short block"),
        positions=("engine_code_required",),
        critical_vehicle_fields=(
            "engine",
            "engine_code",
            "market",
            "production_date",
            "emissions_standard",
            "transmission",
        ),
        quantity_basis="assembly; included ancillaries must be listed",
        partsapi_cat_candidates=("engine assembly", "long block", "двигатель в сборе"),
        confidence=0.78,
    ),
    PartIntentRule(
        intent_id="headlight",
        canonical_name_ru="фара",
        canonical_name_en="headlight",
        patterns=(r"\bфар(?!коп)\w*", r"headlight", r"headlamp"),
        catalog_groups_ru=("кузов", "освещение", "фара"),
        catalog_groups_en=("body", "lighting", "headlight"),
        positions=("left_right_required",),
        critical_vehicle_fields=("side", "market", "production_date", "body", "lamp_type", "afs/leveling"),
        quantity_basis="piece by side",
        partsapi_cat_candidates=("headlight", "headlamp", "фара"),
        confidence=0.82,
    ),
    PartIntentRule(
        intent_id="rear_view_camera",
        canonical_name_ru="камера заднего вида",
        canonical_name_en="rear view camera",
        patterns=(r"камер\w*\s+задн\w*\s+вид", r"rear\s+(view|backup)\s+camera", r"backup\s+camera"),
        catalog_groups_ru=("электрооборудование", "камера заднего вида", "парковочная система"),
        catalog_groups_en=("electrical", "rear view camera", "parking assist camera"),
        positions=("body_trim_and_head_unit_required",),
        critical_vehicle_fields=("market", "production_date", "trim/options", "head_unit", "body", "tailgate"),
        quantity_basis="piece",
        partsapi_cat_candidates=("rear view camera", "parking camera", "камера заднего вида"),
        confidence=0.76,
    ),
    PartIntentRule(
        intent_id="wheel_stud",
        canonical_name_ru="колесная шпилька",
        canonical_name_en="wheel stud",
        patterns=(r"шпильк\w*\s+кол", r"колесн\w*\s+шпиль", r"wheel\s+stud"),
        catalog_groups_ru=("ступица", "колесный крепеж", "шпилька колеса"),
        catalog_groups_en=("hub", "wheel fastener", "wheel stud"),
        positions=("wheel_position_required",),
        critical_vehicle_fields=("axle", "market", "production_date", "hub_type", "thread_size"),
        quantity_basis="piece",
        partsapi_cat_candidates=("wheel stud", "wheel bolt", "шпилька колеса"),
        confidence=0.82,
    ),
    PartIntentRule(
        intent_id="wheel_hub",
        canonical_name_ru="ступица колеса",
        canonical_name_en="wheel hub",
        patterns=(r"\bступиц", r"wheel\s+hub", r"hub\s+bearing", r"ступичн\w*\s+подшип"),
        catalog_groups_ru=("ступица", "подшипник ступицы", "колесный узел"),
        catalog_groups_en=("wheel hub", "hub bearing", "wheel bearing"),
        positions=("axle_required", "left_right_required"),
        critical_vehicle_fields=(
            "axle",
            "side",
            "market",
            "production_date",
            "drivetrain",
            "abs_sensor",
            "bearing_type",
        ),
        quantity_basis="piece by axle and side",
        partsapi_cat_candidates=("wheel hub", "hub bearing", "wheel bearing", "ступица колеса"),
        confidence=0.84,
    ),
    PartIntentRule(
        intent_id="injector_seal_washer",
        canonical_name_ru="шайба форсунки",
        canonical_name_en="injector seal washer",
        patterns=(r"шайб\w*\s+форс", r"форсунк\w*\s+шайб", r"injector\s+(seal|washer)"),
        catalog_groups_ru=("топливная система", "форсунка", "уплотнение форсунки"),
        catalog_groups_en=("fuel system", "injector", "injector seal"),
        positions=("per_injector_quantity_required",),
        critical_vehicle_fields=("engine", "production_date", "fuel_system", "injector_type"),
        quantity_basis="piece per injector; kit if supplier bundles",
        partsapi_cat_candidates=("injector washer", "injector seal", "шайба форсунки"),
        confidence=0.86,
    ),
    PartIntentRule(
        intent_id="transmission_filter",
        canonical_name_ru="фильтр АКПП",
        canonical_name_en="automatic transmission filter",
        patterns=(r"фильтр\w*\s+акп", r"акп\w*\s+фильтр", r"transmission\s+filter", r"atf\s+filter"),
        catalog_groups_ru=("трансмиссия", "фильтр АКПП", "поддон АКПП"),
        catalog_groups_en=("transmission", "automatic transmission filter", "oil pan/filter"),
        positions=("transmission_unit_required",),
        critical_vehicle_fields=("transmission_code", "production_date", "drivetrain", "pan_type", "market"),
        quantity_basis="kit_or_piece_must_be_explicit",
        partsapi_cat_candidates=("transmission filter", "automatic transmission filter", "фильтр акпп"),
        confidence=0.84,
    ),
)


def normalize_part_intent(
    raw: str | None, *, axle: str | None = None, side: str | None = None, position: str | None = None
) -> dict[str, Any]:
    text = str(raw or "").strip()
    axle = axle.strip() if axle else None
    side = side.strip() if side else None
    position = position.strip() if position else None
    lowered = text.casefold()
    matched = next((rule for rule in PART_INTENT_RULES if rule.matches(lowered)), None)

    if matched is None:
        missing_fields = [field for field, value in (("part_group", None), ("axle", axle), ("side", side)) if not value]
        return {
            "ok": True,
            "raw": text,
            "recognized": False,
            "intent_id": "unknown",
            "confidence": 0.2 if text else 0.0,
            "catalog_search_terms": [text] if text else [],
            "positions": [value for value in [axle, side, position] if value],
            "critical_vehicle_fields": ["make", "model", "market", "production_date", "engine", "drivetrain"],
            "required_position_fields": ["part_group", "axle", "side"],
            "partsapi_category_candidates": [],
            "catalog_group_terms": [],
            "risk_fields": ["make", "model", "market", "production_date", "engine", "drivetrain"],
            "clarification_required": bool(text),
            "clarification_fields": missing_fields,
        }

    payload = asdict(matched)
    explicit_positions = [value for value in [axle, side, position] if value]
    required_position_tokens = {
        "axle": any(
            "front_or_rear_required" in value or "front_axle_or_rear_axle_required" in value or value == "axle_required"
            for value in matched.positions
        ),
        "side": any("left_right_required" in value or value == "side_required" for value in matched.positions),
        "inner_outer": any("inner_outer_required" in value for value in matched.positions),
    }
    supplied_context = {
        "axle": axle,
        "side": side,
        "position": position,
        "inner_outer": position,
    }
    missing_fields = [field for field in matched.clarification_fields if not supplied_context.get(field)]
    if required_position_tokens["axle"] and not any(value for value in [axle, position] if value):
        missing_fields.append("axle")
    if required_position_tokens["side"] and not side:
        missing_fields.append("side")
    if required_position_tokens["inner_outer"] and not position:
        missing_fields.append("inner_outer")
    missing_fields = list(dict.fromkeys(missing_fields))
    terms = (
        list(matched.catalog_groups_ru)
        + list(matched.catalog_groups_en)
        + [matched.canonical_name_ru, matched.canonical_name_en]
    )
    derived_required_fields = []
    if required_position_tokens["axle"]:
        derived_required_fields.append("axle")
    if required_position_tokens["side"]:
        derived_required_fields.append("side")
    if required_position_tokens["inner_outer"]:
        derived_required_fields.append("inner_outer")
    derived_required_fields.extend(matched.clarification_fields)
    derived_required_fields = list(dict.fromkeys(derived_required_fields))
    if text:
        terms.insert(0, text)
    payload.update(
        {
            "ok": True,
            "raw": text,
            "recognized": True,
            "catalog_search_terms": list(dict.fromkeys(term for term in terms if term)),
            "catalog_group_terms": list(
                dict.fromkeys(list(matched.catalog_groups_ru) + list(matched.catalog_groups_en))
            ),
            "partsapi_category_candidates": list(matched.partsapi_cat_candidates),
            "required_position_fields": derived_required_fields,
            "risk_fields": list(matched.critical_vehicle_fields),
            "explicit_positions": explicit_positions,
            "clarification_required": bool(missing_fields),
            "clarification_fields": missing_fields,
        }
    )
    return payload
