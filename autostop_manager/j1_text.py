"""Decode bounded J1 document bytes using their HTTP Content-Type charset."""

from email.parser import HeaderParser
from email.policy import HTTP


def decode_http_text(body: bytes, content_type: str) -> str:
    """Leave size limits, extraction and redaction to the document caller."""
    headers = HeaderParser(policy=HTTP).parsestr(f"Content-Type: {content_type}\n\n")
    charset = headers.get_content_charset() or "utf-8"
    try:
        return body.decode(charset, "replace")
    except LookupError:
        return body.decode("utf-8", "replace")
