"""Validate an Avito listing URL without treating its public ad ID as prose."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from .j1_fetch import _API_SECRET, _EMAIL, _JWT, _PHONE, _SECRET

_AD_PATH_ID = re.compile(r"(?:_|/)([0-9]{6,20})/?\Z")
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_VIN_TOKEN = re.compile(r"[A-HJ-NPR-Z0-9]+\Z", re.I)
_VIN_SEPARATORS = re.compile(r"[ ._/\\-]+")
_URL_FIELDS = re.compile(r"[?&;=#]+")
# URL underscores delimit slugs but are word characters to regex \b. Keep
# token contents intact while recognizing opaque secrets beside that delimiter.
_URL_OPAQUE_SECRETS = tuple(
    re.compile(pattern.pattern.replace(r"\b", ""), pattern.flags) for pattern in (_JWT, _API_SECRET)
)


def _decoded(value: str) -> str:
    for _ in range(64):
        next_value = unquote(value)
        if next_value == value:
            break
        value = next_value
    return value


def _has_vin_groups(value: str) -> bool:
    """Consider whole tokens, never a suffix starting inside a title word."""

    for field in _URL_FIELDS.split(value):
        tokens = [token for token in _VIN_SEPARATORS.split(field) if token]
        for start, token in enumerate(tokens):
            if not _VIN_TOKEN.fullmatch(token):
                continue
            candidate = ""
            for end in range(start, len(tokens)):
                part = tokens[end]
                if not _VIN_TOKEN.fullmatch(part):
                    break
                candidate += part
                if len(candidate) > 17:
                    break
                if len(candidate) == 17 and (end == start or sum(c.isdigit() for c in candidate) >= 5):
                    return True
    return False


def _suffix_is_vin_serial(prefix: str, listing_id: str) -> bool:
    """Reject an ambiguous split VIN with its six-digit serial used as an ID.

    The public ID is otherwise excluded from VIN/phone matching. Matching all
    title-plus-ID combinations would reject ordinary eleven-digit ad IDs.
    """

    if len(listing_id) != 6:
        return False
    tokens = [token for token in _VIN_SEPARATORS.split(prefix) if token]
    candidate = ""
    for token in reversed(tokens):
        if not _VIN_TOKEN.fullmatch(token):
            break
        candidate = token + candidate
        if len(candidate) >= 11:
            return len(candidate) == 11
    return False


def clean_avito_listing_url(value: Any, *, allow_http: bool = False, allow_tracking: bool = False) -> str | None:
    """Return a canonical HTTPS listing URL, or reject invalid/sensitive input.

    Tracking is checked before it is removed. The numeric ID suffix has its
    own namespace and is not interpreted as a telephone number or appended
    to title fragments for VIN recognition. Six-digit IDs with an adjacent
    eleven-character VIN prefix are conservatively rejected as ambiguous.
    """

    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2048
        or not isinstance(allow_http, bool)
        or not isinstance(allow_tracking, bool)
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return None
    try:
        parsed = urlsplit(value.strip())
        host = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in ({"http", "https"} if allow_http else {"https"})
        or parsed.username is not None
        or parsed.password is not None
        or port not in ({None, 80} if parsed.scheme == "http" else {None, 443})
        or not (host == "avito.ru" or host.endswith(".avito.ru"))
        or len(host) > 253
        or any(not _HOST_LABEL.fullmatch(label) for label in host.split("."))
        or (not allow_tracking and (parsed.query or parsed.fragment))
        or "\\" in parsed.path
    ):
        return None
    match = _AD_PATH_ID.search(parsed.path)
    if match is None:
        return None
    prefix = _decoded(parsed.path[: match.start(1)])
    if any(segment in {".", ".."} for segment in prefix.split("/")) or _suffix_is_vin_serial(prefix, match.group(1)):
        return None
    # Share the existing non-VIN redaction patterns; use URL-specific VIN
    # grouping here rather than changing the prose rules in J1 globally.
    parts = (host, prefix, _decoded(parsed.query), _decoded(parsed.fragment))
    for part in parts:
        if (
            any(ord(char) < 32 or ord(char) == 127 for char in part)
            or _has_vin_groups(part)
            or any(pattern.search(part) for pattern in _URL_OPAQUE_SECRETS)
            or any(
                pattern.search(candidate)
                for candidate in (part, part.replace("_", " "))
                for pattern in (_EMAIL, _PHONE, _SECRET, _JWT, _API_SECRET)
            )
        ):
            return None
    return urlunsplit(("https", host, parsed.path.rstrip("/"), "", ""))
