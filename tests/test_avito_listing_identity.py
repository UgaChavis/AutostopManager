from __future__ import annotations

from urllib.parse import quote

import pytest

from autostop_manager.avito_listing_identity import clean_avito_listing_url

_ID = "8386499447"
_BASE = "https://www.avito.ru/krasnoyarsk/zapchasti_i_aksessuary/"


@pytest.mark.parametrize(
    "path",
    [
        f"bamper_toyota_camry_{_ID}",
        "bamper_89991112233",  # Public ad IDs are not telephone numbers or a title's VIN serial.
        "item/89991112233",
        "item/12345678901234567",
        f"бампер_{_ID}",
        f"bamper_{_ID}/",
    ],
)
def test_structured_listing_names_and_public_ids_are_preserved(path):
    url = _BASE + path
    assert clean_avito_listing_url(url) == url.rstrip("/")


def test_permitted_http_and_tracking_are_normalized_after_validation():
    url = f"http://WWW.AVITO.RU:80/item/{_ID}/?utm_source=manager#details"
    assert clean_avito_listing_url(url, allow_http=True, allow_tracking=True) == f"https://www.avito.ru/item/{_ID}"
    assert clean_avito_listing_url(f"https://www.avito.ru:443/item/{_ID}") == f"https://www.avito.ru/item/{_ID}"


@pytest.mark.parametrize(
    "url",
    [
        f"http://www.avito.ru/item/{_ID}",
        f"https://www.avito.ru/item/{_ID}?utm_source=manager",
        f"https://www.avito.ru/item/{_ID}#details",
        f"https://avito.ru.evil.test/item/{_ID}",
        f"https://example.org/item/{_ID}",
        f"https://user:pass@www.avito.ru/item/{_ID}",
        f"https://www.avito.ru:80/item/{_ID}",
        f"https://www.avito.ru:1234/item/{_ID}",
        f"https://bad_host.avito.ru/item/{_ID}",
        f"https://www.avito.ru/../item/{_ID}",
        f"https://www.avito.ru/item/{_ID}\n",
        "https://www.avito.ru/",
        "https://www.avito.ru/item/12345",
        "https://www.avito.ru/item/١٢٣٤٥٦٧٨",
        "https://[bad-host/item/12345678",
        None,
        8386499447,
    ],
)
def test_invalid_or_noncanonical_listing_urls_are_rejected(url):
    assert clean_avito_listing_url(url) is None


@pytest.mark.parametrize(
    "sensitive",
    [
        "1HGCM82633A004352",
        "1HGCM82633A_004352",
        "1HG_CM82633_A004352",
        "1HGCM82633A/004352",
        "parts@example.org",
        "sample@example.com_detal",
        "+7_900_111_22_33",
        "8_900_111_22_33",
        "api_key=abcdefghijklmno",
        "password=abcdefghijklmno",
        "ghp_" + "a" * 25,
    ],
)
def test_sensitive_listing_path_parts_remain_rejected(sensitive):
    assert clean_avito_listing_url(_BASE + f"bamper_{sensitive}_{_ID}") is None


def test_split_vin_cannot_be_mistaken_for_a_six_digit_listing_id():
    assert clean_avito_listing_url(_BASE + "1HGCM82633A_004352") is None
    assert clean_avito_listing_url(_BASE + "1HG_CM82633A_004352") is None


@pytest.mark.parametrize("component", ["path", "query", "fragment"])
@pytest.mark.parametrize(
    "value", ["1HGCM82633A/004352", "+7_900_111_22_33", "sample@example.com", "token=abcdefghijklmno"]
)
def test_nested_encoding_does_not_hide_sensitive_tracking_or_path(component, value):
    encoded = quote(quote(value, safe=""), safe="")
    if component == "path":
        url = _BASE + f"bamper_{encoded}_{_ID}"
    elif component == "query":
        url = _BASE + f"bamper_{_ID}?tracking={encoded}"
    else:
        url = _BASE + f"bamper_{_ID}#{encoded}"
    assert clean_avito_listing_url(url, allow_tracking=True) is None


def test_vin_in_subdomain_is_rejected_without_removing_host_protection():
    assert clean_avito_listing_url(f"https://1HGCM82633A004352.avito.ru/item/{_ID}") is None
