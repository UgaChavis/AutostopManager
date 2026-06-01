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
    price_basis_hint: str
    fitment_caveats: tuple[str, ...]
    partsapi_cat_candidates: tuple[str, ...] = ()
    confidence: float = 0.7

    def matches(self, text: str) -> bool:
        return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in self.patterns)


PART_INTENT_RULES: tuple[PartIntentRule, ...] = (
    PartIntentRule(
        intent_id="front_brake_pads",
        canonical_name_ru="передние тормозные колодки",
        canonical_name_en="front brake pads",
        patterns=(r"\bпередн\w*\s+колод", r"\bколод\w*\s+перед", r"front\s+brake\s+pads?"),
        catalog_groups_ru=("тормозная система", "колодки тормозные", "передние тормоза"),
        catalog_groups_en=("brake system", "front brake pads", "disc brake front"),
        positions=("front_axle",),
        critical_vehicle_fields=("market", "production_date", "grade/options", "brake_system", "wheel_size", "engine", "drivetrain"),
        quantity_basis="axle_set",
        price_basis_hint="quote one axle set unless supplier explicitly prices one side or one pad kit differently",
        fitment_caveats=(
            "front/rear axle must be explicit",
            "brake disc diameter, PR/options, or trim can split pad shape",
            "cross numbers are not fitment proof without VIN/frame applicability",
        ),
        partsapi_cat_candidates=("brake pads", "front brake pads", "колодки тормозные передние"),
        confidence=0.9,
    ),
    PartIntentRule(
        intent_id="rear_brake_pads",
        canonical_name_ru="задние тормозные колодки",
        canonical_name_en="rear brake pads",
        patterns=(r"\bзадн\w*\s+колод", r"\bколод\w*\s+зад", r"rear\s+brake\s+pads?"),
        catalog_groups_ru=("тормозная система", "колодки тормозные", "задние тормоза"),
        catalog_groups_en=("brake system", "rear brake pads", "disc/drum brake rear"),
        positions=("rear_axle",),
        critical_vehicle_fields=("market", "production_date", "grade/options", "brake_system", "parking_brake_type", "drivetrain"),
        quantity_basis="axle_set",
        price_basis_hint="separate disc pads from drum shoes and parking brake shoes",
        fitment_caveats=(
            "rear brakes can be disc or drum",
            "parking brake shoe and service brake pad are different parts",
        ),
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
        critical_vehicle_fields=("axle", "market", "production_date", "brake_system", "diameter", "vented/solid", "wheel_size"),
        quantity_basis="piece_or_pair_must_be_explicit",
        price_basis_hint="do not mix one-disc price with pair total",
        fitment_caveats=("diameter and axle side are common split points",),
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
        price_basis_hint="quote per piece and total quantity separately",
        fitment_caveats=("front and rear links usually differ", "left/right can differ on some platforms"),
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
        price_basis_hint="left and right part numbers/prices can differ",
        fitment_caveats=("side is VIN-critical", "bushing/ball-joint included status must be visible"),
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
        price_basis_hint="quote joint kit contents: boot, grease, clips, nut",
        fitment_caveats=("ABS ring teeth/spline count can split fitment", "inner and outer joints are different"),
        partsapi_cat_candidates=("cv joint", "drive shaft joint", "шрус"),
        confidence=0.84,
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
        price_basis_hint="quote full engine set and keep one-piece price separate",
        fitment_caveats=("heat range, electrode type, and engine code are VIN-critical",),
        partsapi_cat_candidates=("spark plug", "свеча зажигания"),
        confidence=0.86,
    ),
    PartIntentRule(
        intent_id="ac_compressor",
        canonical_name_ru="компрессор кондиционера",
        canonical_name_en="air conditioning compressor",
        patterns=(r"компрессор\w*\s+кондиц", r"кондиц\w*\s+компресс", r"\bac\s+compressor", r"a/c\s+compressor"),
        catalog_groups_ru=("кондиционер", "компрессор кондиционера"),
        catalog_groups_en=("air conditioning", "a/c compressor", "compressor"),
        positions=("engine_variant_required",),
        critical_vehicle_fields=("engine", "production_date", "market", "pulley/clutch_type", "refrigerant", "mounting_type"),
        quantity_basis="piece",
        price_basis_hint="separate new, remanufactured, and used/contract compressor prices",
        fitment_caveats=("pulley, connector, clutch/control valve, and mounting can split fitment",),
        partsapi_cat_candidates=("a/c compressor", "air conditioning compressor", "компрессор кондиционера"),
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
        price_basis_hint="do not mix one injector, used set, and new OEM set prices",
        fitment_caveats=("engine code, injector generation, flow/calibration, and seal kit must match",),
        partsapi_cat_candidates=("fuel injector", "injector", "форсунка топливная"),
        confidence=0.86,
    ),
    PartIntentRule(
        intent_id="timing_chain_kit",
        canonical_name_ru="комплект ГРМ",
        canonical_name_en="timing chain or belt kit",
        patterns=(r"\bгрм\b", r"цеп\w*\s+грм", r"ремн\w*\s+грм", r"timing\s+(chain|belt)"),
        catalog_groups_ru=("двигатель", "газораспределительный механизм", "комплект ГРМ"),
        catalog_groups_en=("engine", "timing chain", "timing belt", "timing kit"),
        positions=("engine_variant_required",),
        critical_vehicle_fields=("engine", "engine_code", "production_date", "market", "camshaft_phasers", "chain_or_belt"),
        quantity_basis="kit contents must be listed explicitly",
        price_basis_hint="price the selected kit and separately list seals, guides, phasers, bolts, and fluids when not included",
        fitment_caveats=("chain/belt route, guides, tensioner revision, phasers, and engine code are VIN-critical",),
        partsapi_cat_candidates=("timing chain kit", "timing belt kit", "комплект грм"),
        confidence=0.82,
    ),
    PartIntentRule(
        intent_id="engine_assembly",
        canonical_name_ru="двигатель в сборе",
        canonical_name_en="engine assembly",
        patterns=(r"\bдвс\b", r"\bдвигател\w*\b", r"engine\s+assembly", r"used\s+engine"),
        catalog_groups_ru=("двигатель", "двигатель в сборе", "блок двигателя"),
        catalog_groups_en=("engine", "engine assembly", "long block", "short block"),
        positions=("engine_code_required",),
        critical_vehicle_fields=("engine", "engine_code", "market", "production_date", "emissions_standard", "transmission"),
        quantity_basis="assembly; included ancillaries must be listed",
        price_basis_hint="separate bare engine, long block, complete used engine, warranty, and delivery",
        fitment_caveats=("engine code, emissions, sensors, harness, mounts, and transmission compatibility are critical",),
        partsapi_cat_candidates=("engine assembly", "long block", "двигатель в сборе"),
        confidence=0.78,
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
        price_basis_hint="separate OEM camera, used camera, and harness/connector repair",
        fitment_caveats=("camera connector, head unit, trim, and tailgate/handle assembly can split fitment",),
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
        price_basis_hint="quote per stud; add nut only if requested or damaged",
        fitment_caveats=("thread length and knurl diameter must match",),
        partsapi_cat_candidates=("wheel stud", "wheel bolt", "шпилька колеса"),
        confidence=0.82,
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
        price_basis_hint="do not price one washer as a full injector seal kit",
        fitment_caveats=("engine code and injector type are critical",),
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
        price_basis_hint="separate filter, pan/filter assembly, gasket, bolts, and ATF",
        fitment_caveats=("same vehicle can use different gearbox codes",),
        partsapi_cat_candidates=("transmission filter", "automatic transmission filter", "фильтр акпп"),
        confidence=0.84,
    ),
)


