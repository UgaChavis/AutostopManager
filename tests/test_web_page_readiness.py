from __future__ import annotations

from autostop_manager import web_page_readiness as module
from autostop_manager.web_page_readiness import build_web_page_readiness


def test_web_page_readiness_dry_run_redacts_env_values(tmp_path, monkeypatch):
    for name in (
        "AUTOSTOP_CRAWL4AI_BASE_URL",
        "CRAWL4AI_BASE_URL",
        "AUTOSTOP_CRAWL4AI_API_TOKEN",
        "CRAWL4AI_API_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "AUTOSTOP_CRAWL4AI_BASE_URL=http://127.0.0.1:11235",
                "AUTOSTOP_CRAWL4AI_API_TOKEN=crawl-secret-token",
            ]
        ),
        encoding="utf-8",
    )

    report = build_web_page_readiness(env_paths=(env_file,))

    assert report["ok"] is True
    assert report["schema"] == "WebPageReadinessV1"
    assert report["summary"]["configured_count"] == 1
    assert report["summary"]["primary_configured"] is True
    assert "crawl-secret-token" not in str(report)


def test_web_page_readiness_reports_missing_crawl4ai_env(monkeypatch, tmp_path):
    for name in (
        "AUTOSTOP_CRAWL4AI_BASE_URL",
        "CRAWL4AI_BASE_URL",
        "AUTOSTOP_CRAWL4AI_API_TOKEN",
        "CRAWL4AI_API_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    report = build_web_page_readiness(env_paths=(tmp_path / "missing.env",))

    assert report["summary"]["extractor_count"] == 1
    assert report["summary"]["configured_count"] == 0
    result = report["results"][0]
    assert result["provider"] == "crawl4ai"
    assert "AUTOSTOP_CRAWL4AI_BASE_URL" in result["missing_env_names"]
    assert "AUTOSTOP_CRAWL4AI_API_TOKEN" in result["missing_env_names"]


def test_web_page_readiness_live_probe_uses_crawl4ai_md_endpoint(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "AUTOSTOP_CRAWL4AI_BASE_URL=http://127.0.0.1:11235",
                "AUTOSTOP_CRAWL4AI_API_TOKEN=crawl-secret-token",
            ]
        ),
        encoding="utf-8",
    )
    requests = []

    def fake_read_json_response(request):
        requests.append(request)
        return {"success": True, "markdown": "# Example Domain"}

    monkeypatch.setattr(module, "_read_json_response", fake_read_json_response)
    monkeypatch.setattr(
        module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(module.socket.AF_INET, module.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )

    report = build_web_page_readiness(
        provider="crawl4ai",
        mode="live-readonly",
        env_paths=(env_file,),
        url="https://example.com/",
    )

    assert report["ok"] is True
    assert report["results"][0]["live_readonly_status"] == "ready"
    assert requests[0].full_url == "http://127.0.0.1:11235/md"
    assert requests[0].get_header("Authorization") == "Bearer crawl-secret-token"


def test_web_page_readiness_live_probe_rejects_private_url(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "AUTOSTOP_CRAWL4AI_BASE_URL=http://127.0.0.1:11235",
                "AUTOSTOP_CRAWL4AI_API_TOKEN=crawl-secret-token",
            ]
        ),
        encoding="utf-8",
    )

    report = build_web_page_readiness(
        provider="crawl4ai",
        mode="live-readonly",
        env_paths=(env_file,),
        url="http://127.0.0.1:41731/status",
    )

    assert report["results"][0]["live_readonly_status"] == "error"
    assert "Local or private URLs" in report["results"][0]["error"]
    assert "crawl-secret-token" not in str(report)


def test_web_page_readiness_rejects_unknown_provider():
    report = build_web_page_readiness(provider="missing", mode="dry-run")

    assert report["ok"] is False
    assert report["error"] == "unknown provider"


def test_public_url_guard_rejects_legacy_loopback_and_dns_aliases(monkeypatch):
    def fake_getaddrinfo(host, *_args, **_kwargs):
        address = "127.0.0.1" if host in {"127.1", "0177.0.0.1", "localtest.me"} else "93.184.216.34"
        return [(module.socket.AF_INET, module.socket.SOCK_STREAM, 6, "", (address, 443))]

    monkeypatch.setattr(module.socket, "getaddrinfo", fake_getaddrinfo)

    for url in ("http://127.1/", "http://0177.0.0.1/", "https://localtest.me/"):
        try:
            module._validated_public_http_url(url)
        except ValueError as exc:
            assert "Local or private URLs" in str(exc)
        else:  # pragma: no cover - explicit regression failure branch
            raise AssertionError(f"private URL was accepted: {url}")


def test_public_url_guard_rejects_mixed_public_private_dns(monkeypatch):
    monkeypatch.setattr(
        module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (module.socket.AF_INET, module.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (module.socket.AF_INET, module.socket.SOCK_STREAM, 6, "", ("10.0.0.7", 443)),
        ],
    )

    try:
        module._validated_public_http_url("https://mixed.example/")
    except ValueError as exc:
        assert "Local or private URLs" in str(exc)
    else:  # pragma: no cover - explicit regression failure branch
        raise AssertionError("mixed DNS result was accepted")
