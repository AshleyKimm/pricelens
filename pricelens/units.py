"""Pack sizes and unit prices. All conversions happen here, in code, never in the model.

Base units: count, g, ml. A unit price is stored as minor units per base unit (a float) and
only converted for display: imperial (default) per lb / per fl oz, or metric per 100 g / 100 ml.
"""
from __future__ import annotations

import re

# printed unit -> (base unit, base units per printed unit)
PACK_UNITS = {
    "count": ("count", 1.0), "dozen": ("count", 12.0),
    "g": ("g", 1.0), "kg": ("g", 1000.0), "oz": ("g", 28.349523125), "lb": ("g", 453.59237),
    "ml": ("ml", 1.0), "l": ("ml", 1000.0), "fl oz": ("ml", 29.5735295625), "pt": ("ml", 473.176473),
    "qt": ("ml", 946.352946), "gal": ("ml", 3785.411784),
}
SYSTEMS = ("imperial", "metric")
DEFAULT_SYSTEM = "imperial"

# Display: base unit -> (multiplier from "per base unit", label) for each system
_DISPLAY = {
    "imperial": {"count": (1.0, "each"), "g": (453.59237, "lb"), "ml": (29.5735295625, "fl oz")},
    "metric": {"count": (1.0, "each"), "g": (100.0, "100 g"), "ml": (100.0, "100 ml")},
}


def to_base(quantity: float | None, unit: str | None) -> tuple[float, str] | None:
    if quantity is None or unit is None or unit not in PACK_UNITS or quantity <= 0:
        return None
    base, factor = PACK_UNITS[unit]
    return quantity * factor, base


def _number_variants(q: float) -> list[str]:
    out = {f"{q:g}"}
    if q == 0.5:
        out |= {"1/2", "HALF", ".5"}
    if q == int(q):
        out.add(str(int(q)))
    return list(out)


def pack_problem(printed: str, quantity: float | None, unit: str | None) -> str | None:
    """P1: the model's pack size must be backed by the printed text. Returns a reason, or None if OK.

    The number must appear as printed ("24" in "LG EGGS 24CT", "16.9" in "EVOO 16.9OZ"), except for
    dozens, which may be just the word ("EGGS DOZEN" = 1 dozen; "1/2 DOZEN" = 0.5 dozen).
    """
    if quantity is None and unit is None:
        return None
    if quantity is None or unit is None:
        return "P1: pack size has a quantity without a unit (or the reverse)"
    if unit not in PACK_UNITS:
        return f"P1: unknown pack unit {unit!r}"
    text = printed.upper()
    if unit == "dozen":
        if not re.search(r"DOZEN|\bDZ\b", text):
            return "P1: 'dozen' not in the printed name"
        if quantity == 1 and not re.search(r"(?<![\d./])\d", text):
            return None  # "EGGS DOZEN"
        for v in _number_variants(quantity):
            if re.search(rf"(?<![\d./]){re.escape(v)}(?![\d./])", text):
                return None
        return f"P1: {quantity:g} dozen not supported by {printed!r}"
    for v in _number_variants(quantity):
        # Not part of a larger number or a fraction: '2' must not match inside '12' or '1/2'.
        if re.search(rf"(?<![\d./]){re.escape(v)}(?![\d./])", text):
            return None
    return f"P1: pack quantity {quantity:g} does not appear in {printed!r}"


def unit_price(package_price_minor: int, base_qty: float) -> float:
    return package_price_minor / base_qty


def display(per_base: float, base_unit: str, system: str = DEFAULT_SYSTEM) -> tuple[float, str]:
    """(minor units per display unit, label) e.g. (31.0, 'each') or (149.6, 'lb')."""
    mult, label = _DISPLAY.get(system, _DISPLAY[DEFAULT_SYSTEM])[base_unit]
    return per_base * mult, label


def weighed_to_base(unit: str) -> tuple[float, str] | None:
    """Weighed lines are priced per lb/kg/oz/g already: how many base units one of those is."""
    return to_base(1.0, unit) if unit in ("lb", "kg", "oz", "g") else None


def format_unit_price(base_price: float | None, base_unit: str | None, system: str = DEFAULT_SYSTEM,
                      currency: str = "USD") -> str:
    """'$0.30 / each', '$1.49 / lb', '$0.33 / 100 g'; '' if unknown."""
    from .money import format_amount
    if base_price is None or base_unit is None:
        return ""
    value, label = display(base_price, base_unit, system)
    return f"{format_amount(round(value), currency)} / {label}"
