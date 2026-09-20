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


def _paired_axle_patterns(front: str, rear: str, part: str) -> tuple[str, ...]:
    join = r"\s*(?:/|,|&|\+|(?:,?\s*)(?:и\b|а\s+также\b|and\b)(?:\s+(?:также|also)\b)?)\s*"
    end = r"(?=\s*(?:$|[,;.!?]))"
    return tuple(
        pattern
        for first, second in ((front, rear), (rear, front))
        for pattern in (
            rf"{first}{join}{second}\s+{part}",
            rf"{part}\s+{first}{join}{second}{end}",
            rf"{first}\s+{part}{join}{second}(?:\s+{part})?{end}",
        )
    )


PART_INTENT_RULES: tuple[PartIntentRule, ...] = (
    PartIntentRule(
        intent_id="brake_pads_multiple_axles",
        canonical_name_ru="тормозные колодки на обе оси",
        canonical_name_en="front and rear brake pads",
        patterns=_paired_axle_patterns(r"\bпередн\w*", r"\bзадн\w*", r"(?:тормозн\w*\s+)?колод\w*")
        + _paired_axle_patterns(r"\bfront\b", r"\brear\b", r"(?:brake\s+)?pads?\b"),
        catalog_groups_ru=("тормозная система", "колодки тормозные"),
        catalog_groups_en=("brake system", "brake pads"),
        positions=("front_and_rear_axles",),
        critical_vehicle_fields=(
            "market",
            "production_date",
            "grade/options",
            "brake_system",
            "wheel_size",
            "engine",
            "drivetrain",
        ),
        quantity_basis="two axle sets; resolve each axle as a separate catalog request",
        confidence=0.92,
        clarification_fields=("split_by_axle",),
    ),
    PartIntentRule(
        intent_id="brake_pads_unspecified_axle",
        canonical_name_ru="тормозные колодки",
        canonical_name_en="brake pads",
        patterns=(r"\bколод\w*", r"\bbrake\s+pads?\b"),
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
        intent_id="inner_cv_joint",
        canonical_name_ru="внутренний ШРУС",
        canonical_name_en="inner CV joint",
        patterns=(
            r"\bвнутрен\w*\s+шрус",
            r"\bшрус\w*\s+внутрен",
            r"inner\s+cv\s+joint",
            r"inner\s+joint",
        ),
        catalog_groups_ru=("привод", "шрус", "приводной вал"),
        catalog_groups_en=("driveshaft", "cv joint", "inner joint"),
        positions=("front_or_rear_required", "left_right_required"),
        critical_vehicle_fields=("axle", "side", "drivetrain", "transmission", "production_date"),
        quantity_basis="piece",
        partsapi_cat_candidates=("inner cv joint", "шрус внутренний"),
        confidence=0.88,
    ),
    PartIntentRule(
        intent_id="outer_cv_joint",
        canonical_name_ru="наружный ШРУС",
        canonical_name_en="outer CV joint",
        patterns=(r"\bнаружн\w*\s+шрус", r"\bшрус\w*\s+наруж", r"outer\s+cv\s+joint", r"outer\s+joint"),
        catalog_groups_ru=("привод", "шрус", "приводной вал"),
        catalog_groups_en=("driveshaft", "cv joint", "outer joint"),
        positions=("front_or_rear_required", "left_right_required"),
        critical_vehicle_fields=("axle", "side", "drivetrain", "abs_ring", "transmission", "production_date"),
        quantity_basis="piece",
        partsapi_cat_candidates=("outer cv joint", "шрус наружный"),
        confidence=0.84,
    ),
    PartIntentRule(
        intent_id="cv_joint_unspecified",
        canonical_name_ru="ШРУС (внутренний или наружный)",
        canonical_name_en="CV joint (inner or outer)",
        patterns=(r"\bшрус", r"cv\s+joint"),
        catalog_groups_ru=("привод", "шрус", "приводной вал"),
        catalog_groups_en=("driveshaft", "cv joint"),
        positions=("front_or_rear_required", "left_right_required", "inner_outer_required"),
        critical_vehicle_fields=(
            "axle",
            "side",
            "inner_outer",
            "drivetrain",
            "abs_ring",
            "transmission",
            "production_date",
        ),
        quantity_basis="piece after inner/outer confirmation",
        partsapi_cat_candidates=("cv joint", "drive shaft joint", "шрус"),
        confidence=0.72,
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
        intent_id="glow_plug",
        canonical_name_ru="свеча накаливания",
        canonical_name_en="glow plug",
        patterns=(r"\bсвеч\w*\s+накал", r"\bнакал\w*\s+свеч", r"glow\s+plugs?"),
        catalog_groups_ru=("дизельный двигатель", "свечи накаливания"),
        catalog_groups_en=("diesel engine", "glow plug"),
        positions=("per_cylinder_quantity_required",),
        critical_vehicle_fields=("engine", "engine_code", "production_date", "fuel_type", "market"),
        quantity_basis="set by cylinder count unless one plug is explicitly requested",
        partsapi_cat_candidates=("glow plug", "свеча накаливания"),
        confidence=0.88,
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
        patterns=(
            r"\bамортизатор",
            r"\bстойк\w*\s+аморт",
            r"\bамортизацион\w*\s+стойк\w*",
            r"shock\s+absorber",
            r"\bstrut\b",
        ),
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


def _match_parts(text: str) -> list[PartIntentRule]:
    # Prefer complete names over contained generic names (injector washer / injector).
    text = text.casefold()
    candidates = [
        (match.start(), match.end(), rule)
        for rule in PART_INTENT_RULES
        for pattern in rule.patterns
        for match in re.finditer(pattern, text)
    ]
    selected: list[tuple[int, int, PartIntentRule]] = []
    for start, end, rule in sorted(candidates, key=lambda item: (item[0] - item[1], item[0])):
        if all(end <= other_start or start >= other_end for other_start, other_end, _ in selected):
            selected.append((start, end, rule))
    return list({rule.intent_id: rule for _, _, rule in sorted(selected, key=lambda item: item[0])}.values())


def _legacy_position_coordinates(position: str | None) -> tuple[str | None, str | None]:
    """Map a legacy position value to at most one structured coordinate."""

    if not position:
        return None, None
    lowered = position.casefold()
    axle_hint = bool(
        re.search(r"(?:^|[\s_/-])(?:front(?:_axle)?|rear(?:_axle)?|передн\w*|задн\w*)(?:$|[\s_/-])", lowered)
    )
    inner_outer_hint = bool(
        re.search(r"(?:^|[\s_/-])(?:inner|outer|internal|external|внутрен\w*|наружн\w*)(?:$|[\s_/-])", lowered)
    )
    if axle_hint == inner_outer_hint:
        return None, None
    return (position, None) if axle_hint else (None, position)


def _single_text_position_hint(text: str, patterns: dict[str, str]) -> str | None:
    """Return one unambiguous position coordinate found in the part wording."""

    normalized = re.sub(r"[_/\\-]+", " ", text.casefold())
    matches = {value for value, pattern in patterns.items() if re.search(pattern, normalized)}
    return next(iter(matches)) if len(matches) == 1 else None


def _infer_position_context(text: str) -> dict[str, str]:
    """Extract only unambiguous axle/side hints from a recognised part request.

    Wording such as "front and rear" deliberately yields no axle value and
    keeps the request on the clarification path.
    """

    coordinates = {
        "axle": _single_text_position_hint(
            text,
            {
                "front": r"\b(?:front(?:\s+axle)?|передн\w*)\b",
                "rear": r"\b(?:rear(?:\s+axle)?|задн\w*)\b",
            },
        ),
        "side": _single_text_position_hint(
            text,
            {
                "left": r"\b(?:left|lh|лев\w*)\b",
                "right": r"\b(?:right|rh|прав\w*)\b",
            },
        ),
        "inner_outer": _single_text_position_hint(
            text,
            {
                "inner": r"\b(?:inner|internal|inboard|внутрен\w*)\b",
                "outer": r"\b(?:outer|external|outboard|наружн\w*)\b",
            },
        ),
    }
    return {key: value for key, value in coordinates.items() if value is not None}


def normalize_part_intent(
    raw: str | None,
    *,
    axle: str | None = None,
    side: str | None = None,
    position: str | None = None,
    inner_outer: str | None = None,
) -> dict[str, Any]:
    text = str(raw or "").strip()
    axle = axle.strip() if axle else None
    side = side.strip() if side else None
    position = position.strip() if position else None
    inner_outer = inner_outer.strip() if inner_outer else None
    legacy_axle, legacy_inner_outer = _legacy_position_coordinates(position)
    inferred_context = _infer_position_context(text)
    effective_axle = axle or legacy_axle or inferred_context.get("axle")
    effective_side = side or inferred_context.get("side")
    effective_inner_outer = inner_outer or legacy_inner_outer or inferred_context.get("inner_outer")
    explicit_positions = list(dict.fromkeys(value for value in [axle, side, position, inner_outer] if value))
    matches = _match_parts(text)
    matched = matches[0] if len(matches) == 1 else None

    if matched is None:
        missing_fields = (
            ["split_by_part"]
            if matches
            else [field for field, value in (("part_group", None), ("axle", axle), ("side", side)) if not value]
        )
        return {
            "ok": True,
            "raw": text,
            "recognized": bool(matches),
            "intent_id": "multiple_parts" if matches else "unknown",
            "matched_intents": [rule.intent_id for rule in matches],
            "confidence": 0.2 if text else 0.0,
            "catalog_search_terms": [text] if text else [],
            "positions": explicit_positions,
            "critical_vehicle_fields": ["make", "model", "market", "production_date", "engine", "drivetrain"],
            "required_position_fields": ["split_by_part"] if matches else ["part_group", "axle", "side"],
            "partsapi_category_candidates": [],
            "catalog_group_terms": [],
            "risk_fields": ["make", "model", "market", "production_date", "engine", "drivetrain"],
            "clarification_required": bool(text),
            "clarification_fields": missing_fields,
        }

    payload = asdict(matched)
    required_position_tokens = {
        "axle": any(
            "front_or_rear_required" in value or "front_axle_or_rear_axle_required" in value or value == "axle_required"
            for value in matched.positions
        ),
        "side": any("left_right_required" in value or value == "side_required" for value in matched.positions),
        "inner_outer": any("inner_outer_required" in value for value in matched.positions),
    }
    supplied_context = {
        "axle": effective_axle,
        "side": effective_side,
        "position": position,
        "inner_outer": effective_inner_outer,
    }
    explicit_context = {
        "axle": axle or legacy_axle,
        "side": side,
        "position": position,
        "inner_outer": inner_outer or legacy_inner_outer,
    }
    inferred_context = {
        key: value for key, value in inferred_context.items() if value and not explicit_context.get(key)
    }
    derived_required_fields = list(
        dict.fromkeys(
            [field for field, required in required_position_tokens.items() if required]
            + list(matched.clarification_fields)
        )
    )
    missing_fields = [
        field
        for field in dict.fromkeys([*matched.clarification_fields, *derived_required_fields])
        if not supplied_context.get(field)
    ]
    group_terms = list(dict.fromkeys([*matched.catalog_groups_ru, *matched.catalog_groups_en]))
    terms = [text, *group_terms, matched.canonical_name_ru, matched.canonical_name_en]
    payload.update(
        {
            "ok": True,
            "raw": text,
            "recognized": True,
            "catalog_search_terms": list(dict.fromkeys(term for term in terms if term)),
            "catalog_group_terms": group_terms,
            "partsapi_category_candidates": list(matched.partsapi_cat_candidates),
            "required_position_fields": derived_required_fields,
            "risk_fields": list(matched.critical_vehicle_fields),
            "explicit_positions": explicit_positions,
            "explicit_position_context": {key: value for key, value in explicit_context.items() if value},
            "inferred_position_context": inferred_context,
            "clarification_required": bool(missing_fields),
            "clarification_fields": missing_fields,
        }
    )
    return payload
