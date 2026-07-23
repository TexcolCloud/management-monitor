from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit


REDACTED = "<redacted>"
MAX_DIAGNOSTIC_LENGTH = 2000

_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_IDENTIFIER = re.compile(r"\b(?:ou|oc)_[A-Za-z0-9_-]+\b")
_HTTP_URL = re.compile(r"(?i)\b(?:https?|wss?)://[^\s\"'<>]+")
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:access[_-]?token|token|app[_-]?secret|password|signature|"
    r"ticket|code|auth(?:orization)?|session|jwt|key)=)[^&#\s]+"
)
_KEY_VALUE_SECRET = re.compile(
    r"(?ix)"
    r"((?:[\"']?(?:authorization|cookie|set-cookie|token|access_token|"
    r"app_secret|password|open_id)[\"']?)\s*[:=]\s*)"
    r"(?:[\"'][^\"']*[\"']|[^\s,;}]+)"
)


def _without_url_credentials(match: re.Match[str]) -> str:
    try:
        parsed = urlsplit(match.group(0))
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except ValueError:
        return REDACTED


def redact_text(value: Any, limit: int | None = None) -> str:
    text = str(value)
    text = _HTTP_URL.sub(_without_url_credentials, text)
    text = _BEARER.sub("Bearer " + REDACTED, text)
    text = _QUERY_SECRET.sub(lambda match: match.group(1) + REDACTED, text)
    text = _KEY_VALUE_SECRET.sub(lambda match: match.group(1) + REDACTED, text)
    text = _IDENTIFIER.sub(REDACTED, text)
    if limit is not None and len(text) > limit:
        return text[:limit] + "..."
    return text


def diagnostic_error(error: BaseException | str) -> str:
    return redact_text(error, limit=MAX_DIAGNOSTIC_LENGTH)
