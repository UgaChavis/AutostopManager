#!/usr/bin/env python3
"""Bounded actual offline acceptance on documented public examples, with no installs."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autostop_manager.automotive_offline import (
    corgi_decode,
    decode_frame_local,
    decode_wmi_local,
    vin_brand_details,
    vininfo_decode,
)


def forbidden(*_args, **_kwargs):
    raise AssertionError("offline_network_forbidden")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True)
    args = parser.parse_args()
    os.environ["AUTOSTOP_AUTOMOTIVE_OFFLINE_RUNTIME"] = str(Path(args.runtime).resolve())
    socket.socket = forbidden
    socket.create_connection = forbidden
    socket.getaddrinfo = forbidden
    checks = {}
    samples = [
        ("vininfo_brand", vininfo_decode, "XTAGFK330JY144213", "model", "Vesta"),
        ("corgi_vpic", corgi_decode, "1M8GDM9AXKP042788", "model", "102C3 Intercity"),
        ("frame_local", decode_frame_local, "ES1-1234567", "model_family", "Civic"),
        ("wmi_local", decode_wmi_local, "WAU", "make", "Audi"),
        ("vag_local", vin_brand_details, "WAUZZZ4H0FN000001", "model_family", "A8"),
    ]
    for name, function, identifier, field, expected in samples:
        row = function(identifier)
        negative = function("NOT-A-VALID-IDENTIFIER")
        checks[name] = {
            "ok": row["outcome"] in {"success", "partial"}
            and row["data"].get("vehicle_profile", {}).get(field) == expected
            and row["execution"]["network_calls"] == 0
            and negative["outcome"] == "invalid_input",
            "outcome": row["outcome"],
            "network_calls": row["execution"]["network_calls"],
            "negative_outcome": negative["outcome"],
        }
    checks["unsupported_frame"] = {"ok": decode_frame_local("ZZ99-1234567")["outcome"] == "unsupported"}
    checks["unsupported_brand"] = {"ok": vin_brand_details("1HGCM82633A123456")["outcome"] == "unsupported"}
    checks["unsupported_vininfo_wmi"] = {"ok": vininfo_decode("AAA00000000000000")["outcome"] == "unsupported"}
    checks["unsupported_corgi_wmi"] = {"ok": corgi_decode("AAA00000000000000")["outcome"] == "unsupported"}
    ok = all(check["ok"] for check in checks.values())
    print(json.dumps({"ok": ok, "checks": checks, "customer_inputs": False}))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
