from __future__ import annotations

from collections.abc import Mapping

import pytest

from autostop_manager import drom_listings


@pytest.fixture(autouse=True)
def _disable_runtime_env_file_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drom_listings, "load_runtime_env", list)
    monkeypatch.delenv(drom_listings.DROM_LISTINGS_ENABLED_ENV, raising=False)


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(drom_listings.DROM_LISTINGS_ENABLED_ENV, "1")
    monkeypatch.setenv("WEBBEE_API_TOKEN", "webbee-test-token")
    monkeypatch.setenv("WEBBEE_DROM_ROBOT_ALIAS", "baza.drom")


def test_enabled_dry_run_builds_baza_parts_search_url_without_credentials_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(drom_listings.DROM_LISTINGS_ENABLED_ENV, "1")
    result = drom_listings.drom_start_parts_search("фильтр масляный", dry_run=True)

    assert result == {
        "ok": True,
        "source": "drom",
        "status": "dry_run",
        "search_url": "https://baza.drom.ru/krasnoyarsk/sell_spare_parts/?query=%D1%84%D0%B8%D0%BB%D1%8C%D1%82%D1%80+%D0%BC%D0%B0%D1%81%D0%BB%D1%8F%D0%BD%D1%8B%D0%B9",
        "limit": 50,
        "page_limit": 3,
        "task_id": None,
        "uid": None,
        "outcome_uncertain": False,
    }


def test_disabled_drom_never_contacts_webbee_even_with_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBBEE_API_TOKEN", "webbee-test-token")
    monkeypatch.setenv("WEBBEE_DROM_ROBOT_ALIAS", "baza.drom")
    monkeypatch.setattr(
        drom_listings,
        "_api_request",
        lambda *_args, **_kwargs: pytest.fail("disabled Drom must not call Webbee"),
    )

    for result in (
        drom_listings.drom_start_parts_search("фильтр масляный"),
        drom_listings.drom_start_parts_search("фильтр масляный", dry_run=True),
        drom_listings.drom_get_parts_search(12, "run-12"),
    ):
        assert result["ok"] is False
        assert result["status"] == "disabled"
        assert result["error"] == "webbee_disabled"


@pytest.mark.parametrize(
    "query",
    [
        "ZZZ00000000000000",
        "+7 999 123-45-67",
        "buyer@example.org",
        "https://example.org/list",
        "x" * (drom_listings.MAX_QUERY_CHARS + 1),
    ],
)
def test_sensitive_or_unbounded_search_query_is_rejected(query: str) -> None:
    result = drom_listings.drom_start_parts_search(query, dry_run=True)

    assert result["ok"] is False
    assert result["error"] == "search_query_invalid_or_sensitive"
    assert query not in str(result)


@pytest.mark.parametrize(
    ("region", "limit", "error"),
    [
        ("../auto.drom.ru", 50, "region_invalid"),
        ("Красноярск", 50, "region_invalid"),
        ("krasnoyarsk", 0, "limit_out_of_range"),
        ("krasnoyarsk", drom_listings.MAX_LIMIT + 1, "limit_out_of_range"),
    ],
)
def test_region_and_result_limit_are_bounded(region: str, limit: int, error: str) -> None:
    result = drom_listings.drom_start_parts_search("амортизатор", region=region, limit=limit, dry_run=True)

    assert result["ok"] is False
    assert result["error"] == error


@pytest.mark.parametrize("page_limit", [0, drom_listings.MAX_PAGE_LIMIT + 1])
def test_page_limit_is_bounded(page_limit: int) -> None:
    result = drom_listings.drom_start_parts_search("амортизатор", page_limit=page_limit, dry_run=True)

    assert result["ok"] is False
    assert result["error"] == "page_limit_out_of_range"


