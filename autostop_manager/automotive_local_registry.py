"""Single local hint registry shared by composed and independent decoders.

Existing project rules are preserved as family hints, never factory build evidence.
No external brand tables or private VIN records are copied into this registry.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

REGISTRY_VERSION = "autostop.local-vehicle-hints.v1"
REGISTRY_ORIGIN = "AutoStop project rules migrated from vehicle_identity.py"
PRIMARY_LINEAGE = "autostop_local_vehicle_rules"

WMI_HINTS = {
    "WDD": {
        "make": "Mercedes-Benz",
        "manufacturer": "Mercedes-Benz Cars",
        "market": "Europe/global",
        "country": "Germany",
        "vehicle_type": "Passenger car",
    },
    "WDC": {
        "make": "Mercedes-Benz",
        "manufacturer": "Mercedes-Benz Cars / Mercedes-Benz USA market dependent",
        "market": "Europe/global",
        "country": "Germany/ROW market dependent",
        "vehicle_type": "MPV/SUV",
    },
    "WAU": {
        "make": "Audi",
        "manufacturer": "Audi AG",
        "market": "Europe/global",
        "country": "Germany",
        "vehicle_type": "Passenger car",
    },
    "WVW": {
        "make": "Volkswagen",
        "manufacturer": "Volkswagen AG",
        "market": "Europe/global",
        "country": "Germany",
        "vehicle_type": "Passenger car",
    },
    "VSK": {
        "make": "Nissan",
        "manufacturer": "Nissan Motor Iberica / Europe market dependent",
        "market": "Europe/ROW",
        "country": "Spain/ROW market dependent",
        "vehicle_type": "Passenger/SUV",
    },
    "X4X": {
        "make": "BMW",
        "manufacturer": "BMW local assembly / Russia market dependent",
        "market": "Russia/CIS",
        "country": "Russia",
        "vehicle_type": "Passenger car",
    },
    "JMZ": {
        "make": "Mazda",
        "manufacturer": "Mazda Motor Corporation",
        "market": "Europe/global",
        "country": "Japan/ROW market dependent",
        "vehicle_type": "Passenger/MPV/SUV",
    },
    "1C4": {
        "make": "Jeep",
        "manufacturer": "FCA US LLC",
        "market": "North America",
        "country": "United States",
        "vehicle_type": "MPV/SUV",
    },
    "JHL": {
        "make": "Honda",
        "manufacturer": "Honda Motor Co., Ltd.",
        "market": "Japan/global",
        "country": "Japan",
        "vehicle_type": "MPV/SUV",
    },
    "JTE": {
        "make": "Toyota",
        "manufacturer": "Toyota Motor Corporation",
        "market": "Japan/global",
        "country": "Japan",
        "vehicle_type": "MPV/SUV",
    },
    "XW8": {
        "make": "Volkswagen Group",
        "manufacturer": "Volkswagen Group Rus / local assembly",
        "market": "Russia/CIS",
        "country": "Russia",
        "vehicle_type": "Passenger car",
    },
    "MMC": {
        "make": "Mitsubishi",
        "manufacturer": "Mitsubishi Motors",
        "market": "Asia/ROW",
        "country": "Thailand/Japan-market dependent",
        "vehicle_type": "Pickup/SUV",
    },
    "LSC": {
        "make": "Changan",
        "manufacturer": "Changan Automobile",
        "market": "China/ROW",
        "country": "China",
        "vehicle_type": "Passenger/pickup",
    },
}


@dataclass(frozen=True)
class PlatformRule:
    rule_id: str
    pattern: str
    kind: str
    fields: dict[str, Any]
    evidence: str
    confidence: float
    notes: str = ""

    def matches(self, identifier: str) -> bool:
        return re.match(self.pattern, identifier, flags=re.IGNORECASE) is not None


PLATFORM_RULES: tuple[PlatformRule, ...] = (
    PlatformRule(
        "mercedes_wdd212",
        r"^WDD212",
        "vin_prefix",
        {"make": "Mercedes-Benz", "platform": "W212 E-Class", "model_family": "E-Class"},
        "VIN WMI WDD plus Mercedes 212 platform prefix.",
        0.72,
    ),
    PlatformRule(
        "vw_russia_polo_61",
        r"^XW8ZZZ61",
        "vin_prefix",
        {"make": "Volkswagen", "model_family": "Polo / Polo Sedan", "market": "Russia/CIS"},
        "XW8 local VW Group WMI plus 61 model family prefix used by Polo-class vehicles.",
        0.68,
    ),
    PlatformRule(
        "audi_a8_d4_4h",
        r"^WAUZZZ4H",
        "vin_prefix",
        {"make": "Audi", "model": "A8", "platform": "D4 / 4H", "market": "Europe/ROW"},
        "Audi WMI plus 4H model-platform prefix; exact engine/options need Audi EPC/ETKA.",
        0.8,
    ),
    PlatformRule(
        "vw_golf_mk7_au",
        r"^WVWZZZAU",
        "vin_prefix",
        {"make": "Volkswagen", "model_family": "Golf", "platform": "Mk7 / MQB AU", "market": "Europe/ROW"},
        "Volkswagen WMI plus AU Golf/MQB platform prefix; PR/options need ETKA/partslink24.",
        0.78,
    ),
    PlatformRule(
        "mercedes_gle_c292_wdc292",
        r"^WDC292",
        "vin_prefix",
        {
            "make": "Mercedes-Benz",
            "model_family": "GLE Coupe / GLE-Class",
            "platform": "C292/W292",
            "market": "Europe/ROW",
        },
        "Mercedes-Benz WDC WMI plus 292 GLE Coupe/GLE family platform prefix; exact options need Mercedes EPC.",
        0.78,
    ),
    PlatformRule(
        "nissan_pathfinder_r51_vskjvwr51",
        r"^VSKJVWR51",
        "vin_prefix",
        {"make": "Nissan", "model": "Pathfinder", "platform": "R51", "market": "Europe/ROW"},
        "Nissan Europe WMI plus R51 Pathfinder prefix; exact trim/options need Nissan EPC.",
        0.78,
    ),
    PlatformRule(
        "mazda_cx5_ke_jmzke",
        r"^JMZKE",
        "vin_prefix",
        {"make": "Mazda", "model": "CX-5", "platform": "KE", "market": "Europe/ROW"},
        "Mazda WMI plus KE CX-5 platform prefix; exact engine/options need Mazda EPC.",
        0.78,
    ),
    PlatformRule(
        "bmw_russia_g30_x4xjd19",
        r"^X4XJD19",
        "vin_prefix",
        {"make": "BMW", "model_family": "5 Series", "platform": "G30/G31 family", "market": "Russia/CIS"},
        "BMW local-assembly WMI plus CRM-observed 5-series prefix; exact variant/options need BMW ETK/AIR.",
        0.72,
    ),
    PlatformRule(
        "bmw_russia_e90_x4xva98",
        r"^X4XVA98",
        "vin_prefix",
        {"make": "BMW", "model_family": "3 Series", "platform": "E90/E91/E92 family", "market": "Russia/CIS"},
        "BMW local-assembly WMI plus CRM-observed 3-series prefix; exact variant/options need BMW ETK/AIR.",
        0.72,
    ),
    PlatformRule(
        "jeep_wk2_overland_5_7",
        r"^1C4RJFCT",
        "vin_prefix",
        {
            "make": "Jeep",
            "model": "Grand Cherokee",
            "platform": "WK2",
            "trim": "Overland",
            "engine": "5.7 V8 gasoline",
            "drivetrain": "4WD",
        },
        "North-American VIN prefix and vPIC-clean pattern for WK2 Grand Cherokee Overland 5.7.",
        0.9,
    ),
    PlatformRule(
        "suzuki_hustler_mr41s",
        r"^MR41S[-]?\d{5,7}$",
        "jdm_frame",
        {"make": "Suzuki", "model": "Hustler", "platform": "MR41S", "engine": "R06A 0.66L kei", "market": "Japan"},
        "Japanese frame/model code MR41S; requires Suzuki EPC for production/options.",
        0.76,
    ),
    PlatformRule(
        "honda_crv_rd5",
        r"^JHLRD5",
        "vin_prefix",
        {"make": "Honda", "model_family": "CR-V", "platform": "RD5/RD-series", "market": "Japan/global"},
        "Honda JHL WMI plus RD5 CR-V platform prefix.",
        0.74,
    ),
    PlatformRule(
        "honda_civic_es1_frame",
        r"^ES1[-]?\d{6,7}$",
        "jdm_frame",
        {"make": "Honda", "model": "Civic", "platform": "ES1", "market": "Japan/ROW"},
        "Honda ES1 frame/body-number pattern; exact production and options need Honda/Japan EPC.",
        0.84,
    ),
    PlatformRule(
        "mitsubishi_l200_mmcjjjkl",
        r"^MMCJJJKL",
        "vin_prefix",
        {
            "make": "Mitsubishi",
            "model_family": "L200 / Triton",
            "engine": "4N15 2.4 diesel likely when CRM confirms",
            "market": "Asia/ROW",
        },
        "Mitsubishi MMC WMI plus L200/Triton-style prefix; exact trim needs Mitsubishi EPC.",
        0.7,
    ),
    PlatformRule(
        "changan_hunter_lscbbz2a",
        r"^LSCBBZ2A",
        "vin_prefix",
        {"make": "Changan", "model_family": "Hunter Plus / SC10", "market": "China/ROW"},
        "Changan LSC WMI plus CRM-matching Hunter/SC10 prefix.",
        0.68,
    ),
    PlatformRule(
        "skoda_rapid_russia_xw8ac2nh",
        r"^XW8AC2NH",
        "vin_prefix",
        {"make": "Skoda", "model": "Rapid", "market": "Russia/CIS"},
        "XW8 local VW Group WMI plus Skoda Rapid-style prefix.",
        0.68,
    ),
    PlatformRule(
        "toyota_prado_150_jtebu3fj",
        r"^JTEBU3FJ",
        "vin_prefix",
        {
            "make": "Toyota",
            "model": "Land Cruiser Prado 150",
            "engine": "1GR-FE 4.0 V6 gasoline",
            "drivetrain": "4WD",
            "market": "Japan/ROW",
        },
        "Toyota JTE WMI plus Prado 150 1GR-FE prefix; exact production/options need Toyota EPC.",
        0.82,
    ),
    PlatformRule(
        "toyota_prado_120_jtebu29j",
        r"^JTEBU29J",
        "vin_prefix",
        {
            "make": "Toyota",
            "model": "Land Cruiser Prado 120",
            "engine": "1GR-FE 4.0 V6 gasoline",
            "drivetrain": "4WD",
            "market": "Japan/ROW",
        },
        "Toyota JTE WMI plus Prado 120 1GR-FE prefix; exact production/options need Toyota EPC.",
        0.82,
    ),
)
