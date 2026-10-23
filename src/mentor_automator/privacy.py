"""No raw exceptions, HTTP traffic, browser dumps, or credential values are logged."""

import logging
import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


def clean(text: str, limit: int = 6000) -> str:
    text = re.sub(r"https?://[^\s<>]+", "[ссылка удалена]", text, flags=re.I)
    text = re.sub(
        r"(?i)\b(?:sesskey|password|username|token|cookie|authorization)\s*[:=]\s*\S+", "[скрыто]", text
    )
    return text[:limit]


def public_url(url: str, base: str, allowed_path: str, keys: set[str]) -> str:
    u, b = urlsplit(url), urlsplit(base)
    if (u.scheme, u.netloc) != (b.scheme, b.netloc) or u.path != b.path.rstrip("/") + allowed_path:
        raise ValueError("Unexpected navigation")
    query = parse_qs(u.query)
    safe = {k: query[k][0] for k in keys if k in query and query[k][0].isdigit()}
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(safe), ""))


def configure_logging():
    # Exceptions from Playwright/HTTP libraries can include credentials or request bodies.
    # Only application event codes are recorded. No handlers write arbitrary third-party output.
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(logging.NullHandler())
    logging.disable(logging.CRITICAL)
