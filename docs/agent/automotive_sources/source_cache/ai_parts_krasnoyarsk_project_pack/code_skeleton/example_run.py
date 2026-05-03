"""Пример запуска offline MVP на синтетических данных.

Запуск из папки code_skeleton:
python example_run.py
"""

from __future__ import annotations

import json
from pathlib import Path

from models import Offer, PartNeed, PartRequest, SearchConstraints, Vehicle
from scoring import rank_offers
from reporting import build_manager_report


def load_first_request(path: Path) -> PartRequest:
    raw = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    return PartRequest(
        request_id=raw["request_id"],
        vehicle=Vehicle(**raw["vehicle"]),
        part=PartNeed(**raw["part"]),
        constraints=SearchConstraints(**raw["constraints"]),
        missing_fields=raw.get("missing_fields", []),
        risk_flags=raw.get("risk_flags", []),
    )


def load_offers(path: Path) -> list[Offer]:
    offers = []
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = json.loads(line)
        raw.pop("scores", None)
        offers.append(Offer(**raw))
    return offers


if __name__ == "__main__":
    data_dir = Path(__file__).resolve().parents[1] / "data"
    req = load_first_request(data_dir / "sample_part_requests.jsonl")
    offers = load_offers(data_dir / "sample_offers_synthetic.jsonl")[:3]
    ranked = rank_offers(offers, mode="urgent_today")
    print(build_manager_report(req, ranked))
