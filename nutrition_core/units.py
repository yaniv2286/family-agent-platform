"""Decimal-safe arithmetic helpers.

Transport uses decimal strings; persistence uses NUMERIC-compatible values.
Rounding is for display only - domain math keeps full precision.
"""
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Optional, Union

Number = Union[str, int, float, Decimal]

_QUANTUM = Decimal("0.000001")


def dec(value: Number) -> Decimal:
    """Parse a value into Decimal. Rejects NaN/inf and malformed input."""
    if isinstance(value, Decimal):
        d = value
    else:
        try:
            d = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"not a decimal value: {value!r}") from exc
    if d.is_nan() or d.is_infinite():
        raise ValueError(f"non-finite decimal: {value!r}")
    return d


def dstr(value: Optional[Decimal]) -> Optional[str]:
    """Serialize a Decimal (or None for unknown) to a transport string."""
    return None if value is None else format(value.normalize(), "f")


def scale_per_basis(nutrient_per_basis: Number, basis_g: Number, quantity_g: Number) -> Decimal:
    """nutrient amount for quantity_g of a food whose nutrient is per basis_g.

    meal[n] += (edibleGrams / basisGrams) * nutrientPerBasis  (DESIGN 7.1)
    """
    basis = dec(basis_g)
    if basis <= 0:
        raise ValueError("basis_g must be positive")
    qty = dec(quantity_g)
    if qty < 0:
        raise ValueError("quantity_g must be non-negative")
    return (qty / basis) * dec(nutrient_per_basis)


def round_display(value: Optional[Decimal], places: int = 1) -> Optional[str]:
    """Round strictly for display. Never feed this back into checks."""
    if value is None:
        return None
    q = Decimal(1).scaleb(-places)
    return format(value.quantize(q, rounding=ROUND_HALF_UP), "f")
