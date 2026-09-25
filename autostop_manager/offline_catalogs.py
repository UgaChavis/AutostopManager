"""Bounded, read-only lookup in the local offline parts catalog cache."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config import get_db_path


CATALOG_ROOT_ENV = "AUTOSTOP_OFFLINE_CATALOG_ROOT"
MAX_QUERY_CHARS = 80
MAX_RESULTS = 20
MAX_CATALOGS = 64
MAX_INDEX_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 1024**3
MAX_TOTAL_BYTES = 4 * 1024**3
MAX_SEARCH_SECONDS = 20.0
MAX_EXCERPT_CHARS = 360
_CATALOG_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}\Z")
_VIN = re.compile(r"(?<![A-Za-z0-9])[A-HJ-NPR-Za-hj-npr-z0-9]{17}(?![A-Za-z0-9])")
_PAGE_MARKER = re.compile(r"\[PAGE\s+(\d+)\]")


def _catalog_root() -> Path | None:
    configured = os.environ.get(CATALOG_ROOT_ENV, "").strip()
    root = Path(configured).expanduser() if configured else get_db_path().parent / "offline_parts_catalogs"
    if not root.is_absolute() or ".." in root.parts:
        return None
    return root.resolve()


def _source_url(value: Any) -> str:
    try:
        parts = urlsplit(str(value or ""))
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return ""
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))[:800]
    except ValueError:
        return ""


def _safe_text_path(root: Path, entry: dict[str, Any]) -> Path | None:
    catalog_id = str(entry.get("catalog_id") or "")
    raw_path = str(entry.get("text_path") or "")
    if not _CATALOG_ID.fullmatch(catalog_id) or not raw_path:
        return None
    name = Path(raw_path).name
    if Path(name).stem != catalog_id or Path(name).suffix.lower() not in {".txt", ".tsv"}:
        return None
    path = (root / "text" / name).resolve()
    return path if path.is_relative_to(root) else None


def _load_entries(root: Path) -> tuple[list[dict[str, Any]], str | None]:
    index_path = root / "catalog_index.json"
    try:
        if index_path.stat().st_size > MAX_INDEX_BYTES:
            return [], "catalog_index_too_large"
        payload = json.loads(index_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return [], "catalog_index_missing"
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [], "catalog_index_unreadable"
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        return [], "catalog_index_invalid"
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in payload["entries"]:
        if not isinstance(item, dict):
            continue
        catalog_id = str(item.get("catalog_id") or "")
        if _CATALOG_ID.fullmatch(catalog_id) and catalog_id not in seen:
            entries.append(item)
            seen.add(catalog_id)
    return entries, None


def _rg_matches(
    path: Path, query: str, limit: int, deadline: float, *, is_pdf: bool
) -> tuple[list[tuple[int, str]], str | None]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return [], "search_timeout"
    is_tsv = path.suffix.lower() == ".tsv"
    literal = "".join(f"\\{char}" if char in r"\.^$|?*+()[]{}" else char for char in query)
    try:
        with path.open("rb") as source:
            has_synthetic_page_markers = is_pdf and source.read(16).startswith(b"\f[PAGE ")
    except OSError:
        return [], "catalog_read_failed"
    if is_tsv:
        # A sheet name is repeated on every physical XLSX row. Match only the
        # article/data fields after the sheet and source-row columns.
        expression = r"^[^\t]*\t[0-9]+\t.*" + literal
    elif has_synthetic_page_markers:
        # Extracted PDF pages have synthetic [PAGE N] lines. Exclude them in
        # ripgrep itself, before --max-count can hide a real catalog match.
        expression = r"^[^\x0C].*" + literal
    else:
        expression = query
    command = [
        "rg",
        "--no-config",
        "--no-messages",
        "--text",
        "--ignore-case",
        "--line-number",
        "--no-filename",
        "--max-columns",
        "20000",
        "--max-columns-preview",
        "--max-count",
        str(limit),
    ]
    if not is_tsv and not has_synthetic_page_markers:
        command.append("--fixed-strings")
    command.extend(["-e", expression, "--", str(path)])
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=remaining,
            check=False,
        )
    except FileNotFoundError:
        return [], "ripgrep_unavailable"
    except subprocess.TimeoutExpired:
        return [], "search_timeout"
    except OSError:
        return [], "catalog_read_failed"
    if result.returncode not in {0, 1}:
        return [], "catalog_search_failed"
    matches: list[tuple[int, str]] = []
    for line in result.stdout.split("\n"):
        number, separator, content = line.partition(":")
        if separator and number.isdecimal():
            matches.append((int(number), content))
    return matches, None


def _pdf_locators(path: Path, line_numbers: set[int], deadline: float) -> dict[int, tuple[int, str]]:
    locators: dict[int, tuple[int, str]] = {}
    if not line_numbers:
        return locators
    page = 1
    previous = ""
    last_line = max(line_numbers)
    with path.open("r", encoding="utf-8-sig", errors="replace") as source:
        for line_number, raw in enumerate(source, 1):
            if line_number % 2048 == 0 and time.monotonic() >= deadline:
                raise TimeoutError
            marker = _PAGE_MARKER.search(raw)
            page = int(marker.group(1)) if marker else page + raw.count("\f")
            if raw.count("\f"):
                previous = ""
            compact = " ".join(raw.replace("\f", " ").split())
            if line_number in line_numbers:
                locators[line_number] = (page, previous)
            if compact and not marker:
                previous = compact[:MAX_EXCERPT_CHARS]  # context from the same page only
            if line_number >= last_line:
                break
    return locators


def _old_xlsx_locators(path: Path, line_numbers: set[int], deadline: float) -> dict[int, tuple[str, int]]:
    locators: dict[int, tuple[str, int]] = {}
    if not line_numbers:
        return locators
    sheet = ""
    row_number = 0
    last_line = max(line_numbers)
    with path.open("r", encoding="utf-8-sig", errors="replace") as source:
        for line_number, raw in enumerate(source, 1):
            if line_number % 2048 == 0 and time.monotonic() >= deadline:
                raise TimeoutError
            if raw.startswith("### "):
                sheet = raw[4:].strip()
                row_number = 0
            elif sheet:
                row_number += 1
            if line_number in line_numbers:
                locators[line_number] = (sheet, row_number)
            if line_number >= last_line:
                break
    return locators


def _excerpt(value: str, query: str) -> str:
    compact = " ".join(value.replace("\f", " ").split())
    position = compact.casefold().find(query.casefold())
    if len(compact) > MAX_EXCERPT_CHARS and position >= 0:
        start = max(0, position - 100)
        compact = compact[start : start + MAX_EXCERPT_CHARS]
    return _VIN.sub("[VIN REDACTED]", compact[:MAX_EXCERPT_CHARS])


def _metadata_text(entry: dict[str, Any], field: str, limit: int = 240) -> str:
    compact = " ".join(str(entry.get(field) or "").split())[:limit]
    return _VIN.sub("[VIN REDACTED]", compact)


def _result(
    entry: dict[str, Any],
    content: str,
    locator: dict[str, Any],
    *,
    query: str,
    previous: str = "",
    extraction_method: str | None = None,
) -> dict[str, Any]:
    context = f"{previous[-100:]} | {content}" if previous else content
    result = {
        "catalog_id": entry["catalog_id"],
        "brand": _metadata_text(entry, "brand"),
        "title": _metadata_text(entry, "title"),
        "edition": _metadata_text(entry, "edition"),
        "market_scope": _metadata_text(entry, "market_scope"),
        "product_scope": _metadata_text(entry, "product_scope") or _metadata_text(entry, "scope"),
        "format": _metadata_text(entry, "format", 16),
        "source_url": _source_url(entry.get("source_url")),
        "source_sha256": _metadata_text(entry, "sha256", 64),
        "text_extract_status": _metadata_text(entry, "text_extract_status", 64) or "unknown",
        "limits": _metadata_text(entry, "limits", 500),
        "locator": locator,
        "excerpt": _excerpt(context, query),
        "candidate_only": True,
        "fitment_status": "unverified_catalog_candidate",
    }
    if extraction_method is not None:
        result["extraction_method"] = extraction_method
        result["ocr_unverified"] = extraction_method == "ocr"
    return result


def _format_matches(
    entry: dict[str, Any],
    path: Path,
    matches: list[tuple[int, str]],
    query: str,
    deadline: float,
) -> tuple[list[dict[str, Any]], list[str]]:
    is_xlsx = str(entry.get("format") or "").lower() == "xlsx"
    line_numbers = {line for line, _ in matches}
    pdf_locators = _pdf_locators(path, line_numbers, deadline) if not is_xlsx else {}
    old_xlsx_locators = _old_xlsx_locators(path, line_numbers, deadline) if is_xlsx and path.suffix == ".txt" else {}
    raw_ocr_pages = entry.get("ocr_pages")
    ocr_pages = (
        {page for page in raw_ocr_pages if type(page) is int and page > 0} if isinstance(raw_ocr_pages, list) else set()
    )
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for line_number, content in matches:
        if is_xlsx and path.suffix == ".tsv":
            sheet, separator, rest = content.partition("\t")
            row_text, row_separator, fields = rest.partition("\t") if separator else ("", "", "")
            if not row_separator or not row_text.isdecimal():
                warnings.append("catalog_row_invalid")
                continue
            locator = {"sheet": sheet, "row": int(row_text)}
            snippet = fields
            previous = ""
            extraction_method = None
        elif is_xlsx:
            sheet, row_number = old_xlsx_locators.get(line_number, ("", 0))
            locator = {"sheet": sheet, "row": row_number}
            snippet = content
            previous = ""
            extraction_method = None
        else:
            page, previous = pdf_locators.get(line_number, (0, ""))
            locator = {"page": page}
            snippet = content
            extraction_method = "ocr" if page in ocr_pages else "text"
        rows.append(
            _result(
                entry,
                snippet,
                locator,
                query=query,
                previous=previous,
                extraction_method=extraction_method,
            )
        )
    return rows, warnings


def search_offline_parts_catalogs(
    query: str,
    *,
    catalog_id: str | None = None,
    brand: str | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    """Search local extracted catalog text without promoting a hit to confirmed fitment."""
    if (
        not isinstance(query, str)
        or not 3 <= len(query.strip()) <= MAX_QUERY_CHARS
        or any(ord(character) < 32 for character in query)
    ):
        return {"ok": False, "error": "query_invalid", "results": []}
    if _VIN.search(query):
        return {"ok": False, "error": "vin_query_rejected", "results": []}
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
        return {"ok": False, "error": "limit_invalid", "results": []}
    if catalog_id is not None and (not isinstance(catalog_id, str) or not _CATALOG_ID.fullmatch(catalog_id)):
        return {"ok": False, "error": "catalog_id_invalid", "results": []}
    if brand is not None and (not isinstance(brand, str) or len(brand) > 80 or _VIN.search(brand)):
        return {"ok": False, "error": "brand_invalid", "results": []}
    root = _catalog_root()
    if root is None:
        return {"ok": False, "error": "catalog_root_invalid", "results": []}
    entries, error = _load_entries(root)
    if error:
        return {"ok": False, "error": error, "results": []}
    selected = [
        entry
        for entry in entries
        if (catalog_id is None or entry["catalog_id"] == catalog_id)
        and (not brand or brand.casefold() in str(entry.get("brand") or "").casefold())
    ]
    if catalog_id is not None and not selected:
        return {"ok": False, "error": "catalog_not_found", "results": []}
    deadline = time.monotonic() + MAX_SEARCH_SECONDS
    results: list[dict[str, Any]] = []
    skipped_catalog_ids: list[str] = []
    partially_extracted_catalog_ids: list[str] = []
    warnings: list[str] = []
    scanned_catalogs = 0
    scanned_bytes = 0
    result_limit_reached = False
    for position, entry in enumerate(selected[:MAX_CATALOGS]):
        entry_id = str(entry["catalog_id"])
        if time.monotonic() >= deadline:
            warnings.append("search_timeout")
            skipped_catalog_ids.extend(str(row["catalog_id"]) for row in selected[position:])
            break
        path = _safe_text_path(root, entry)
        if path is None or not path.is_file():
            warnings.append("catalog_text_missing_or_invalid")
            skipped_catalog_ids.append(entry_id)
            continue
        try:
            size = path.stat().st_size
        except OSError:
            warnings.append("catalog_read_failed")
            skipped_catalog_ids.append(entry_id)
            continue
        if size > MAX_FILE_BYTES or scanned_bytes + size > MAX_TOTAL_BYTES:
            warnings.append("catalog_size_limit")
            skipped_catalog_ids.append(entry_id)
            continue
        matches, search_error = _rg_matches(
            path,
            query.strip(),
            limit - len(results),
            deadline,
            is_pdf=str(entry.get("format") or "").lower() == "pdf",
        )
        if search_error:
            warnings.append(search_error)
            skipped_catalog_ids.append(entry_id)
            continue
        scanned_catalogs += 1
        scanned_bytes += size
        extraction_status = str(entry.get("text_extract_status") or "")
        extraction_incomplete = extraction_status.startswith("partial_") or extraction_status in {"failed", "error"}
        if extraction_incomplete or entry.get("empty_pages"):
            partially_extracted_catalog_ids.append(entry_id)
            warnings.append("catalog_extraction_partial")
        try:
            formatted, format_warnings = _format_matches(entry, path, matches, query.strip(), deadline)
        except (OSError, TimeoutError):
            warnings.append("catalog_locator_unavailable")
            skipped_catalog_ids.append(entry_id)
            continue
        warnings.extend(format_warnings)
        results.extend(formatted)
        if len(results) >= limit:
            result_limit_reached = True
            skipped_catalog_ids.extend(str(row["catalog_id"]) for row in selected[position + 1 :])
            break
    if len(selected) > MAX_CATALOGS and not result_limit_reached:
        warnings.append("catalog_count_limit")
        skipped_catalog_ids.extend(str(row["catalog_id"]) for row in selected[MAX_CATALOGS:])
    return {
        "ok": True,
        "schema": "offline_parts_catalog_search_v1",
        "candidate_only": True,
        "fitment_rule": "A catalog hit is a candidate; confirm vehicle, engine, market, dimensions and OEM/VIN applicability independently.",
        "selected_catalogs": len(selected),
        "scanned_catalogs": scanned_catalogs,
        "scanned_bytes": scanned_bytes,
        "result_limit_reached": result_limit_reached,
        "search_incomplete": bool(skipped_catalog_ids or warnings or result_limit_reached),
        "skipped_catalog_ids": list(dict.fromkeys(skipped_catalog_ids))[:MAX_CATALOGS],
        "skipped_catalog_count": len(set(skipped_catalog_ids)),
        "partially_extracted_catalog_ids": partially_extracted_catalog_ids,
        "warnings": list(dict.fromkeys(warnings)),
        "results": results[:limit],
    }
