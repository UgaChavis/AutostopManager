#!/usr/bin/env python3
"""Fill only indexed empty PDF pages with clearly marked, unverified OCR text.

The original PDF and every nonempty page remain unchanged. The private cache
index is replaced after all changed text files. If an interrupted run installed
text before the index, the OCR marker lets the next run reconcile that page.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


PAGE_RE = re.compile(r"(?P<marker>\f\[PAGE (?P<number>\d+)\]\n)(?P<body>.*?)(?=\f\[PAGE \d+\]\n|\Z)", re.DOTALL)
OCR_MARKER = "[OCR UNVERIFIED: eng+rus]"
EXPECTED_RELATIVE_ROOT = Path("data/offline_parts_catalogs")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_entry_path(cache_root: Path, recorded: str, kind: str, expected_name: str) -> Path:
    expected = EXPECTED_RELATIVE_ROOT / kind / expected_name
    if Path(recorded) != expected:
        raise ValueError(f"unexpected {kind} path in cache index")
    result = cache_root / kind / expected_name
    if result.is_symlink() or not result.is_file():
        raise ValueError(f"missing or linked {kind} file: {expected_name}")
    return result


def _validate_pages(entry: dict[str, object]) -> list[int]:
    count = entry.get("pages")
    pages = entry.get("empty_pages")
    if not isinstance(count, int) or count <= 0 or not isinstance(pages, list):
        raise ValueError("invalid PDF page metadata")
    if any(not isinstance(page, int) or isinstance(page, bool) or page < 1 or page > count for page in pages):
        raise ValueError("invalid empty page number")
    if len(pages) != len(set(pages)):
        raise ValueError("duplicate empty page number")
    return sorted(pages)


def _page_bodies(content: str) -> dict[int, str]:
    matches = list(PAGE_RE.finditer(content))
    if not matches or matches[0].start() != 0 or matches[-1].end() != len(content):
        raise ValueError("PDF text file has unexpected page markers")
    numbers = [int(match.group("number")) for match in matches]
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError("PDF text file has missing or repeated page markers")
    return {int(match.group("number")): match.group("body") for match in matches}


def _ocr_page(pdf: Path, page: int, temporary: Path) -> str:
    image_base = temporary / f"page-{page}"
    image = image_base.with_suffix(".png")
    try:
        render = subprocess.run(
            [
                "pdftoppm",
                "-f",
                str(page),
                "-l",
                str(page),
                "-r",
                "220",
                "-gray",
                "-singlefile",
                "-png",
                str(pdf),
                str(image_base),
            ],
            capture_output=True,
            timeout=180,
            check=False,
        )
        if render.returncode or not image.is_file():
            raise RuntimeError(
                f"pdftoppm failed on {pdf.name} page {page}: {render.stderr.decode('utf-8', 'replace')[:250]}"
            )
        ocr = subprocess.run(
            ["tesseract", str(image), "stdout", "-l", "eng+rus", "--psm", "3"],
            capture_output=True,
            timeout=180,
            check=False,
        )
        if ocr.returncode:
            raise RuntimeError(
                f"tesseract failed on {pdf.name} page {page}: {ocr.stderr.decode('utf-8', 'replace')[:250]}"
            )
        text = ocr.stdout.decode("utf-8", "replace")
        # Keep a single page in a single marker block; OCR must not introduce
        # control characters or a fake following page marker.
        text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text).strip()
        if not any(character.isalnum() for character in text):
            return ""
        return text
    finally:
        image.unlink(missing_ok=True)


def _replace_pages(content: str, replacements: dict[int, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        number = int(match.group("number"))
        body = replacements.get(number)
        if body is None:
            return match.group(0)
        return match.group("marker") + OCR_MARKER + "\n" + body + "\n"

    return PAGE_RE.sub(replace, content)


def _private_file(path: Path, source: Path) -> None:
    owner = source.stat()
    os.chown(path, owner.st_uid, owner.st_gid)
    os.chmod(path, owner.st_mode & 0o777)


def _pending_pdf_entries(
    cache_root: Path, entries: list[dict[str, object]]
) -> list[tuple[dict[str, object], list[int], Path, Path]]:
    pending = []
    for entry in entries:
        if not entry.get("empty_pages"):
            continue
        if entry.get("format") != "pdf":
            raise ValueError("non-PDF entry declares empty_pages")
        catalog_id = entry.get("catalog_id")
        filename = entry.get("filename")
        if not isinstance(catalog_id, str) or not re.fullmatch(r"[a-z0-9_-]{1,80}", catalog_id):
            raise ValueError("invalid catalog_id")
        if not isinstance(filename, str) or Path(filename).name != filename or not filename.lower().endswith(".pdf"):
            raise ValueError("invalid PDF filename")
        pages = _validate_pages(entry)
        pdf = _safe_entry_path(cache_root, entry.get("local_file", ""), "pdf", filename)
        text = _safe_entry_path(cache_root, entry.get("text_path", ""), "text", f"{catalog_id}.txt")
        if _sha256(pdf) != entry.get("sha256", "").lower():
            raise ValueError(f"original PDF checksum differs: {catalog_id}")
        body_by_page = _page_bodies(text.read_text(encoding="utf-8"))
        if len(body_by_page) != entry["pages"] or any(page not in body_by_page for page in pages):
            raise ValueError(f"PDF text page count differs: {catalog_id}")
        for page in pages:
            body = body_by_page[page]
            if any(character.isalnum() for character in body) and not body.startswith(OCR_MARKER):
                raise ValueError(f"page has unmarked text despite empty_pages: {catalog_id}/{page}")
        pending.append((entry, pages, pdf, text))
    return pending


def ocr_catalogs(cache_root: Path, *, verify_only: bool) -> dict[str, object]:
    cache_root = cache_root.resolve(strict=True)
    index_path = cache_root / "catalog_index.json"
    if index_path.is_symlink() or not index_path.is_file():
        raise ValueError("cache index missing or linked")
    index_before = _sha256(index_path)
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(index, dict) or not isinstance(index.get("entries"), list):
        raise ValueError("invalid cache index")
    entries = index["entries"]
    if any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("invalid cache index entry")
    pending = _pending_pdf_entries(cache_root, entries)

    report: dict[str, object] = {
        "status": "verified_only_no_write" if verify_only else "no_empty_pages",
        "catalogs_with_empty_pages": len(pending),
        "empty_pages_before": sum(len(pages) for _, pages, _, _ in pending),
        "catalogs": [{"catalog_id": entry["catalog_id"], "empty_pages": pages} for entry, pages, _, _ in pending],
    }
    if verify_only or not pending:
        return report

    changes: list[tuple[Path, Path, str]] = []
    page_reports: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix=".catalog-ocr-", dir=cache_root) as temporary:
        stage = Path(temporary)
        for entry, pages, pdf, text_path in pending:
            content = text_path.read_text(encoding="utf-8")
            body_by_page = _page_bodies(content)
            before_hash = _sha256(text_path)
            replacements: dict[int, str] = {}
            recovered: list[int] = []
            failures: list[int] = []
            for page in pages:
                old = body_by_page[page]
                if old.startswith(OCR_MARKER) and any(
                    character.isalnum() for character in old.removeprefix(OCR_MARKER)
                ):
                    recovered.append(page)
                    continue
                ocr_text = _ocr_page(pdf, page, stage)
                if ocr_text:
                    replacements[page] = ocr_text
                else:
                    failures.append(page)
            filled = sorted([*replacements, *recovered])
            if replacements:
                staged_text = stage / f"{entry['catalog_id']}.txt"
                staged_text.write_text(_replace_pages(content, replacements), encoding="utf-8", newline="")
                _private_file(staged_text, text_path)
                changes.append((staged_text, text_path, before_hash))
                text_bytes = staged_text.stat().st_size
            else:
                text_bytes = text_path.stat().st_size
            entry["empty_pages"] = [page for page in pages if page not in filled]
            entry["text_pages"] = entry["pages"] - len(entry["empty_pages"])
            prior_ocr = entry.get("ocr_pages", [])
            if not isinstance(prior_ocr, list) or any(not isinstance(page, int) for page in prior_ocr):
                raise ValueError("invalid prior ocr_pages")
            entry["ocr_pages"] = sorted(set(prior_ocr) | set(filled))
            entry["ocr_page_count"] = len(entry["ocr_pages"])
            entry["ocr_status"] = "unverified" if entry["ocr_pages"] else "none"
            entry["ocr_note"] = (
                "OCR text is unverified; inspect the original page before relying on an article or fitment claim."
            )
            entry["text_extract_status"] = "ok_with_unverified_ocr" if not entry["empty_pages"] else "partial_needs_ocr"
            entry["text_bytes"] = text_bytes
            page_reports.append(
                {
                    "catalog_id": entry["catalog_id"],
                    "filled": filled,
                    "remaining": entry["empty_pages"],
                    "ocr_empty": failures,
                }
            )

        if not any(item["filled"] for item in page_reports):
            report["status"] = "ocr_found_no_text"
            report["catalogs"] = page_reports
            return report
        index["updated_on"] = datetime.now(UTC).date().isoformat()
        staged_index = stage / "catalog_index.json"
        staged_index.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _private_file(staged_index, index_path)

        # All expensive rendering finished. Check inputs again, then replace
        # text files and finally the index. A repeated run reconciles any OCR
        # text installed before an interrupted index replacement.
        if _sha256(index_path) != index_before:
            raise RuntimeError("cache index changed during OCR")
        for _, original, old_hash in changes:
            if _sha256(original) != old_hash:
                raise RuntimeError(f"PDF text changed during OCR: {original.name}")
        for staged, original, _ in changes:
            os.replace(staged, original)
        os.replace(staged_index, index_path)

    report.update(
        {
            "status": "ocr_updated",
            "filled_pages": sum(len(item["filled"]) for item in page_reports),
            "remaining_empty_pages": sum(len(item["remaining"]) for item in page_reports),
            "catalogs": page_reports,
        }
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-root", type=Path, required=True, help="existing private data/offline_parts_catalogs directory"
    )
    parser.add_argument(
        "--verify-only", action="store_true", help="list indexed empty pages without rendering or writing"
    )
    args = parser.parse_args()
    for program in ("pdftoppm", "tesseract"):
        if not shutil.which(program) and not args.verify_only:
            parser.error(f"required executable missing: {program}")
    print(json.dumps(ocr_catalogs(args.cache_root, verify_only=args.verify_only), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
