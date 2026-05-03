#!/usr/bin/env python3
"""
Download legally available open automotive datasets.

This script intentionally downloads only open/public datasets and API examples.
It does not scrape paid OEM portals, books, standards, or professional databases.

Run:
    python download_open_datasets.py --out ./open_automotive_data

Some sites may block automated requests or require updated URLs/User-Agent.
Review each source's current terms before production ingestion.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List

USER_AGENT = "AutomotiveRepairKnowledgePack/1.0 (+internal shop data ingestion)"

DATASETS = [
    {
        "id": "nhtsa_safercar_csv",
        "name": "NHTSA Safercar CSV",
        "url": "https://static.nhtsa.gov/nhtsa/downloads/Safercar/Safercar_data.csv",
        "kind": "csv",
        "notes": "Open CSV containing Safercar.gov data.",
    },
    {
        "id": "nhtsa_recalls_post_2010",
        "name": "NHTSA Recalls Flat File POST 2010",
        "url": "https://static.nhtsa.gov/odi/ffdd/rcl/FLAT_RCL_POST_2010.zip",
        "kind": "zip",
        "notes": "NHTSA recall flat file.",
    },
    {
        "id": "nhtsa_complaints_all",
        "name": "NHTSA Complaints Flat File",
        "url": "https://static.nhtsa.gov/odi/ffdd/cmpl/FLAT_CMPL.zip",
        "kind": "zip",
        "notes": "NHTSA complaints flat file.",
    },
    {
        "id": "nhtsa_tsbs_flat",
        "name": "NHTSA TSB Flat File",
        "url": "http://www-odi.nhtsa.dot.gov/downloads/folders/TSBS/FLAT_TSBS.zip",
        "kind": "zip",
        "notes": "NHTSA Technical Service Bulletin flat file.",
    },
    {
        "id": "nhtsa_tsbs_2025_2026",
        "name": "NHTSA TSB Received 2025-2026",
        "url": "https://static.nhtsa.gov/odi/ffdd/tsbs/TSBS_RECEIVED_2025-2026.zip",
        "kind": "zip",
        "notes": "Recent NHTSA TSB/manufacturer communications archive.",
    },
    {
        "id": "epa_fueleconomy_vehicles",
        "name": "EPA FuelEconomy.gov Vehicle CSV",
        "url": "https://fueleconomy.gov/feg/epadata/vehicles.csv.zip",
        "kind": "zip",
        "notes": "EPA vehicle fuel economy and attributes.",
    },
    {
        "id": "uk_dvsa_recalls_csv",
        "name": "UK DVSA Vehicle Recalls CSV",
        "url": "https://www.check-vehicle-recalls.service.gov.uk/documents/RecallsFile.csv",
        "kind": "csv",
        "notes": "United Kingdom recall file; may require browser-compatible access headers.",
    },
]

VPIC_ENDPOINTS = [
    {
        "id": "vpic_decodevin_example",
        "url": "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/1HGCM82633A004352?format=json",
        "notes": "Example VIN decode endpoint. Replace sample VIN in production.",
    },
    {
        "id": "vpic_getallmakes",
        "url": "https://vpic.nhtsa.dot.gov/api/vehicles/getallmakes?format=json",
        "notes": "All makes endpoint.",
    },
]


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})


def download(url: str, destination: Path, retries: int = 2) -> bool:
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(_request(url), timeout=60) as response:
                destination.write_bytes(response.read())
            return True
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt >= retries:
                print(f"FAILED: {url} -> {destination} ({exc})", file=sys.stderr)
                return False
            time.sleep(2 * (attempt + 1))
    return False


def unzip(zip_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out_dir)


def write_manifest(out_dir: Path, rows: List[Dict[str, str]]) -> None:
    manifest = {
        "generated_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "dataset_count": len(rows),
        "datasets": rows,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="./open_automotive_data", help="Output directory")
    parser.add_argument("--no-unzip", action="store_true", help="Do not unzip downloaded ZIP files")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_rows: List[Dict[str, str]] = []

    for ds in DATASETS:
        suffix = ".zip" if ds["kind"] == "zip" else ".csv"
        target = out_dir / "downloads" / f"{ds['id']}{suffix}"
        ok = download(ds["url"], target)
        row = {
            "id": ds["id"],
            "name": ds["name"],
            "url": ds["url"],
            "kind": ds["kind"],
            "downloaded": str(ok),
            "local_path": str(target if ok else ""),
            "notes": ds["notes"],
        }
        manifest_rows.append(row)
        if ok and ds["kind"] == "zip" and not args.no_unzip:
            extract_dir = out_dir / "extracted" / ds["id"]
            try:
                unzip(target, extract_dir)
                row["extracted_path"] = str(extract_dir)
            except zipfile.BadZipFile as exc:
                row["extracted_path"] = ""
                row["notes"] += f" | unzip failed: {exc}"

    for endpoint in VPIC_ENDPOINTS:
        target = out_dir / "api_examples" / f"{endpoint['id']}.json"
        ok = download(endpoint["url"], target)
        manifest_rows.append({
            "id": endpoint["id"],
            "name": endpoint["id"],
            "url": endpoint["url"],
            "kind": "api_json_example",
            "downloaded": str(ok),
            "local_path": str(target if ok else ""),
            "notes": endpoint["notes"],
        })

    write_manifest(out_dir, manifest_rows)
    print(f"Wrote manifest: {out_dir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
