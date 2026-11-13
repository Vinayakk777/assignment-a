"""Value and header normalisation for lead ingestion.

All parsing follows the assignment rules and never includes a raw PII value in
an exception message.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Iterable

from .crypto import canonical_phone

_HEADER_SEPARATOR_RE = re.compile(r"[_\s]+")
_CURRENCY_PREFIX_RE = re.compile(r"^(?:₹|rs\.?|inr)\s*", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"^\d+(?:\.\d+)?$")
_DATE_YMD_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DATE_DMY_SLASH_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_DATE_DMY_DASH_RE = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$")
_DATE_DMY_MONTH_RE = re.compile(r"^(\d{1,2})-([A-Za-z]{3,9})-(\d{4})$")

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def normalize_header(value: str) -> str:
    """Lower-case, trim and collapse ``_``/whitespace runs to one space."""
    if not isinstance(value, str):
        return ""
    return _HEADER_SEPARATOR_RE.sub(" ", value.strip().lower())


def parse_phone(raw: str) -> str:
    """Return the canonical ``+91XXXXXXXXXX`` phone or raise ``ValueError``."""
    return canonical_phone(raw)


def parse_date(raw: str) -> date:
    """Parse the four accepted day-first date formats.

    Accepted: ``YYYY-MM-DD``, ``DD/MM/YYYY``, ``DD-MM-YYYY`` and
    ``DD-Mon-YYYY``. Impossible dates are rejected by :class:`datetime.date`.
    """
    text = raw.strip()

    match = _DATE_YMD_RE.fullmatch(text)
    if match:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))

    match = _DATE_DMY_SLASH_RE.fullmatch(text)
    if match:
        return date(int(match.group(3)), int(match.group(2)), int(match.group(1)))

    match = _DATE_DMY_DASH_RE.fullmatch(text)
    if match:
        return date(int(match.group(3)), int(match.group(2)), int(match.group(1)))

    match = _DATE_DMY_MONTH_RE.fullmatch(text)
    if match:
        month = _MONTHS.get(match.group(2).lower())
        if month is None:
            raise ValueError("invalid date")
        return date(int(match.group(3)), month, int(match.group(1)))

    raise ValueError("invalid date")


def parse_decimal(raw: str) -> Decimal:
    """Parse a non-negative currency amount with Decimal, rounded to 2 places."""
    text = raw.strip()
    text = _CURRENCY_PREFIX_RE.sub("", text, count=1).strip()
    if not text or any(character.isspace() for character in text):
        raise ValueError("invalid decimal")
    text = text.replace(",", "")
    if not _AMOUNT_RE.fullmatch(text):
        raise ValueError("invalid decimal")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - defensive
        raise ValueError("invalid decimal") from exc
    if not value.is_finite() or value < 0:
        raise ValueError("invalid decimal")
    with localcontext() as context:
        context.prec = max(50, len(value.as_tuple().digits) + 5)
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def format_decimal(value: Decimal) -> str:
    """Serialise a Decimal with exactly two decimal places."""
    return format(value, ".2f")


def parse_enum(raw: str, values: Iterable[str]) -> str:
    """Validate an enum case-insensitively and return its lower-case form."""
    key = raw.strip().lower()
    allowed = {str(value).strip().lower() for value in values}
    if key not in allowed:
        raise ValueError("invalid enum")
    return key


def normalize_default_value(
    field_type: str, value: object, enum_values: Iterable[str] = ()
) -> object:
    """Validate and normalise a schema default using the field's type rules."""
    raw = str(value).strip()
    if field_type == "string":
        if not raw:
            raise ValueError("invalid default")
        return raw
    if field_type == "phone":
        return parse_phone(raw)
    if field_type == "date":
        return parse_date(raw).isoformat()
    if field_type == "decimal":
        return format_decimal(parse_decimal(raw))
    if field_type == "enum":
        return parse_enum(raw, enum_values)
    raise ValueError("unsupported field type")


def normalize_field_value(
    field_type: str, raw: str, enum_values: Iterable[str] = ()
) -> object:
    """Normalise a non-empty cell value to its stored representation."""
    if field_type == "string":
        value = raw.strip()
        if not value:
            raise ValueError("invalid string")
        return value
    if field_type == "phone":
        return parse_phone(raw)
    if field_type == "date":
        return parse_date(raw).isoformat()
    if field_type == "decimal":
        return format_decimal(parse_decimal(raw))
    if field_type == "enum":
        return parse_enum(raw, enum_values)
    raise ValueError("unsupported field type")
