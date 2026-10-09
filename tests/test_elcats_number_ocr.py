from __future__ import annotations

import subprocess
import time
from pathlib import Path
import shutil

import pytest

from autostop_manager import elcats_number_ocr as ocr
from autostop_manager.public_catalog_http import CatalogReadError


def _tsv(number: str, score: str) -> bytes:
    return f"level\tconf\ttext\n5\t{score}\t{number}\n".encode()


@pytest.mark.parametrize(
    "secondary,score,status",
    [
        ("48130091A0", "44.5", "ocr_candidate_unverified"),
        ("48130091AO", "95", "ocr_inconclusive"),
        ("48130091A0", "34", "ocr_inconclusive"),
        ("ABCDE", "95", "ocr_inconclusive"),
    ],
)
def test_agreement_and_confidence_gate_never_marks_verified(monkeypatch, secondary, score, status):
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "/fixture/tesseract")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["input"] == b"png" and kwargs["timeout"] <= 5
        return subprocess.CompletedProcess(
            command, 0, stdout=_tsv("48130091A0", "90") if len(calls) == 1 else _tsv(secondary, score)
        )

    monkeypatch.setattr(ocr.subprocess, "run", run)
    result = ocr.read_number_ocr(b"png", deadline=time.monotonic() + 10)
    assert result["status"] == status and result["identifier_verified"] is False
    assert result["raw_number"] == ("48130091A0" if status == "ocr_candidate_unverified" else None)
    assert calls[0][calls[0].index("--psm") + 1] == "7"
    assert calls[1][calls[1].index("--psm") + 1] == "13"


def test_missing_engine_and_expired_deadline_do_not_start_process(monkeypatch):
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    with pytest.raises(CatalogReadError, match="catalog_ocr_unavailable"):
        ocr.read_number_ocr(b"png", deadline=time.monotonic() + 1)
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "/fixture/tesseract")
    with pytest.raises(CatalogReadError, match="catalog_deadline_exceeded"):
        ocr.read_number_ocr(b"png", deadline=0)


@pytest.mark.parametrize(
    "failure,error",
    [
        (OSError("engine"), "catalog_ocr_unavailable"),
        (subprocess.TimeoutExpired("tesseract", 1), "catalog_ocr_timeout"),
    ],
)
def test_ocr_process_failures_are_safe_stable_reasons(monkeypatch, failure, error):
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "/fixture/tesseract")

    def run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(ocr.subprocess, "run", run)
    with pytest.raises(CatalogReadError, match=error):
        ocr.read_number_ocr(b"png", deadline=time.monotonic() + 1)


def test_decoder_failure_and_malformed_confidence(monkeypatch):
    assert ocr._number_from_tsv("conf\ttext\nx\t48130091A0") == ("", -1)
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "/fixture/tesseract")
    monkeypatch.setattr(
        ocr.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 1, stdout=b"")
    )
    with pytest.raises(CatalogReadError, match="catalog_ocr_failed"):
        ocr.read_number_ocr(b"png", deadline=time.monotonic() + 1)


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="optional local OCR engine is absent")
def test_local_decoder_of_clean_public_number_fixture():
    body = (Path(__file__).parent / "fixtures/elcats/ssangyong_pad_number.png").read_bytes()
    response = ocr.read_number_ocr(body, deadline=time.monotonic() + 10)
    assert response["raw_number"] == "48130091A0"
    assert response["status"] == "ocr_candidate_unverified"
    assert response["identifier_verified"] is False
