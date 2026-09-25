#!/usr/bin/env python3
"""Install the pinned public catalog release into the private Manager cache.

The normal coordinated deploy calls this before entering maintenance. A complete
cache is verified locally without contacting GitHub. The ZIP is needed only for
the first import or when entries are absent from an existing cache.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.request import Request, urlopen

from scripts.import_offline_parts_catalogs import _slug, import_catalogs
from scripts.ocr_offline_parts_catalogs import ocr_catalogs


RELEASE_URL = (
    "https://github.com/UgaChavis/AutostopManager/releases/download/"
    "catalogs-2026-09-25/autostop-parts-catalogs-20260925-public.zip"
)
RELEASE_SHA256 = "b2b1e40510e91579a5e8e4be90e0d4a2b461122b6a73af27ec4600d4a1ff7ffe"
RELEASE_BYTES = 373_251_668
# The manifest is deliberately small and public. These checksums let an already
# installed cache prove it has every released original without downloading 356 MiB.
RELEASE_CATALOGS = {
    "DENSO-Heavy-Duty-CV-Catalogue-2024.pdf": "c1549b9f1ccde7e2b8ff9d1d467505cd9fac556b628c21ab129d938b15c57e21",
    "DENSO-Commercial-Heavy-Duty-Master-Data-2023-6.xlsx": "45cce075c1cfae68bd05457c5b97d34d5ec06661cfd7da600052e87eccd6856e",
    "DENSO-Spark-and-Glow-Plug-Applications.pdf": "0a25dd09d3949e9783f609aab78ba0fc1901e318e2384d61ed5289e9123da231",
    "Continental-Battery-Catalogue-2025.pdf": "df2960ce17715c81c241dee7bc07ee934e50cd52fed8703e7fa580ac71847951",
    "NISMO-Parts-Catalogue-2023.pdf": "c7aa926a270885ca4526a274f8ef4e4b32258e3f19e6ecfe6786edc2fa105fa1",
    "ZF-Reman-Transmission-Catalogue-2026.pdf": "3223671f113e5d962205695e85ef0ca09cbdd90f66bfe8f5b795833221569a90",
    "HELLA-Product-Overview-2026.pdf": "5bfd1c1607fbb24ce2d9c5726c5b1ed1c0b793afd413064c1478e43518e452f4",
    "HELLA-Heavy-Duty-Catalogue-2026.pdf": "ef417a3d85a8250cd57e97de4c02b0795162bb54f6cfc9f39d03ad16b5472ef0",
    "HELLA-Starters-Alternators-Buyers-Guide.xlsx": "7de05441ce1964ed83c7c11cb38f6c44bb87068b38612ed1fa773de8a841892b",
    "HELLA-Brake-Caliper-Buyers-Guide-2025.xlsx": "eb7247d392b0f8693b34bfa9888c58c83d8f975807a338d30a166257fe767291",
    "HELLA-New-Products-August-2026.xlsx": "2ecdf74d146a9a8060708e80faa029f11a5f8372839f43bb35cc7c2389d407b2",
    "GMB-Japan-Water-Pump-Catalogue-2026.pdf": "d44a0471776bbfb77e60732b253558a2bf07c5a70f2f737814cae988f728d4db",
    "KYB-RHD-Shock-Absorbers-2026.pdf": "f772c561f168419eee2dd4cf08ba2351b3b3dbe659509beca5c7379e2fc824ba",
    "ATE-Clutch-Parts-2026.pdf": "cf13802aee5286c2d4c44d7efd83b182a54ee90a118ae7e48d5ef5becc1079fc",
    "ATE-Drum-Brakes-2026.pdf": "e8c1b5039ea533b474a1433fe772ef1adda3b2b8c8f31a82f8e741e50f5a059b",
    "HKS-Japan-Muffler-Lineup-2025-09.pdf": "08486fef6bd37f7a9804ccc3b9e5e1304525690fda5caace11892cbf62452aa8",
    "NGK-NTK-Spark-Glow-2026-2027.pdf": "e87677044bf74679e058dbb8fea95aa4602e7a843e30ecaaa442c328625b2d0a",
    "MAHLE-Turbochargers-2025.pdf": "7b8341790586d1ec389e68df92db7cae6f6efbacd55b34a92a09e5dca94b30a2",
    "MAHLE-Bearings-2025.pdf": "35a9441e17514c07c17aa9c189fc72d27a169c1d9e76c54b9656dd79890916f7",
    "MAHLE-Piston-Rings-2025.pdf": "62e2e6bb43c799bdeca684b77652a5d26a79b837569358d80667beffa43553ab",
    "MAHLE-Valve-Train-Components-2025.pdf": "d9be89be7a5bb369f7bcf7d20477661f4e93355fc24c362734403c91b346e2c9",
    "NTK-Throttle-Bodies-2026-2027.pdf": "02290a7975663d64c8131c00d4f5386fe43440eda366cd84b074868e292d011f",
    "NTK-MAF-MAP-Sensors-2026-05.pdf": "346354d2cafab3298d7fa181fdec9f68554265f9047fb15e0ed1720722a97704",
    "NTK-Camshaft-Crankshaft-Sensors-2026-2027.pdf": "f2b6adde40af1c839c0b08cfb815546516ac73f7661b9eaed8385ff324065fd1",
    "NTK-Exhaust-Pressure-Sensors-2026-03.pdf": "607733a3adbcf5168e40f657b279cca80c2c32277db9688b6b3ddd7f46fbc425",
}
SMOKE_CATALOG_ID = "denso_heavy_duty_cv_catalogue_2024"
SMOKE_QUERY = "DENSO"
MAX_INDEX_BYTES = 2 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _root(argument: Path) -> Path:
    if not argument.is_absolute() or ".." in argument.parts or argument.is_symlink():
        raise ValueError("cache root must be an absolute, non-linked path without '..'")
    parent = argument.parent.resolve(strict=True)
    if not parent.is_dir():
        raise ValueError("cache parent is not a directory")
    root = parent / argument.name
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise ValueError("cache root is linked or not a directory")
    return root


def _index(root: Path) -> dict[str, object] | None:
    path = root / "catalog_index.json"
    if path.is_symlink():
        raise ValueError("cache index is linked")
    if not path.exists():
        if root.exists() and any(root.iterdir()):
            raise ValueError("nonempty cache has no index; refusing to replace its history")
        return None
    if not path.is_file() or path.stat().st_size > MAX_INDEX_BYTES:
        raise ValueError("cache index missing, linked or too large")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("cache index is unreadable") from exc
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or payload.get("catalog_count") != len(entries):
        raise ValueError("invalid cache index or catalog count")
    if any(not isinstance(item, dict) for item in entries):
        raise ValueError("invalid cache index entry")
    return payload


def _require(programs: tuple[str, ...]) -> None:
    missing = [program for program in programs if not shutil.which(program)]
    if missing:
        raise RuntimeError("required programs missing: " + ", ".join(missing))


def _require_ocr() -> None:
    _require(("pdftoppm", "tesseract"))
    try:
        result = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Tesseract language check timed out") from exc
    languages = set(result.stdout.split()) | set(result.stderr.split())
    if result.returncode or not {"eng", "rus"}.issubset(languages):
        raise RuntimeError("Tesseract OCR languages eng and rus are required")


def _safe_catalog_file(root: Path, kind: str, name: str) -> Path:
    folder = root / kind
    path = folder / name
    if folder.is_symlink() or path.is_symlink() or not path.is_file():
        raise ValueError(f"catalog {kind} file missing or linked: {name}")
    return path


def _validate_release_entry(root: Path, name: str, expected_hash: str, entry: dict[str, object]) -> tuple[int, bool]:
    catalog_id = _slug(name)
    kind = Path(name).suffix.lower().lstrip(".")
    text_suffix = "txt" if kind == "pdf" else "tsv"
    expected_original = f"data/offline_parts_catalogs/{kind}/{name}"
    expected_text = f"data/offline_parts_catalogs/text/{catalog_id}.{text_suffix}"
    if (
        entry.get("catalog_id") != catalog_id
        or entry.get("sha256") != expected_hash
        or entry.get("format") != kind
        or entry.get("local_file") != expected_original
        or entry.get("text_path") != expected_text
    ):
        raise ValueError(f"released catalog index differs: {catalog_id}")
    original = _safe_catalog_file(root, kind, name)
    text_file = _safe_catalog_file(root, "text", f"{catalog_id}.{text_suffix}")
    if _sha256(original) != expected_hash:
        raise ValueError(f"released catalog checksum differs: {catalog_id}")
    if not text_file.stat().st_size or text_file.stat().st_size != entry.get("text_bytes"):
        raise ValueError(f"released catalog search text missing or changed: {catalog_id}")
    status = entry.get("text_extract_status")
    needs_ocr = False
    if kind == "pdf":
        pages = entry.get("pages")
        text_pages = entry.get("text_pages")
        empty_pages = entry.get("empty_pages")
        if not isinstance(pages, int) or pages <= 0 or not isinstance(empty_pages, list):
            raise ValueError(f"PDF page metadata invalid: {catalog_id}")
        if not isinstance(text_pages, int) or text_pages + len(empty_pages) != pages:
            raise ValueError(f"PDF page counts differ: {catalog_id}")
        if status == "partial_needs_ocr" and empty_pages:
            needs_ocr = True
        elif status not in {"ok", "ok_with_unverified_ocr"} or empty_pages:
            raise ValueError(f"PDF extraction metadata differs: {catalog_id}")
    elif status != "ok":
        raise ValueError(f"XLSX search text incomplete: {catalog_id}")
    if kind == "xlsx" and not isinstance(entry.get("populated_rows"), int):
        raise ValueError(f"XLSX extraction metadata missing: {catalog_id}")
    recorded_ocr_pages = entry.get("ocr_page_count", 0)
    if type(recorded_ocr_pages) is not int or recorded_ocr_pages < 0:
        raise ValueError(f"OCR page metadata invalid: {catalog_id}")
    return recorded_ocr_pages, needs_ocr


def _readiness(root: Path) -> tuple[bool, int, str]:
    """Check released originals, derived text and one call through actual search."""
    index = _index(root)
    if index is None:
        return False, 0, "cache index absent"
    entries = index["entries"]
    assert isinstance(entries, list)
    by_name: dict[str, dict[str, object]] = {}
    for entry in entries:
        assert isinstance(entry, dict)
        name = entry.get("filename")
        if isinstance(name, str) and name in RELEASE_CATALOGS:
            if name in by_name:
                raise ValueError(f"duplicate catalog in cache index: {name}")
            by_name[name] = entry
    ocr_pages = 0
    needs_ocr = False
    for name, expected_hash in RELEASE_CATALOGS.items():
        entry = by_name.get(name)
        if entry is None:
            continue
        added_ocr_pages, entry_needs_ocr = _validate_release_entry(root, name, expected_hash, entry)
        ocr_pages += added_ocr_pages
        needs_ocr |= entry_needs_ocr
    if len(by_name) != len(RELEASE_CATALOGS):
        return False, len(entries), f"released catalogs present: {len(by_name)}/{len(RELEASE_CATALOGS)}"
    if needs_ocr:
        return False, len(entries), "ocr_needed"
    _require(("/usr/bin/rg",))
    # The explicit root prevents config defaults, .env files or DB paths from
    # influencing this read-only smoke check.
    from autostop_manager.offline_catalogs import search_offline_parts_catalogs

    previous = os.environ.get("AUTOSTOP_OFFLINE_CATALOG_ROOT")
    previous_path = os.environ.get("PATH")
    os.environ["AUTOSTOP_OFFLINE_CATALOG_ROOT"] = str(root)
    os.environ["PATH"] = "/usr/bin:/bin"
    try:
        smoke = search_offline_parts_catalogs(SMOKE_QUERY, catalog_id=SMOKE_CATALOG_ID, limit=1)
    finally:
        if previous is None:
            os.environ.pop("AUTOSTOP_OFFLINE_CATALOG_ROOT", None)
        else:
            os.environ["AUTOSTOP_OFFLINE_CATALOG_ROOT"] = previous
        if previous_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous_path
    if not smoke.get("ok") or smoke.get("scanned_catalogs") != 1 or not smoke.get("results"):
        raise RuntimeError("catalog search smoke failed")
    locator = smoke["results"][0].get("locator", {})
    if not isinstance(locator.get("page"), int) or locator["page"] < 1:
        raise RuntimeError("catalog search page locator missing")
    return True, len(entries), str(ocr_pages)


def _download(destination: Path) -> None:
    digest = hashlib.sha256()
    size = 0
    request = Request(RELEASE_URL, headers={"User-Agent": "AutostopManager-catalog-sync/1"})
    with urlopen(request, timeout=60) as response, destination.open("wb") as output:
        for block in iter(lambda: response.read(4 * 1024 * 1024), b""):
            size += len(block)
            if size > RELEASE_BYTES:
                raise ValueError("catalog release exceeds pinned byte count")
            digest.update(block)
            output.write(block)
    if size != RELEASE_BYTES or digest.hexdigest() != RELEASE_SHA256:
        raise ValueError("catalog release ZIP size or SHA-256 differs from pinned asset")


def _bootstrap(root: Path, stage: Path) -> None:
    if _index(root) is not None:
        return
    root.mkdir(mode=0o700, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("cache changed before bootstrap")
    initial = stage / "catalog_index.json"
    initial.write_text('{"catalog_count":0,"entries":[]}\n', encoding="utf-8")
    initial.chmod(0o600)
    os.replace(initial, root / "catalog_index.json")


@contextmanager
def _lock(root: Path) -> Iterator[None]:
    path = root.parent / ".offline-parts-catalog-sync.lock"
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another catalog sync is already running") from exc
        yield
    finally:
        os.close(descriptor)


def sync(cache_root: Path, *, verify_only: bool = False) -> dict[str, object]:
    root = _root(cache_root)
    if verify_only:
        ready, count, detail = _readiness(root)
        if not ready:
            raise RuntimeError(detail)
        return {
            "status": "ready",
            "release_catalogs": len(RELEASE_CATALOGS),
            "cache_catalogs": count,
            "ocr_pages": int(detail),
        }
    with _lock(root):
        _require(("/usr/bin/rg",))
        ready, count, detail = _readiness(root)
        if ready:
            return {
                "status": "already_ready",
                "release_catalogs": len(RELEASE_CATALOGS),
                "cache_catalogs": count,
                "ocr_pages": int(detail),
            }
        if detail == "ocr_needed":
            _require_ocr()
            ocr_catalogs(root, verify_only=False)
            ready, count, detail = _readiness(root)
            if not ready:
                raise RuntimeError("catalog OCR incomplete: " + detail)
            return {
                "status": "ocr_completed",
                "release_catalogs": len(RELEASE_CATALOGS),
                "cache_catalogs": count,
                "ocr_pages": int(detail),
            }
        _require(("pdfinfo", "pdftotext"))
        with tempfile.TemporaryDirectory(prefix=".offline-catalog-release-", dir=root.parent) as temporary:
            stage = Path(temporary)
            archive = stage / "catalogs.zip"
            _download(archive)
            _bootstrap(root, stage)
            imported = import_catalogs(archive, root, verify_only=False)
            pending = ocr_catalogs(root, verify_only=True)
            if pending["empty_pages_before"]:
                _require_ocr()
                ocr_catalogs(root, verify_only=False)
        ready, count, detail = _readiness(root)
        if not ready:
            raise RuntimeError("catalog import incomplete: " + detail)
        return {
            "status": "imported",
            "release_catalogs": len(RELEASE_CATALOGS),
            "added_catalogs": imported["added_catalogs"],
            "cache_catalogs": count,
            "ocr_pages": int(detail),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", required=True, type=Path, help="private offline_parts_catalogs directory")
    parser.add_argument("--verify-only", action="store_true", help="check existing cache without a download or write")
    args = parser.parse_args()
    try:
        result = sync(args.cache_root, verify_only=args.verify_only)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
