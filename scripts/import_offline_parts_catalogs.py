#!/usr/bin/env python3
"""Import the supplied manufacturer catalogs into the private offline cache.

The archive is data, including its Markdown files. Only catalogs/, catalog-index.csv,
and SHA256SUMS.csv are used. Originals and derived search text stay under the
gitignored cache root. The catalog index is replaced only after every new file
has been written and checked; an interrupted run can be repeated.
"""

from __future__ import annotations

import argparse
import csv
from datetime import UTC, datetime
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit, urlunsplit
import xml.etree.ElementTree as ET
import zipfile


MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
DOC_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
EXPECTED_CSV_FIELDS = {
    "file",
    "brand",
    "catalog",
    "edition",
    "product_scope",
    "market_scope",
    "source_url",
    "fitment_limit",
}
MAX_ARCHIVE_BYTES = 2_000_000_000
MAX_MEMBER_BYTES = 500_000_000
MAX_XLSX_EXPANDED_BYTES = 1_000_000_000
HEADER_ROWS = {
    "DENSO-Commercial-Heavy-Duty-Master-Data-2023-6.xlsx": [3, 3, 6],
    "HELLA-Brake-Caliper-Buyers-Guide-2025.xlsx": [1],
    "HELLA-New-Products-August-2026.xlsx": [9],
    "HELLA-Starters-Alternators-Buyers-Guide.xlsx": [1, 2],
}


