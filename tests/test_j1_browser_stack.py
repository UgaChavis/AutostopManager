"""Focused contracts for J1's separate renderer, proxy, and OCR fallback."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket

import pytest

from autostop_manager import j1_browser, j1_browser_proxy as proxy, j1_browser_renderer as renderer, j1_fetch, j1_ocr


ROOT = Path(__file__).resolve().parents[1]


def test_url_context_dlp_allows_bulletin_id_and_rejects_private_values() -> None:
    bulletin = "https://static.nhtsa.gov/odi/tsbs/2014/SB-10063500-2280.pdf"
    assert j1_browser._public_http_url(bulletin) == bulletin
    assert not j1_browser._public_http_url("https://example.org/ZZZ00000000000000")
    assert not j1_browser._public_http_url("https://example.org/document/12345678901234567")
    assert not j1_browser._public_http_url("http://127.0.0.1/private")
    assert not j1_browser._public_http_url("https://example.org/?token=very-secret-token")


def test_browser_client_is_safely_disabled_when_socket_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(j1_browser, "_isolation_verified", lambda: True)
    result = j1_browser.render_page("https://example.org/article", socket_path=str(tmp_path / "missing.sock"))
    assert result == {
        "ok": False,
        "schema": j1_browser.SCHEMA,
        "error": "browser_unavailable",
        "retryable": True,
        "safe_disabled": True,
    }


def test_browser_client_requires_deployment_isolation_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOSTOP_J1_BROWSER_ISOLATION_MARKER", str(tmp_path / "not-ready"))
    result = j1_browser.render_page("https://example.org/article", socket_path=str(tmp_path / "missing.sock"))
    assert result == {
        "ok": False,
        "schema": j1_browser.SCHEMA,
        "error": "browser_isolation_unverified",
        "retryable": True,
        "safe_disabled": True,
    }


def test_static_empty_html_uses_isolated_browser_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(j1_browser, "isolation_verified", lambda: True)
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (True, 1.0))
    monkeypatch.setattr(j1_fetch, "_rate_limit", lambda *_args: None)
    monkeypatch.setattr(
        j1_fetch,
        "_request_public",
        lambda *_args, **_kwargs: (200, {"content-type": "text/html"}, b"<p>short</p>", "https://example.org/page"),
    )
    monkeypatch.setattr(
        j1_browser,
        "render_page",
        lambda url, **_kwargs: (
            calls.append(url)
            or {
                "ok": True,
                "url": url,
                "title": "Rendered page",
                "text": "Rendered public evidence " + "x" * 100,
                "kind": "browser",
                "extraction_method": "browser_dom",
            }
        ),
    )
    result = j1_fetch.fetch_document("https://example.org/page")
    assert calls == ["https://example.org/page"]
    assert result["ok"] is True
    assert result["extraction_method"] == "browser_dom"
    assert result["browser_attempted"] is True


def test_static_pdf_uses_bounded_ocr_only_after_text_extract_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (True, 1.0))
    monkeypatch.setattr(j1_fetch, "_rate_limit", lambda *_args: None)
    monkeypatch.setattr(
        j1_fetch,
        "_request_public",
        lambda *_args, **_kwargs: (
            200,
            {"content-type": "application/pdf"},
            b"%PDF-1.4\nscan",
            "https://example.org/bulletin.pdf",
        ),
    )
    monkeypatch.setattr(j1_fetch, "_pdf_to_text", lambda _body: "")
    monkeypatch.setattr(
        j1_ocr,
        "extract_scanned_pdf",
        lambda _body, **_kwargs: {
            "ok": True,
            "text": "OCR public technical bulletin " + "x" * 100,
            "kind": "pdf",
            "extraction_method": "pdf_ocr",
            "pages_processed": 1,
        },
    )
    result = j1_fetch.fetch_document("https://example.org/bulletin.pdf")
    assert result["ok"] is True
    assert result["kind"] == "pdf"
    assert result["extraction_method"] == "pdf_ocr"


def test_renderer_returns_only_bounded_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        renderer,
        "_run_chromium",
        lambda *_args, **_kwargs: (
            b"<title>Dynamic page</title><p>Useful rendered details " + b"x" * 100 + b"</p>",
            "",
        ),
    )
    result = renderer.render("https://example.org/page", max_chars=100)
    assert result["ok"] is True
    assert result["kind"] == "browser"
    assert result["extraction_method"] == "browser_dom"
    assert len(result["text"]) <= 100
    assert "<p>" not in result["text"]


def test_renderer_uses_same_robots_agent_and_disables_cookie_persistence(tmp_path: Path) -> None:
    argv = renderer._chromium_argv(
        binary="/usr/bin/chromium",
        url="https://example.org/page",
        profile_dir=str(tmp_path),
        proxy_url="http://172.31.250.3:18890",
        wait_ms=500,
    )
    assert f"--user-agent={j1_fetch.USER_AGENT}" in argv
    assert "--disable-quic" in argv and "--incognito" in argv and "--no-sandbox" in argv
    renderer._write_ephemeral_preferences(str(tmp_path))
    preferences = json.loads((tmp_path / "Default" / "Preferences").read_text(encoding="utf-8"))
    assert preferences["profile"]["default_content_setting_values"]["cookies"] == 2
    assert preferences["profile"]["default_content_setting_values"]["automatic_downloads"] == 2


def test_renderer_gives_chromium_private_writable_runtime_paths(tmp_path: Path) -> None:
    environment = renderer._chromium_environment(str(tmp_path))
    for name in ("HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        path = Path(environment[name])
        assert path.is_relative_to(tmp_path)
        assert path.is_dir()
        assert path.stat().st_mode & 0o777 == 0o700


def test_proxy_rejects_mixed_dns_and_wildcard_without_renderer_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        proxy.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ],
    )
    with pytest.raises(proxy.ProxyRequestError, match="unsafe_dns_answer"):
        proxy.resolve_public_addresses("example.org", 443)
    with pytest.raises(ValueError, match="allowed_peer_required"):
        proxy.serve(host="0.0.0.0", port=18_891)
    assert proxy._allowed_peer("172.31.250.2", proxy._parse_allowed_peers(["172.31.250.2/32"]))
    assert not proxy._allowed_peer("172.31.250.4", proxy._parse_allowed_peers(["172.31.250.2/32"]))


def test_ocr_fails_closed_when_runtime_is_not_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(j1_ocr, "_ocr_languages_available", lambda: False)
    result = j1_ocr.extract_scanned_pdf(b"%PDF-1.4\npublic scan")
    assert result == {"ok": False, "error": "ocr_unavailable", "retryable": True, "extraction_method": "pdf_ocr"}


def test_ocr_transfers_pdf_to_unprivileged_parser_directory(tmp_path: Path) -> None:
    if os.geteuid() != 0:
        pytest.skip("ownership boundary is meaningful only for the root J1 worker")
    workdir = tmp_path / "ocr"
    workdir.mkdir()
    source = workdir / "source.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    j1_ocr._prepare_workdir(workdir, source)
    assert source.stat().st_uid == 65_534
    assert source.stat().st_gid == 65_534


def test_ocr_pipeline_marks_scanned_pdf_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(j1_ocr, "_ocr_languages_available", lambda: True)

    def fake_run(argv: list[str], *, timeout: float, stdout: object = None) -> bool:
        _ = timeout
        if argv[0] == "pdftoppm":
            Path(argv[-1]).parent.joinpath("page-01.jpg").write_bytes(b"\xff\xd8" + b"x" * 100)
        else:
            assert hasattr(stdout, "write")
            stdout.write(b"Scanned public technical evidence " + b"x" * 100)  # type: ignore[union-attr]
        return True

    monkeypatch.setattr(j1_ocr, "_run", fake_run)
    result = j1_ocr.extract_scanned_pdf(b"%PDF-1.4\npublic scan", max_pages=1)
    assert result["ok"] is True
    assert result["extraction_method"] == "pdf_ocr"
    assert result["pages_processed"] == 1


def test_ocr_timeout_is_explicit_after_render(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(j1_ocr, "_ocr_languages_available", lambda: True)

    def render_only(argv: list[str], **_kwargs: object) -> bool:
        if argv[0] == "pdftoppm":
            Path(argv[-1]).parent.joinpath("page-01.jpg").write_bytes(b"\xff\xd8" + b"x" * 100)
        return True

    monkeypatch.setattr(j1_ocr, "_run", render_only)
    ticks = iter((0.0, 0.0, 31.0))
    monkeypatch.setattr(j1_ocr.time, "monotonic", lambda: next(ticks))
    result = j1_ocr.extract_scanned_pdf(b"%PDF-1.4\npublic scan", max_pages=1, timeout_seconds=30)
    assert result == {"ok": False, "error": "ocr_timeout", "retryable": True, "extraction_method": "pdf_ocr"}


def test_browser_image_includes_renderer_source_dependencies() -> None:
    dockerfile = (ROOT / "deploy/j1-browser/Dockerfile").read_text(encoding="utf-8")

    assert "autostop_manager/j1_sources.py" in dockerfile
    assert "autostop_manager/j1_fetch.py" in dockerfile
    assert "autostop_manager/j1_browser.py" in dockerfile


def test_browser_compose_has_bounded_control_network_and_no_public_port() -> None:
    compose = (ROOT / "deploy/j1-browser/docker-compose.yml").read_text(encoding="utf-8")
    unit = (ROOT / "deploy/systemd/autostop-j1-browser.service").read_text(encoding="utf-8")
    installer = (ROOT / "scripts/install-j1-browser-stack.sh").read_text(encoding="utf-8")
    assert "internal: true" in compose
    assert "172.31.250.2/32" in compose
    assert "172.31.250.3:18890" in compose
    assert "ports:" not in compose
    assert "j1_browser_egress" in compose
    assert "init: true" in compose
    assert "pids_limit: 128" in compose
    assert "soft: 1024" in compose and "hard: 1024" in compose
    assert "RuntimeDirectory=autostop-j1-browser-attestation autostop-j1-browser-docker" in unit
    assert "RuntimeDirectory=autostop-j1-browser autostop-j1-browser-attestation" not in unit
    assert "ExecStartPre=+/usr/bin/install -d -m 0710 -o 10001 -g 10001 /run/autostop-j1-browser" in unit
    assert "ExecStartPre=+/usr/bin/rm -f -- /run/autostop-j1-browser/renderer.sock" in unit
    assert "Environment=HOME=/run/autostop-j1-browser-docker" in unit
    assert "Environment=DOCKER_CONFIG=/run/autostop-j1-browser-docker" in unit
    assert "ExecStartPre=/usr/bin/install -d -m 0700 -o root -g root /run/autostop-j1-browser-docker" in unit
    assert "ProtectHome=yes" in unit
    assert unit.index(
        "ExecStartPre=/usr/bin/install -d -m 0700 -o root -g root /run/autostop-j1-browser-docker"
    ) < unit.index("ExecStart=/usr/bin/docker compose")
    assert unit.index(
        "ExecStartPre=+/usr/bin/install -d -m 0710 -o 10001 -g 10001 /run/autostop-j1-browser"
    ) < unit.index("ExecStart=/usr/bin/docker compose")
    assert "isolation-ready" in unit
    assert "docker compose" in unit
    assert "config --quiet" in installer
    assert "isolation_attestation_required" in installer


def test_browser_attestation_uses_a_sealed_directory_and_release_verifier() -> None:
    unit = (ROOT / "deploy/systemd/autostop-j1-browser.service").read_text(encoding="utf-8")
    installer = (ROOT / "scripts/install-j1-browser-stack.sh").read_text(encoding="utf-8")

    assert "RuntimeDirectory=autostop-j1-browser-attestation autostop-j1-browser-docker" in unit
    assert "install -d -m 0700 -o root -g root /run/autostop-j1-browser-attestation" in unit
    assert "/run/autostop-j1-browser-attestation/isolation-ready" in unit
    assert "--verify" in installer
    assert '"${RUNTIME_PYTHON}" -m "${VERIFIER_MODULE}" attest' in installer
    assert "j1_browser_isolation_attested=true" in installer