def test_start_queues_task_and_uses_header_auth_and_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    calls: list[tuple[str, str, str, Mapping[str, object] | None, Mapping[str, str | int] | None]] = []

    def fake_request(method, path, *, token, payload=None, query=None):
        calls.append((method, path, token, payload, query))
        if method == "POST":
            return 200, {"id": 812}
        return 204, None

    monkeypatch.setattr(drom_listings, "_api_request", fake_request)

    result = drom_listings.drom_start_parts_search("ступица", limit=25, page_limit=4)

    assert result["ok"] is True
    assert result["status"] == "queued"
    assert result["task_id"] == 812
    assert result["uid"].startswith("autostop-")
    assert result["idempotent"] is False
    assert len(calls) == 2
    method, path, token, payload, _query = calls[0]
    assert (method, path, token) == ("POST", "/webbee-api/v1.0/tasks", "webbee-test-token")
    assert isinstance(payload, Mapping)
    assert payload["robot"] == "baza.drom"
    assert payload["urls"] == result["search_url"]
    assert payload["settings"] == {"elementCountLimit": 25, "pageCountLimit": 4}
    assert calls[1][0:2] == ("PATCH", "/webbee-api/v1.0/tasks/812/start")
    assert calls[1][4] == {"uid": result["uid"]}


def test_uncertain_create_reconciles_exact_task_without_duplicate_post_or_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    calls: list[tuple[str, str, Mapping[str, object] | None, Mapping[str, str | int] | None]] = []
    create_body: dict[str, object] = {}

    def fake_request(method, path, *, token, payload=None, query=None):
        calls.append((method, path, payload, query))
        if method == "POST":
            create_body.update(payload or {})
            raise TimeoutError("private provider response")
        if method == "GET" and path == "/webbee-api/v1.0/tasks":
            return 200, {"items": [{"id": 23, "name": create_body["name"], "urls": create_body["urls"]}]}
        if method == "PATCH":
            return 204, None
        raise AssertionError((method, path))

    monkeypatch.setattr(drom_listings, "_api_request", fake_request)

    result = drom_listings.drom_start_parts_search("ступица", region="omsk")

    assert result["status"] == "queued"
    assert result["task_id"] == 23
    assert [call[0] for call in calls].count("POST") == 1
    assert [call[0] for call in calls].count("PATCH") == 1
    assert len([call for call in calls if call[1] == "/webbee-api/v1.0/tasks"]) == 2


def test_uncertain_create_without_reconciliation_is_returned_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    calls: list[str] = []

    def fake_request(method, path, *, token, payload=None, query=None):
        calls.append(method)
        if method == "POST":
            raise TimeoutError("private provider response")
        return 200, {"items": []}

    monkeypatch.setattr(drom_listings, "_api_request", fake_request)

    result = drom_listings.drom_start_parts_search("ступица")

    assert result["ok"] is False
    assert result["status"] == "uncertain"
    assert result["error"] == "webbee_task_creation_uncertain"
    assert result["outcome_uncertain"] is True
    assert calls == ["POST", "GET"]
    assert "private provider response" not in str(result)


def test_uncertain_start_is_reconciled_by_uid_without_second_paid_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    calls: list[tuple[str, str, Mapping[str, str | int] | None]] = []

    def fake_request(method, path, *, token, payload=None, query=None):
        calls.append((method, path, query))
        if method == "POST":
            return 200, {"id": 91}
        if method == "PATCH":
            raise TimeoutError("private provider response")
        if method == "GET":
            return 200, {"startedAt": None, "progress": {"processed": 0, "total": 12}}
        raise AssertionError((method, path))

    monkeypatch.setattr(drom_listings, "_api_request", fake_request)

    result = drom_listings.drom_start_parts_search("ступица")

    assert result["ok"] is True
    assert result["status"] == "queued"
    assert result["task_id"] == 91
    assert result["progress"] == {"processed": 0, "total": 12}
    assert len([call for call in calls if call[0] == "PATCH"]) == 1
    assert calls[-1][2] == {"uid": result["uid"]}


@pytest.mark.parametrize(
    ("status_payload", "expected_status"),
    [
        ({"startedAt": None, "progress": {"total": 4}}, "queued"),
        ({"startedAt": "2026-09-27 11:00:00+00", "progress": {"processed": 1, "total": 4}}, "running"),
    ],
)
def test_get_preserves_queued_and_running_states(
    monkeypatch: pytest.MonkeyPatch, status_payload: dict[str, object], expected_status: str
) -> None:
    _configure(monkeypatch)
    calls: list[str] = []

    def fake_request(method, path, *, token, payload=None, query=None):
        calls.append(path)
        return 200, status_payload

    monkeypatch.setattr(drom_listings, "_api_request", fake_request)

    result = drom_listings.drom_get_parts_search(17, "run-17")

    assert result["ok"] is True
    assert result["status"] == expected_status
    assert result["listings"] == []
    assert len(calls) == 1