def normalize_part_intent(raw: str | None, *, axle: str | None = None, side: str | None = None, position: str | None = None) -> dict[str, Any]:
    text = str(raw or "").strip()
    lowered = text.casefold()
    matched = next((rule for rule in PART_INTENT_RULES if rule.matches(lowered)), None)

    if matched is None:
        return {
            "ok": True,
            "raw": text,
            "recognized": False,
            "intent_id": "unknown",
            "confidence": 0.2 if text else 0.0,
            "catalog_search_terms": [text] if text else [],
            "positions": [value for value in [axle, side, position] if value],
            "critical_vehicle_fields": ["make", "model", "market", "production_date", "engine", "drivetrain"],
            "fitment_caveats": ["Unknown part intent; request exact part group, axle/side, and old part/OEM number when available."],
        }

    payload = asdict(matched)
    explicit_positions = [value for value in [axle, side, position] if value]
    terms = list(matched.catalog_groups_ru) + list(matched.catalog_groups_en) + [matched.canonical_name_ru, matched.canonical_name_en]
    if text:
        terms.insert(0, text)
    payload.update(
        {
            "ok": True,
            "raw": text,
            "recognized": True,
            "catalog_search_terms": list(dict.fromkeys(term for term in terms if term)),
            "explicit_positions": explicit_positions,
            "missing_position_context": not explicit_positions
            and any("required" in value for value in matched.positions),
        }
    )
    return payload
