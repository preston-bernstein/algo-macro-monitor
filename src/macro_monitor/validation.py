"""Input validation shared across the write paths.

Centralises FR-20 (ingest field validation) and FR-18's strict ``observed_date`` check so the
exact same rules apply whether a value arrives via ``collect-rss``, ``ingest``, or ``correlate``.
Every function raises ``ValidationError`` (a ValueError subclass) and writes nothing.
"""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlparse

# A strict YYYY-MM-DD shape; the *calendar* validity is checked separately with date.fromisoformat.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Ticker-like: letters/digits plus the handful of separators real symbols use (BRK.B, RDS-A, ^VIX).
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9.\-^=]{1,15}$")

MAX_URL_LEN = 2048
MAX_TITLE_LEN = 2000
MAX_SYMBOLS = 64


class ValidationError(ValueError):
    """Raised when an input fails validation, before any DB write or subprocess call."""


def validate_observed_date(value: str, *, field_name: str = "observed_date") -> str:
    """FR-18/FR-20: strict YYYY-MM-DD *and* a real calendar date. Returns the normalised string.

    This is the single gate that any externally-derived date passes through before it is used to
    build a query. Shell metacharacters, ``$()``, backticks, etc. all fail the regex here — long
    before the value reaches any SQL parameter or (in a hypothetical remote deployment) subprocess.

    ``field_name`` is the caller's own name for the value being validated (``checked_on``,
    ``since``, ...) — this function is reused well beyond ``observed_date`` itself, and the error
    message should name the field the user actually typed, not this function's own parameter name.
    """
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValidationError(f"{field_name} must match YYYY-MM-DD, got {value!r}")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field_name} is not a real calendar date: {value!r}") from exc
    return parsed.isoformat()


def validate_url(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError("url must be a non-empty string")
    if len(value) > MAX_URL_LEN:
        raise ValidationError(f"url exceeds {MAX_URL_LEN} chars")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ValidationError(f"url must be http/https, got scheme {parsed.scheme!r}")
    if not parsed.netloc:
        raise ValidationError("url must have a host")
    return value


def validate_title(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("title_or_snippet must be non-empty")
    if len(value) > MAX_TITLE_LEN:
        raise ValidationError(f"title_or_snippet exceeds {MAX_TITLE_LEN} chars")
    return value


def validate_symbols(symbols: list[str] | None) -> list[str]:
    """Every tagged symbol must be ticker-like — no free-form strings reach a later query."""
    if symbols is None:
        return []
    if len(symbols) > MAX_SYMBOLS:
        raise ValidationError(f"too many tagged_symbols (max {MAX_SYMBOLS})")
    out: list[str] = []
    for sym in symbols:
        if not isinstance(sym, str) or not _SYMBOL_RE.match(sym):
            raise ValidationError(f"invalid ticker symbol: {sym!r}")
        out.append(sym.upper())
    return out