def test_get_complete_normalizes_listing_without_confirming_stock_or_fitment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    calls: list[str] = []

    def fake_request(method, path, *, token, payload=None, query=None):
        calls.append(path)
        if path.endswith("/status"):
            return 200, {
                "startedAt": "2026-09-27 11:00:00+00",
                "completedAt": "2026-09-27 11:01:00+00",
                "progress": {"processed": 1, "success": 1, "total": 1},
            }
        return 200, {
            "data": [
                {
                    "ID": 234,
                    "Ссылка на объект": "https://baza.drom.ru/krasnoyarsk/sell_spare_parts/part/234/",
                    "Название": "Ступица передняя",
                    "Описание": "Подходит на Corolla",
                    "Цена": "от 1 250 руб.",
                    "Название города": "Омск",
                    "Наличие": "Под заказ",
                    "Название продавца": "+7 999 123-45-67",
                    "Информация о доставке": "Доставка в ТК",
                    "Дата создания": "сегодня в 10:00",
                    "Время сбора данных": "2026-09-27T11:01:00Z",
                }
            ]
        }

    monkeypatch.setattr(drom_listings, "_api_request", fake_request)

    result = drom_listings.drom_get_parts_search(234, "run-234")

    assert result["ok"] is True
    assert result["status"] == "complete"
    assert result["count"] == 1
    listing = result["listings"][0]
    assert listing == {
        "source": "drom",
        "listing_id": "234",
        "url": "https://baza.drom.ru/krasnoyarsk/sell_spare_parts/part/234/",
        "title": "Ступица передняя",
        "description": "Подходит на Corolla",
        "price_rub": 1250,
        "price_text": "от 1 250 руб.",
        "price_qualifier": "from",
        "city": "Омск",
        "condition": None,
        "seller": {
            "name": "[redacted]",
            "type": None,
            "rating": None,
            "reviews_count": None,
            "reviews": None,
        },
        "delivery": {"available": None, "label": "Доставка в ТК"},
        "availability": "Под заказ",
        "published_at": "сегодня в 10:00",
        "observed_at": "2026-09-27T11:01:00Z",
        "status": "lead",
        "fitment_confirmed": False,
        "availability_confirmed": False,
    }
    assert calls == ["/webbee-api/v1.0/tasks/234/status", "/webbee-api/v1.0/tasks/234/result/json"]
    assert "+7 999 123-45-67" not in str(result)


@pytest.mark.parametrize("result_payload", [[], {"data": []}])
def test_get_complete_accepts_empty_results(monkeypatch: pytest.MonkeyPatch, result_payload: object) -> None:
    _configure(monkeypatch)
    responses = iter(
        [
            (200, {"startedAt": "2026-09-27", "completedAt": "2026-09-27", "progress": {"total": 0}}),
            (200, result_payload),
        ]
    )
    monkeypatch.setattr(drom_listings, "_api_request", lambda *_args, **_kwargs: next(responses))

    result = drom_listings.drom_get_parts_search(12, "run-12")

    assert result["ok"] is True
    assert result["status"] == "complete"
    assert result["listings"] == []
    assert result["count"] == 0


def test_get_skips_incomplete_or_non_baza_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    responses = iter(
        [
            (200, {"startedAt": "2026-09-27", "completedAt": "2026-09-27"}),
            (
                200,
                [
                    {"Название": "No link"},
                    {"Название": "External", "url": "https://auto.drom.ru/car/1"},
                    {
                        "Название": "Valid",
                        "url": "https://baza.drom.ru/krasnoyarsk/sell_spare_parts/part/1/",
                    },
                ],
            ),
        ]
    )
    monkeypatch.setattr(drom_listings, "_api_request", lambda *_args, **_kwargs: next(responses))

    result = drom_listings.drom_get_parts_search(12, "run-12")

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["skipped_incomplete_listing_count"] == 2
    assert result["listings"][0]["url"].startswith("https://baza.drom.ru/")