def _sha256_stream(stream: io.BufferedIOBase) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def _safe_zip_member(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and not name.startswith("/") and "\\" not in name and ":" not in name and ".." not in path.parts


def _read_csv_member(archive: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    with archive.open(name) as raw, io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
        reader = csv.DictReader(text)
        if reader.fieldnames is None:
            raise ValueError(f"empty CSV: {name}")
        rows = list(reader)
        if any(None in row for row in rows):
            raise ValueError(f"malformed CSV: {name}")
        return rows


def verify_archive(archive: zipfile.ZipFile) -> list[dict[str, str]]:
    """Verify paths, CRC and SHA-256 before writing to the cache."""
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise ValueError("duplicate archive paths")
    if sum(info.file_size for info in infos) > MAX_ARCHIVE_BYTES:
        raise ValueError("archive exceeds unpacked size limit")
    for info in infos:
        if not _safe_zip_member(info.filename) or info.is_dir():
            raise ValueError(f"unsafe archive entry: {info.filename!r}")
        if info.file_size > MAX_MEMBER_BYTES or info.flag_bits & 1:
            raise ValueError(f"oversized or encrypted archive entry: {info.filename!r}")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError(f"archive symlink: {info.filename!r}")
    if "catalog-index.csv" not in names or "SHA256SUMS.csv" not in names:
        raise ValueError("archive manifest or catalog index missing")

    checksum_rows = _read_csv_member(archive, "SHA256SUMS.csv")
    if not checksum_rows or set(checksum_rows[0]) != {"Path", "SHA256"}:
        raise ValueError("unexpected SHA256SUMS.csv columns")
    checksums = {row["Path"]: row["SHA256"].lower() for row in checksum_rows}
    if len(checksums) != len(checksum_rows) or set(checksums) != set(names) - {"SHA256SUMS.csv"}:
        raise ValueError("SHA256SUMS.csv does not cover archive entries exactly")
    for name in names:
        if name == "SHA256SUMS.csv":
            continue
        with archive.open(name) as stream:  # ZIP CRC is checked at EOF.
            actual = _sha256_stream(stream)
        if actual != checksums[name]:
            raise ValueError(f"SHA-256 mismatch: {name}")

    rows = _read_csv_member(archive, "catalog-index.csv")
    if not rows or set(rows[0]) != EXPECTED_CSV_FIELDS:
        raise ValueError("unexpected catalog-index.csv columns")
    catalog_names = {name.removeprefix("catalogs/") for name in names if name.startswith("catalogs/")}
    indexed_names = [row["file"] for row in rows]
    if len(indexed_names) != len(set(indexed_names)) or set(indexed_names) != catalog_names:
        raise ValueError("catalog-index.csv and catalogs/ have different files")
    for row in rows:
        name = row["file"]
        if (
            not _safe_zip_member(name)
            or PurePosixPath(name).name != name
            or Path(name).suffix.lower() not in {".pdf", ".xlsx"}
        ):
            raise ValueError(f"unsafe or unsupported catalog filename: {name!r}")
        row["sha256"] = checksums[f"catalogs/{name}"]
    return rows


def _slug(filename: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", Path(filename).stem.lower()).strip("_")
    if not slug:
        raise ValueError(f"no catalog id for {filename!r}")
    return slug


def _sanitize_url(url: str) -> tuple[str, bool]:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("catalog source_url must be HTTP(S)")
    # Query strings may contain bearer-like public download tokens. Keep only
    # the source page/path as attribution; never persist query or fragment.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")), bool(parsed.query or parsed.fragment)


def _relative(cache_root: Path, path: Path) -> str:
    return str(Path("data/offline_parts_catalogs") / path.relative_to(cache_root))


def _pdf_pages(path: Path) -> int:
    proc = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, timeout=120, check=False)
    if proc.returncode:
        raise RuntimeError(f"pdfinfo failed for {path.name}: {proc.stderr[:300]}")
    match = re.search(r"^Pages:\s*(\d+)\s*$", proc.stdout, re.MULTILINE)
    if not match:
        raise RuntimeError(f"pdfinfo omitted page count for {path.name}")
    return int(match.group(1))


def _extract_pdf(original: Path, target: Path) -> dict[str, object]:
    pages = _pdf_pages(original)
    raw_path = target.with_suffix(".pdftotext.tmp")
    try:
        with raw_path.open("wb") as raw:
            proc = subprocess.run(
                ["pdftotext", "-layout", "-enc", "UTF-8", str(original), "-"],
                stdout=raw,
                stderr=subprocess.PIPE,
                timeout=1800,
                check=False,
            )
        if proc.returncode:
            raise RuntimeError(f"pdftotext failed for {original.name}: {proc.stderr.decode('utf-8', 'replace')[:300]}")
        empty_pages: list[int] = []
        sparse_pages: list[int] = []
        page_number = 0

        with target.open("w", encoding="utf-8", newline="\n") as out, raw_path.open("rb") as raw:
            pending = b""

            def write_page(payload: bytes) -> None:
                nonlocal page_number
                page_number += 1
                content = payload.decode("utf-8", "replace").strip()
                chars = sum(character.isalnum() for character in content)
                if chars == 0:
                    empty_pages.append(page_number)
                elif chars < 20:
                    sparse_pages.append(page_number)
                out.write(f"\f[PAGE {page_number}]\n{content}\n")

            for block in iter(lambda: raw.read(1024 * 1024), b""):
                pending += block
                while b"\f" in pending:
                    page, pending = pending.split(b"\f", 1)
                    write_page(page)
            if pending.strip():
                write_page(pending)
            if page_number > pages:
                raise RuntimeError(f"pdftotext page count exceeds pdfinfo for {original.name}")
            while page_number < pages:
                write_page(b"")

        return {
            "pages": pages,
            "text_pages": pages - len(empty_pages),
            "empty_pages": empty_pages,
            "sparse_pages": sparse_pages,
            "text_extract_status": "ok" if not empty_pages else "partial_needs_ocr",
            "text_bytes": target.stat().st_size,
        }
    finally:
        raw_path.unlink(missing_ok=True)


def _xlsx_shared_strings(book: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in book.namelist():
        return []
    with book.open("xl/sharedStrings.xml") as stream:
        root = ET.parse(stream).getroot()
    return ["".join(node.text or "" for node in item.iter(MAIN_NS + "t")) for item in root.findall(MAIN_NS + "si")]


def _xlsx_sheet_paths(book: zipfile.ZipFile) -> list[tuple[str, str]]:
    with book.open("xl/workbook.xml") as stream:
        workbook = ET.parse(stream).getroot()
    with book.open("xl/_rels/workbook.xml.rels") as stream:
        relationships = ET.parse(stream).getroot()
    targets = {item.get("Id"): item.get("Target") for item in relationships.findall(PKG_REL_NS + "Relationship")}
    sheets: list[tuple[str, str]] = []
    for sheet in workbook.findall("./" + MAIN_NS + "sheets/" + MAIN_NS + "sheet"):
        name = sheet.get("name", "")
        target = targets.get(sheet.get(DOC_REL_NS + "id"))
        if not name or not target or not _safe_zip_member(target.lstrip("/")):
            raise ValueError("invalid XLSX sheet relationship")
        member = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
        if not member.startswith("xl/worksheets/") or member not in book.namelist():
            raise ValueError("XLSX sheet target missing or outside worksheets")
        sheets.append((name, member))
    return sheets


def _xlsx_zero_formats(book: zipfile.ZipFile) -> dict[int, int]:
    if "xl/styles.xml" not in book.namelist():
        return {}
    with book.open("xl/styles.xml") as stream:
        styles = ET.parse(stream).getroot()
    custom = {
        int(item.get("numFmtId", "0")): item.get("formatCode", "")
        for item in styles.findall("./" + MAIN_NS + "numFmts/" + MAIN_NS + "numFmt")
    }
    result: dict[int, int] = {}
    for index, item in enumerate(styles.findall("./" + MAIN_NS + "cellXfs/" + MAIN_NS + "xf")):
        code = custom.get(int(item.get("numFmtId", "0")), "")
        if re.fullmatch(r"0{2,}", code):
            result[index] = len(code)
    return result


def _column_number(ref: str) -> int:
    match = re.match(r"([A-Z]+)", ref)
    if not match:
        raise ValueError(f"invalid XLSX cell reference: {ref!r}")
    number = 0
    for character in match.group(1):
        number = number * 26 + ord(character) - ord("A") + 1
    return number


def _column_label(number: int) -> str:
    result = ""
    while number:
        number, rem = divmod(number - 1, 26)
        result = chr(65 + rem) + result
    return result


def _safe_tsv(value: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f\u2028\u2029]+", " ", value).strip()


def _xlsx_row_values(
    row: ET.Element,
    shared: list[str],
    zero_formats: dict[int, int],
    metrics: dict[str, int],
) -> dict[int, str]:
    values: dict[int, str] = {}
    for cell in row.findall(MAIN_NS + "c"):
        column = _column_number(cell.get("r", ""))
        kind = cell.get("t", "n")
        value_element = cell.find(MAIN_NS + "v")
        inline_element = cell.find(MAIN_NS + "is")
        formula = cell.find(MAIN_NS + "f")
        value = value_element.text or "" if value_element is not None else ""
        if formula is not None:
            metrics["formula_cells"] += 1
            if not value:
                metrics["formula_without_cache"] += 1
                continue  # Never evaluate formulas or external links.
        if kind == "s" and value:
            index = int(value)
            if index < 0 or index >= len(shared):
                raise ValueError("invalid XLSX shared string index")
            value = shared[index]
        elif kind == "inlineStr" and inline_element is not None:
            value = "".join(node.text or "" for node in inline_element.iter(MAIN_NS + "t"))
        elif kind == "n" and value:
            metrics["numeric_raw_cells"] += 1
            width = zero_formats.get(int(cell.get("s", "0")))
            if width and re.fullmatch(r"\d+", value):
                value = value.zfill(width)
                metrics["numeric_zero_padded_cells"] += 1
        value = _safe_tsv(value)
        if value:
            values[column] = value
    return values


def _extract_xlsx(original: Path, target: Path) -> dict[str, object]:
    with zipfile.ZipFile(original) as book:
        infos = book.infolist()
        if sum(item.file_size for item in infos) > MAX_XLSX_EXPANDED_BYTES or any(
            item.file_size > MAX_XLSX_EXPANDED_BYTES for item in infos
        ):
            raise ValueError(f"XLSX expanded size limit exceeded: {original.name}")
        if any(item.filename.lower().endswith(("vbaproject.bin", ".vbs", ".js")) for item in infos):
            raise ValueError(f"XLSX contains active content: {original.name}")
        shared = _xlsx_shared_strings(book)
        sheets = _xlsx_sheet_paths(book)
        zero_formats = _xlsx_zero_formats(book)
        expected_headers = HEADER_ROWS.get(original.name)
        if expected_headers is None or len(expected_headers) != len(sheets):
            raise ValueError(f"header rows not configured for XLSX: {original.name}")
        sheet_rows: dict[str, int] = {}
        populated_rows = 0
        metrics = {
            "formula_cells": 0,
            "formula_without_cache": 0,
            "numeric_raw_cells": 0,
            "numeric_zero_padded_cells": 0,
        }
        external_links = sum(name.startswith("xl/externalLinks/") for name in book.namelist())

        with target.open("w", encoding="utf-8", newline="\n") as out:
            for (sheet_name, member), header_row in zip(sheets, expected_headers, strict=True):
                headers: dict[int, str] = {}
                count = 0
                with book.open(member) as stream:
                    iterator = ET.iterparse(stream, events=("start", "end"))
                    sheet_data: ET.Element | None = None
                    for event, row in iterator:
                        if event == "start" and row.tag == MAIN_NS + "sheetData":
                            sheet_data = row
                        if event != "end" or row.tag != MAIN_NS + "row":
                            continue
                        row_number = int(row.get("r", "0"))
                        if row_number <= 0:
                            raise ValueError(f"invalid XLSX row number in {member}")
                        values = _xlsx_row_values(row, shared, zero_formats, metrics)
                        if row_number == header_row:
                            headers = values.copy()
                        fields = [_safe_tsv(sheet_name), str(row_number)]
                        for column, value in sorted(values.items()):
                            label = headers.get(column) if row_number > header_row else None
                            fields.append(f"{_safe_tsv(label or _column_label(column))}:{value}")
                        out.write("\t".join(fields) + "\n")
                        count += 1
                        if values:
                            populated_rows += 1
                        if sheet_data is None:
                            raise ValueError(f"XLSX sheetData missing in {member}")
                        sheet_data.clear()  # DENSO has over 1M rows; keep XML memory bounded.
                if not headers:
                    raise ValueError(f"configured XLSX header row is empty: {original.name}/{sheet_name}")
                sheet_rows[sheet_name] = count

    return {
        "sheets": len(sheets),
        "sheet_rows": sheet_rows,
        "rows": sum(sheet_rows.values()),
        "populated_rows": populated_rows,
        "formula_cells_skipped": metrics["formula_cells"],
        "formula_without_cached_value": metrics["formula_without_cache"],
        "external_link_entries_ignored": external_links,
        "numeric_raw_cells": metrics["numeric_raw_cells"],
        "numeric_zero_padded_cells": metrics["numeric_zero_padded_cells"],
        "numeric_value_note": "Numeric XML values are raw Excel values unless a simple all-zero number format was restored; other display formats are not guaranteed.",
        "text_extract_status": "ok" if not metrics["formula_without_cache"] else "partial_uncached_formulas",
        "text_bytes": target.stat().st_size,
    }


def _load_current_index(cache_root: Path) -> dict[str, object]:
    path = cache_root / "catalog_index.json"
    if not path.exists():
        raise FileNotFoundError(f"existing cache index missing: {path}")
    index = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(index, dict) or not isinstance(index.get("entries"), list):
        raise ValueError("invalid existing cache index")
    if index.get("catalog_count") != len(index["entries"]):
        raise ValueError("existing catalog_count is inconsistent")
    return index


def _chown_private(path: Path, owner: os.stat_result) -> None:
    os.chown(path, owner.st_uid, owner.st_gid)
    os.chmod(path, 0o700 if path.is_dir() else 0o600)


def import_catalogs(archive_path: Path, cache_root: Path, *, verify_only: bool) -> dict[str, object]:
    cache_root = cache_root.resolve(strict=True)
    existing = _load_current_index(cache_root)
    current_entries = existing["entries"]
    assert isinstance(current_entries, list)
    existing_by_id = {entry["catalog_id"]: entry for entry in current_entries}
    existing_by_hash = {entry.get("sha256", "").lower(): entry for entry in current_entries if entry.get("sha256")}
    owner = cache_root.stat()

    with zipfile.ZipFile(archive_path) as archive:
        rows = verify_archive(archive)
        plans = []
        skipped = []
        for row in rows:
            catalog_id = _slug(row["file"])
            prior = existing_by_id.get(catalog_id)
            if prior and prior.get("sha256", "").lower() != row["sha256"]:
                raise ValueError(f"catalog id collision with different content: {catalog_id}")
            if prior or row["sha256"] in existing_by_hash:
                skipped.append(row["file"])
                continue
            source_url, query_removed = _sanitize_url(row["source_url"])
            plans.append((row, catalog_id, source_url, query_removed))
        if len({catalog_id for _, catalog_id, _, _ in plans}) != len(plans):
            raise ValueError("new catalog ids collide")
        result: dict[str, object] = {
            "archive_catalogs": len(rows),
            "existing_catalogs": len(current_entries),
            "planned_new_catalogs": len(plans),
            "skipped_existing": skipped,
            "sanitized_source_urls": sum(query_removed for _, _, _, query_removed in plans),
            "cache_root": str(cache_root),
        }
        if verify_only:
            result["status"] = "verified_only_no_write"
            return result

        # Stage in the same filesystem; install the index last. No existing entry
        # or source file is replaced. Leftover new files after interruption can
        # be reused only if byte-identical on a repeated import.
        with tempfile.TemporaryDirectory(prefix=".catalog-import-", dir=cache_root) as temporary:
            stage = Path(temporary)
            new_entries = []
            for row, catalog_id, source_url, query_removed in plans:
                filename = row["file"]
                suffix = Path(filename).suffix.lower().lstrip(".")
                staged_original = stage / "originals" / filename
                staged_text = stage / "text" / f"{catalog_id}.{'txt' if suffix == 'pdf' else 'tsv'}"
                staged_original.parent.mkdir(parents=True, exist_ok=True)
                staged_text.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(f"catalogs/{filename}") as source, staged_original.open("wb") as output:
                    shutil.copyfileobj(source, output, length=4 * 1024 * 1024)
                with staged_original.open("rb") as stream:
                    if _sha256_stream(stream) != row["sha256"]:
                        raise ValueError(f"staged catalog checksum changed: {filename}")
                extraction = (
                    _extract_pdf(staged_original, staged_text)
                    if suffix == "pdf"
                    else _extract_xlsx(staged_original, staged_text)
                )
                original_path = cache_root / suffix / filename
                text_path = cache_root / "text" / staged_text.name
                new_entries.append(
                    {
                        "catalog_id": catalog_id,
                        "brand": row["brand"],
                        "title": row["catalog"],
                        "format": suffix,
                        "filename": filename,
                        "edition": row["edition"],
                        "product_scope": row["product_scope"],
                        "market_scope": row["market_scope"],
                        "scope": row["product_scope"],
                        "source_url": source_url,
                        "source_url_query_removed": query_removed,
                        "limits": row["fitment_limit"],
                        "authority": "supplied_catalog_file",
                        "offline_use": "Provided catalog evidence; verify vehicle and part applicability independently.",
                        "retrieved_on": datetime.now(UTC).date().isoformat(),
                        "local_file": _relative(cache_root, original_path),
                        "text_path": _relative(cache_root, text_path),
                        "download_status": "archive_verified",
                        "bytes": staged_original.stat().st_size,
                        "sha256": row["sha256"],
                        **extraction,
                    }
                )

            next_index = dict(existing)
            next_index["entries"] = [*current_entries, *new_entries]
            next_index["catalog_count"] = len(next_index["entries"])
            next_index["updated_on"] = datetime.now(UTC).date().isoformat()
            staged_index = stage / "catalog_index.json"
            staged_index.write_text(json.dumps(next_index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            _chown_private(staged_index, owner)

            for entry in new_entries:
                original_path = cache_root / entry["format"] / entry["filename"]
                text_path = cache_root / "text" / Path(entry["text_path"]).name
                staged_original = stage / "originals" / entry["filename"]
                staged_text = stage / "text" / Path(entry["text_path"]).name
                for staged, destination in ((staged_original, original_path), (staged_text, text_path)):
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _chown_private(destination.parent, owner)
                    if destination.exists():
                        with staged.open("rb") as left, destination.open("rb") as right:
                            if _sha256_stream(left) != _sha256_stream(right):
                                raise ValueError(f"existing unindexed file differs: {destination}")
                        continue
                    _chown_private(staged, owner)
                    os.replace(staged, destination)
            os.replace(staged_index, cache_root / "catalog_index.json")

        result.update(
            {
                "status": "imported",
                "added_catalogs": len(new_entries),
                "total_catalogs": len(current_entries) + len(new_entries),
                "pdf_pages": sum(entry.get("pages", 0) for entry in new_entries),
                "pdf_text_pages": sum(entry.get("text_pages", 0) for entry in new_entries),
                "pdf_empty_pages": sum(len(entry.get("empty_pages", [])) for entry in new_entries),
                "xlsx_sheets": sum(entry.get("sheets", 0) for entry in new_entries),
                "xlsx_rows": sum(entry.get("rows", 0) for entry in new_entries),
                "xlsx_uncached_formula_cells": sum(
                    entry.get("formula_without_cached_value", 0) for entry in new_entries
                ),
                "catalogs": [
                    {
                        "catalog_id": entry["catalog_id"],
                        "status": entry["text_extract_status"],
                        "pages": entry.get("pages"),
                        "text_pages": entry.get("text_pages"),
                        "empty_pages": entry.get("empty_pages", []),
                        "sheets": entry.get("sheets"),
                        "sheet_rows": entry.get("sheet_rows"),
                    }
                    for entry in new_entries
                ],
                "entries_needing_followup": [
                    {
                        "catalog_id": entry["catalog_id"],
                        "status": entry["text_extract_status"],
                        "empty_pages": entry.get("empty_pages", []),
                    }
                    for entry in new_entries
                    if entry["text_extract_status"] != "ok"
                ],
            }
        )
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True, help="supplied catalog ZIP")
    parser.add_argument(
        "--cache-root", type=Path, required=True, help="existing private data/offline_parts_catalogs directory"
    )
    parser.add_argument("--verify-only", action="store_true", help="verify manifest and plan without writing")
    args = parser.parse_args()
    print(
        json.dumps(
            import_catalogs(args.archive, args.cache_root, verify_only=args.verify_only), ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
