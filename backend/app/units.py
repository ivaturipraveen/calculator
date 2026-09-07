"""Unit handling: every field is stored and computed in its base unit.

A unit is affine, not just a multiplier -- `base = raw * factor + offset` --
because temperature is in the corpus and °F -> °C needs both halves. Treating
every unit as a plain factor silently drops the offset and reports a fever as
a hypothermia.
"""

from __future__ import annotations

from typing import Any, Optional


def _same(a: Optional[str], b: Optional[str]) -> bool:
    """Unit codes differ in case between the script and the registry."""
    return bool(a) and bool(b) and str(a).strip().lower() == str(b).strip().lower()


def unit_for(field: dict, code: Optional[str]) -> Optional[dict]:
    """The unit a value is in: the one asked for, else the field's own base.

    Matching is case-insensitive because the two sources disagree: a field
    declares `hours` and the shared registry lists `Hours`. An exact match
    silently fell through to the first unit in the list -- Days -- and read
    every reservoir-change interval as 24 times its value.
    """
    units = field.get("units") or []
    if not units:
        return None
    if code:
        for u in units:
            if _same(u.get("code"), code) or _same(u.get("label"), code):
                return u
    want = field.get("display_unit") or field.get("base_unit")
    for u in units:
        if _same(u.get("code"), want):
            return u
    # No base unit could be identified, so no conversion can be justified.
    # Guessing at the first entry is how a wrong factor gets applied silently.
    return next((u for u in units if u.get("factor") == 1), None)


def to_base(value: float, field: dict, code: Optional[str]) -> float:
    u = unit_for(field, code)
    if not u:
        return value
    factor = u.get("factor")
    offset = u.get("offset") or 0.0
    if factor in (None, 0):
        return value
    return value * float(factor) + float(offset)


def from_base(value: float, field: dict, code: Optional[str]) -> float:
    u = unit_for(field, code)
    if not u:
        return value
    factor = u.get("factor")
    offset = u.get("offset") or 0.0
    if factor in (None, 0):
        return value
    return (value - float(offset)) / float(factor)


def base_code(field: dict) -> Optional[str]:
    return field.get("base_unit") or (
        (field.get("units") or [{}])[0].get("code") if field.get("units") else None
    )


def field_units(field: dict) -> list[dict[str, Any]]:
    """The unit choices a form should offer, base unit first."""
    units = field.get("units") or []
    if not units:
        code = field.get("base_unit") or field.get("unit")
        return [{"code": code, "label": code}] if code else []
    base = base_code(field)
    ordered = sorted(units, key=lambda u: (u.get("code") != base, str(u.get("code"))))
    return [
        {
            "code": u.get("code"),
            "label": u.get("label") or u.get("code"),
            "factor": u.get("factor"),
            "offset": u.get("offset") or 0.0,
            "is_base": u.get("code") == base,
        }
        for u in ordered
    ]