def test_get_falls_back_to_numeric_url_id_and_deduplicates_by_id_and_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    responses = iter(
        [
            (200, {"startedAt": "2026-09-27", "completedAt": "2026-09-27"}),
            (
                200,
                [
                    {
                        "Название": "Ступица",
                        "url": "https://baza.drom.ru/krasnoyarsk/sell_spare_parts/part/234/",
                    },
                    {
                        "ID": "234",
                        "Название": "Дубликат ID",
                        "url": "https://baza.drom.ru/omsk/sell_spare_parts/part/999/",
                    },
                    {
                        "ID": "345",
                        "Название": "Дубликат URL",
                        "url": "https://baza.drom.ru/krasnoyarsk/sell_spare_parts/part/234/",
                    },
                ],
            ),
        ]
    )
    monkeypatch.setattr(drom_listings, "_api_request", lambda *_args, **_kwargs: next(responses))

    result = drom_listings.drom_get_parts_search(12, "run-12")

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["listings"][0]["listing_id"] == "234"
    assert result["duplicate_listing_count"] == 2


def test_get_recovers_negative_webbee_id_from_baza_g_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    responses = iter(
        [
            (200, {"startedAt": "2026-09-27", "completedAt": "2026-09-27"}),
            (
                200,
                [
                    {
                        "ID": -16400011444,
                        "Название": "Фильтр масляный",
                        "Ссылка на объект": "https://baza.drom.ru/lobnya/sell_spare_parts/filtr-masljanyj-g16400011444.html",
                        "Цена": "305",
                    },
                    {
                        "Название": "Повтор из другого региона",
                        "Ссылка на объект": "https://baza.drom.ru/moskva/sell_spare_parts/filtr-masljanyj-g16400011444.html",
                    },
                ],
            ),
        ]
    )
    monkeypatch.setattr(drom_listings, "_api_request", lambda *_args, **_kwargs: next(responses))

    result = drom_listings.drom_get_parts_search(12, "run-12")

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["listings"][0]["listing_id"] == "-16400011444"
    assert result["listings"][0]["price_rub"] == 305
    assert result["duplicate_listing_count"] == 1


def test_failed_provider_task_does_not_fetch_results(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        drom_listings,
        "_api_request",
        lambda _method, path, **_kwargs: (
            calls.append(path) or 200,
            {"startedAt": "2026-09-27", "completedAt": "2026-09-27", "progress": {"success": 0, "withError": 2}},
        ),
    )

    result = drom_listings.drom_get_parts_search(12, "run-12")

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["error"] == "webbee_task_failed"
    assert calls == ["/webbee-api/v1.0/tasks/12/status"]


def test_provider_errors_are_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(drom_listings, "_api_request", lambda *_args, **_kwargs: (401, {"error": "secret-token-body"}))

    result = drom_listings.drom_get_parts_search(12, "run-12")

    assert result["ok"] is False
    assert result["error"] == "webbee_auth_failed"
    assert "secret-token-body" not in str(result)


@pytest.mark.parametrize(
    ("http_status", "expected_error"),
    [
        (401, "webbee_auth_failed"),
        (403, "webbee_access_denied"),
        (429, "webbee_quota_or_rate_limited"),
    ],
)
def test_status_read_errors_distinguish_auth_access_and_quota(
    monkeypatch: pytest.MonkeyPatch, http_status: int, expected_error: str
) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(
        drom_listings,
        "_api_request",
        lambda *_args, **_kwargs: (http_status, {"error": "private-provider-body"}),
    )

    result = drom_listings.drom_get_parts_search(12, "run-12")

    assert result["ok"] is False
    assert result["error"] == expected_error
    assert "private-provider-body" not in str(result)


@pytest.mark.parametrize(
    ("http_status", "expected_error"),
    [
        (401, "webbee_auth_failed"),
        (403, "webbee_access_denied"),
        (429, "webbee_quota_or_rate_limited"),
    ],
)
def test_result_download_errors_distinguish_auth_access_and_quota(
    monkeypatch: pytest.MonkeyPatch, http_status: int, expected_error: str
) -> None:
    _configure(monkeypatch)
    responses = iter(
        [
            (200, {"startedAt": "2026-09-27", "completedAt": "2026-09-27"}),
            (http_status, {"error": "private-provider-body"}),
        ]
    )
    monkeypatch.setattr(drom_listings, "_api_request", lambda *_args, **_kwargs: next(responses))

    result = drom_listings.drom_get_parts_search(12, "run-12")

    assert result["ok"] is False
    assert result["error"] == expected_error
    assert result["provider_status"] == "complete"
    assert "private-provider-body" not in str(result)
