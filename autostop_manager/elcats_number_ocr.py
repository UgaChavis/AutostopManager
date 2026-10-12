"""Optional local OCR of ordinary public part-number images, never CAPTCHA."""

from __future__ import annotations

import csv
import io
import re
import shutil
import subprocess
import time
from typing import Any

from .public_catalog_http import CatalogReadError


def _number_from_tsv(text: str) -> tuple[str, float]:
    words = []
    scores = []
    for row in csv.DictReader(io.StringIO(text), delimiter="\t"):
        word = (row.get("text") or "").strip()
        if not word:
            continue
        try:
            score = float(row.get("conf") or "-1")
        except ValueError:
            return "", -1
        words.append(word)
        scores.append(score)
    number = "".join(words).upper()
    if not re.fullmatch(r"(?=.*[0-9])[A-Z0-9]{5,24}", number) or not scores:
        return "", -1
    return number, min(scores)


def read_number_ocr(body: bytes, *, deadline: float) -> dict[str, Any]:
    """Agreement is an extraction check; it never proves OEM identity or fitment."""
    executable = shutil.which("tesseract")
    if executable is None:
        raise CatalogReadError("catalog_ocr_unavailable")
    observations: list[dict[str, Any]] = []
    for mode in (7, 13):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CatalogReadError("catalog_deadline_exceeded")
        try:
            process = subprocess.run(
                [
                    executable,
                    "stdin",
                    "stdout",
                    "--psm",
                    str(mode),
                    "-c",
                    "tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
                    "tsv",
                ],
                input=body,
                capture_output=True,
                timeout=min(5, remaining),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CatalogReadError("catalog_ocr_timeout") from exc
        except OSError as exc:
            raise CatalogReadError("catalog_ocr_unavailable") from exc
        if process.returncode:
            raise CatalogReadError("catalog_ocr_failed")
        number, confidence = _number_from_tsv(process.stdout.decode("utf-8", errors="replace"))
        observations.append({"psm": mode, "number": number, "confidence": round(confidence, 3)})
    agreed = (
        observations[0]["number"]
        and observations[0]["number"] == observations[1]["number"]
        and observations[0]["confidence"] >= 85
        and observations[1]["confidence"] >= 35
    )
    return {
        "raw_number": observations[0]["number"] if agreed else None,
        "normalized_number": observations[0]["number"] if agreed else None,
        "status": "ocr_candidate_unverified" if agreed else "ocr_inconclusive",
        "method": "local_tesseract_dual_psm",
        "observations": observations,
        "identifier_verified": False,
    }
