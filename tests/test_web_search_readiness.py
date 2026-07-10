from __future__ import annotations

import io

from autostop_manager import web_search_readiness as module
from autostop_manager.web_search_readiness import build_web_search_readiness


def test_web_search_readiness_dry_run_redacts_env_values(tmp_path, monkeypatch):
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CUSTOM_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CUSTOM_SEARCH_CX", raising=False)
    monkeypatch.delenv("AUTOSTOP_SEARXNG_BASE_URL", raising=False)
    monkeypatch.delenv("SEARXNG_BASE_URL", raising=False)
    monkeypatch.delenv("AUTOSTOP_MARGINALIA_ENABLED", raising=False)
    monkeypatch.delenv("AUTOSTOP_MARGINALIA_API_KEY", raising=False)
    monkeypatch.delenv("MARGINALIA_API_KEY", raising=False)
    monkeypatch.delenv("AUTOSTOP_SEARCH_DISABLED_PROVIDERS", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "BRAVE_SEARCH_API_KEY=brave-secret-value",
                "TAVILY_API_KEY=tavily-secret-value",
                "GOOGLE_CUSTOM_SEARCH_API_KEY=google-secret-value",
                "GOOGLE_CUSTOM_SEARCH_CX=google-cx-secret",
            ]
        ),
        encoding="utf-8",
    )

    report = build_web_search_readiness(env_paths=(env_file,))

    assert report["ok"] is True
    assert report["schema"] == "WebSearchReadinessV1"
    assert report["summary"]["quality_configured_count"] == 3
    assert report["summary"]["fallback_only"] is False
    rendered = str(report)
    assert "brave-secret-value" not in rendered
    assert "tavily-secret-value" not in rendered
    assert "google-secret-value" not in rendered
    assert "google-cx-secret" not in rendered


def test_web_search_readiness_reports_missing_quality_search(monkeypatch, tmp_path):
    for name in (
        "BRAVE_SEARCH_API_KEY",
        "BRAVE_API_KEY",
        "TAVILY_API_KEY",
        "GOOGLE_CUSTOM_SEARCH_API_KEY",
        "GOOGLE_CSE_API_KEY",
        "GOOGLE_CUSTOM_SEARCH_CX",
        "GOOGLE_CSE_CX",
        "GOOGLE_CSE_ID",
        "AUTOSTOP_SEARXNG_BASE_URL",
        "SEARXNG_BASE_URL",
        "AUTOSTOP_MARGINALIA_ENABLED",
        "AUTOSTOP_MARGINALIA_API_KEY",
        "MARGINALIA_API_KEY",
        "AUTOSTOP_SEARCH_DISABLED_PROVIDERS",
    ):
        monkeypatch.delenv(name, raising=False)

    report = build_web_search_readiness(env_paths=(tmp_path / "missing.env",))

    assert report["summary"]["quality_provider_count"] == 5
    assert report["summary"]["quality_configured_count"] == 0
    assert report["summary"]["fallback_available"] is True
    assert report["summary"]["fallback_only"] is True
    missing = {
        result["provider"]: result["missing_env_names"]
        for result in report["results"]
        if result["provider"] != "duckduckgo"
    }
    assert "AUTOSTOP_SEARXNG_BASE_URL" in missing["searxng"]
    assert "AUTOSTOP_MARGINALIA_ENABLED" in missing["marginalia"]
    assert "BRAVE_SEARCH_API_KEY" in missing["brave"]
    assert "TAVILY_API_KEY" in missing["tavily"]
    assert "GOOGLE_CUSTOM_SEARCH_API_KEY" in missing["google_cse"]
    assert "GOOGLE_CUSTOM_SEARCH_CX" in missing["google_cse"]


def test_web_search_readiness_reports_free_provider_config_and_disabled_paid(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOSTOP_SEARXNG_BASE_URL", raising=False)
    monkeypatch.delenv("AUTOSTOP_MARGINALIA_ENABLED", raising=False)
    monkeypatch.delenv("AUTOSTOP_SEARCH_DISABLED_PROVIDERS", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "AUTOSTOP_SEARXNG_BASE_URL=http://127.0.0.1:8890",
                "AUTOSTOP_MARGINALIA_ENABLED=1",
                "AUTOSTOP_SEARCH_DISABLED_PROVIDERS=brave,tavily,google_cse",
            ]
        ),
        encoding="utf-8",
    )

    report = build_web_search_readiness(env_paths=(env_file,))

    by_provider = {item["provider"]: item for item in report["results"]}
    assert by_provider["searxng"]["configured"] is True
    assert by_provider["marginalia"]["configured"] is True
    assert by_provider["brave"]["disabled"] is True
    assert by_provider["tavily"]["disabled"] is True
    assert by_provider["google_cse"]["disabled"] is True
    assert report["summary"]["quality_provider_count"] == 2
    assert report["summary"]["quality_configured_count"] == 2
    assert report["summary"]["disabled_provider_count"] == 3
    assert report["summary"]["fallback_only"] is False


def test_web_search_readiness_rejects_unknown_provider():
    report = build_web_search_readiness(provider="missing", mode="dry-run")

    assert report["ok"] is False
    assert report["error"] == "unknown provider"


def test_web_search_readiness_rejects_unbounded_query_before_live_call():
    report = build_web_search_readiness(query="x" * 241)

    assert report["ok"] is False
    assert "1..240" in report["error"]


def test_web_search_response_reader_rejects_oversized_body(monkeypatch):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        module._NO_REDIRECT_OPENER,
        "open",
        lambda *_args, **_kwargs: Response(b"x" * (module.MAX_RESPONSE_BYTES + 1)),
    )

    request = module.urllib.request.Request("https://example.com/")
    try:
        module._read_text_response(request)
    except ValueError as exc:
        assert "maximum size" in str(exc)
    else:  # pragma: no cover - explicit regression failure branch
        raise AssertionError("oversized response was accepted")
