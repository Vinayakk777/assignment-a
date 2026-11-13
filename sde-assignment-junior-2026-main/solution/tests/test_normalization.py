from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from au_ledger.normalization import (
    format_decimal,
    normalize_field_value,
    normalize_header,
    parse_date,
    parse_decimal,
    parse_enum,
    parse_phone,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Customer__  Name  ", "customer name"),
        ("MOBILE___NO", "mobile no"),
        ("policy_no", "policy no"),
        ("  Expiry   Date ", "expiry date"),
        ("agent_notes", "agent notes"),
        ("\tInsured\t Name\n", "insured name"),
    ],
)
def test_header_normalisation(raw, expected):
    assert normalize_header(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "9876543210",
        "+91 98765 43210",
        "09876543210",
        "91-98765-43210",
        "(+91) 9876543210",
        "+919876543210",
        "  +91  98765 43210  ",
    ],
)
def test_phone_normalisation(raw):
    assert parse_phone(raw) == "+919876543210"


@pytest.mark.parametrize(
    "raw",
    ["", "987654321", "5876543210", "022-1234-5678", "N/A", "+1 415 555 0134"],
)
def test_phone_rejects_invalid_values_without_echoing_them(raw):
    with pytest.raises(ValueError) as excinfo:
        parse_phone(raw)
    if raw.strip():
        assert raw.strip() not in str(excinfo.value)


@pytest.mark.parametrize(
    "raw",
    [
        "2026-12-01",
        "01/12/2026",
        "01-12-2026",
        "01-Dec-2026",
        "01-dec-2026",
        "1/12/2026",
        "1-Dec-2026",
    ],
)
def test_date_formats(raw):
    assert parse_date(raw) == date(2026, 12, 1)


@pytest.mark.parametrize(
    "raw",
    [
        "31/02/2026",
        "2026-13-01",
        "2026-02-30",
        "soon",
        "TBD",
        "2026/12/01",
        "01.12.2026",
        "01-Xyz-2026",
        "01-Dec-26",
        "01-Dec-2026 10:00",
    ],
)
def test_invalid_dates(raw):
    with pytest.raises(ValueError):
        parse_date(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("4000", "4000.00"),
        ("4000.5", "4000.50"),
        ("₹12,345", "12345.00"),
        ("Rs 1234", "1234.00"),
        ("Rs. 1,234.56", "1234.56"),
        ("INR 4000.1", "4000.10"),
        ("rs1234", "1234.00"),
        ("inr1,234.5", "1234.50"),
        ("1,23,456.78", "123456.78"),
        ("1.005", "1.01"),
        ("1.004", "1.00"),
        ("0", "0.00"),
    ],
)
def test_decimal_and_currency_normalisation(raw, expected):
    value = parse_decimal(raw)
    assert isinstance(value, Decimal)
    assert format_decimal(value) == expected


@pytest.mark.parametrize(
    "raw",
    ["N/A", "-", "twelve thousand", "-1", "1 234", "1.2.3", "₹", "INR", "1e3", "1,234."],
)
def test_invalid_decimals(raw):
    with pytest.raises(ValueError):
        parse_decimal(raw)


def test_decimal_arithmetic_does_not_use_float():
    result = parse_decimal("0.1") + parse_decimal("0.2")
    assert result == Decimal("0.30")


def test_decimal_rounds_half_up():
    assert format_decimal(parse_decimal("10.005")) == "10.01"
    assert format_decimal(parse_decimal("10.004")) == "10.00"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("HI", "hi"), (" hi ", "hi"), ("En", "en"), ("TA", "ta")],
)
def test_enum_is_case_insensitive_and_stored_lower(raw, expected):
    assert parse_enum(raw, ["en", "hi", "ta"]) == expected


def test_enum_accepts_mixed_case_schema_values():
    assert parse_enum("ACTIVE", ["Active", "Pending"]) == "active"
    with pytest.raises(ValueError):
        parse_enum("unknown", ["Active", "Pending"])


def test_normalize_field_value_returns_stored_representations():
    assert normalize_field_value("string", "  Asha  ") == "Asha"
    assert normalize_field_value("phone", "09876543210") == "+919876543210"
    assert normalize_field_value("date", "01-Dec-2026") == "2026-12-01"
    assert normalize_field_value("decimal", "₹1,234.5") == "1234.50"
    assert normalize_field_value("enum", "HI", ["en", "hi"]) == "hi"
