"""Exact decimal <-> atom conversion helpers.

All arithmetic uses ``decimal.Decimal`` with a high-precision context so
results are exact for the decimal strings this library accepts.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

from .currencies import CURRENCIES

_PRECISION = 80


def parse_amount(text: str, currency: str) -> int:
    """Parse a decimal string into atoms of ``currency``.

    Raises ``ValueError`` if the string is not a non-negative decimal that is
    exactly representable in the currency's atomic unit.
    """
    if currency not in CURRENCIES:
        raise ValueError(f"unknown currency: {currency!r}")
    try:
        value = Decimal(text)
    except Exception:
        raise ValueError(f"invalid decimal amount: {text!r}") from None
    if not value.is_finite() or value < 0:
        raise ValueError(f"amount must be a non-negative finite decimal: {text!r}")
    scale = 10 ** CURRENCIES[currency]
    atoms = value * scale
    if atoms != atoms.to_integral_value():
        raise ValueError(
            f"amount {text!r} is not exactly representable in {currency} atoms"
        )
    return int(atoms)


def format_amount(atoms: int, currency: str) -> str:
    """Format atoms as a plain decimal string in whole currency units."""
    scale = 10 ** CURRENCIES[currency]
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        value = Decimal(atoms) / Decimal(scale)
    return format(value, "f")


def parse_price(text: str) -> Decimal:
    """Parse a positive decimal price string."""
    try:
        value = Decimal(text)
    except Exception:
        raise ValueError(f"invalid price: {text!r}") from None
    if not value.is_finite() or value <= 0:
        raise ValueError(f"price must be a positive finite decimal: {text!r}")
    return value
