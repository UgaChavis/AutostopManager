#!/usr/bin/env python3
"""Isolated pinned vininfo worker. Input/output are transient JSON on pipes."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import sys


def denied(*_args, **_kwargs):
    raise RuntimeError("offline_network_denied")


socket.socket = denied
socket.create_connection = denied
socket.getaddrinfo = denied
sys.path.insert(0, str(Path(sys.argv[1]) / "python-packages"))

from vininfo import Vin  # noqa: E402

payload = json.load(sys.stdin)
vin = Vin(payload["identifier"])
profile = {
    key: value
    for key, value in {"manufacturer": vin.manufacturer, "country": vin.country, "region": vin.region}.items()
    if value and value != "UnsupportedBrand"
}
details = {}
if vin.details:
    for key in ("body", "engine", "model", "plant", "transmission"):
        item = getattr(vin.details, key)
        if item.code or item.name:
            details[key] = {"code": item.code, "name": item.name}
        if item.name:
            profile[key] = item.name
years = vin.years
if len(years) == 1:
    profile["model_year"] = years[0]
print(
    json.dumps(
        {
            "vehicle_profile": profile,
            "model_year_candidates": years,
            "brand_details": details,
            "checksum": {"matches": vin.verify_checksum(), "mandatory_for_all_markets": False},
            "network_calls": 0,
        }
    )
)
