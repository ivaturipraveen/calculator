"""Canonical unit conversions, used to fill factors the sources do not state.

Every conversion is expressed in the affine form the vendor's own dropdowns use:

    base_value = raw * factor + offset

That single form covers plain scaling (lb -> kg) and the offset conversions
(degF -> degC) that a scale-only table cannot express -- which is exactly why
the mockup had to special-case temperature in code instead of in its factor
maps, leaving those options with no recoverable factor.

Only physical and standard clinical conversions belong here. They are fixed
constants, not clinical judgement, so supplying them is safe; anything
substance-specific (a drug's mg <-> mmol, say) must come from the source
document and is deliberately absent.
"""

from __future__ import annotations

from typing import Optional

from .identity import squash

# dimension -> base unit -> {alias: (factor, offset)}
CONVERSIONS: dict[str, dict] = {
    "temperature": {
        "base": "degC",
        "units": {
            "degc": (1.0, 0.0),
            "degreesc": (1.0, 0.0),
            "c": (1.0, 0.0),
            "celsius": (1.0, 0.0),
            "degf": (5.0 / 9.0, -160.0 / 9.0),
            "degreesf": (5.0 / 9.0, -160.0 / 9.0),
            "f": (5.0 / 9.0, -160.0 / 9.0),
            "fahrenheit": (5.0 / 9.0, -160.0 / 9.0),
            "k": (1.0, -273.15),
            "kelvin": (1.0, -273.15),
        },
    },
    "mass": {
        "base": "kg",
        "units": {
            "kg": (1.0, 0.0), "kilogram": (1.0, 0.0), "kgs": (1.0, 0.0),
            "g": (0.001, 0.0), "gm": (0.001, 0.0), "gram": (0.001, 0.0),
            "mg": (1e-6, 0.0), "mcg": (1e-9, 0.0),
            "lb": (0.45359237, 0.0), "lbs": (0.45359237, 0.0),
            "pound": (0.45359237, 0.0), "pounds": (0.45359237, 0.0),
            "oz": (0.028349523125, 0.0), "ounce": (0.028349523125, 0.0),
        },
    },
    "length": {
        "base": "cm",
        "units": {
            "cm": (1.0, 0.0), "centimetre": (1.0, 0.0), "centimeter": (1.0, 0.0),
            "m": (100.0, 0.0), "meter": (100.0, 0.0), "meters": (100.0, 0.0),
            "metre": (100.0, 0.0), "kilometre": (100000.0, 0.0),
            "km": (100000.0, 0.0),
            "mm": (0.1, 0.0), "millimetre": (0.1, 0.0),
            "in": (2.54, 0.0), "inch": (2.54, 0.0), "inches": (2.54, 0.0),
            "ft": (30.48, 0.0), "feet": (30.48, 0.0), "foot": (30.48, 0.0),
            "mile": (160934.4, 0.0),
        },
    },
    "pressure": {
        "base": "mmHg",
        "units": {
            "mmhg": (1.0, 0.0), "torr": (0.999999857533699, 0.0),
            "pascal": (0.00750062, 0.0), "pa": (0.00750062, 0.0),
            "kpa": (7.50062, 0.0),
            "cmh2o": (0.735559, 0.0), "atm": (760.0, 0.0),
        },
    },
    "time_age": {
        "base": "yr",
        "units": {
            "yr": (1.0, 0.0), "year": (1.0, 0.0), "years": (1.0, 0.0),
            "mo": (1.0 / 12.0, 0.0), "month": (1.0 / 12.0, 0.0),
            "months": (1.0 / 12.0, 0.0),
            "wk": (1.0 / 52.1775, 0.0), "week": (1.0 / 52.1775, 0.0),
            "day": (1.0 / 365.25, 0.0), "days": (1.0 / 365.25, 0.0),
        },
    },
    "time": {
        "base": "hr",
        "units": {
            "hr": (1.0, 0.0), "hrs": (1.0, 0.0), "hour": (1.0, 0.0),
            "hours": (1.0, 0.0),
            "min": (1.0 / 60.0, 0.0), "minute": (1.0 / 60.0, 0.0),
            "minutes": (1.0 / 60.0, 0.0),
            "sec": (1.0 / 3600.0, 0.0), "seconds": (1.0 / 3600.0, 0.0),
            "day": (24.0, 0.0), "days": (24.0, 0.0),
        },
    },
    "volume": {
        "base": "mL",
        "units": {
            "ml": (1.0, 0.0), "millilitre": (1.0, 0.0), "milliliter": (1.0, 0.0),
            "l": (1000.0, 0.0), "litre": (1000.0, 0.0), "liter": (1000.0, 0.0),
            "dl": (100.0, 0.0), "mcl": (1e-3, 0.0), "microl": (1e-3, 0.0),
        },
    },
    "fraction": {
        "base": "fraction",
        "units": {
            "fraction": (1.0, 0.0), "fractiono2": (1.0, 0.0),
            "ratio": (1.0, 0.0), "rate": (1.0, 0.0),
            "%": (0.01, 0.0), "percent": (0.01, 0.0), "%o2": (0.01, 0.0),
        },
    },
    "ratio": {
        "base": "ratio",
        "units": {
            "ratio": (1.0, 0.0), "fraction": (1.0, 0.0), "rate": (1.0, 0.0),
            "%": (0.01, 0.0), "percent": (0.01, 0.0),
        },
    },
    "creatinine": {
        "base": "mg/dL",
        "units": {
            "mgdl": (1.0, 0.0), "mgpercent": (1.0, 0.0),
            "mcmoll": (1.0 / 88.4, 0.0), "umoll": (1.0 / 88.4, 0.0),
            "mmoll": (1000.0 / 88.4, 0.0),
        },
    },
    "cholesterol": {
        "base": "mg/dL",
        "units": {"mgdl": (1.0, 0.0), "mmoll": (1.0 / 0.02586, 0.0)},
    },
    "bilirubin": {
        "base": "mg/dL",
        "units": {"mgdl": (1.0, 0.0), "mcmoll": (1.0 / 17.1, 0.0),
                  "umoll": (1.0 / 17.1, 0.0)},
    },
}

# Options with no defensible physical conversion. The mockup treats these as
# pass-through; recording why keeps that decision visible rather than implied.
UNCONVERTIBLE = {
    "litreso2": "a volume of O2 is not a fraction; the source treats it as already "
                "being the plain 0-1 fraction",
}


def resolve(code: str, dimension: Optional[str]) -> Optional[tuple[float, float]]:
    """Look up (factor, offset) for a unit code within a dimension."""
    if not code or not dimension:
        return None
    table = CONVERSIONS.get(dimension)
    if not table:
        return None
    return table["units"].get(squash(code))


def fill_unresolved(inputs: list[dict]) -> dict[str, int]:
    """Supply factors the sources left blank, from the canonical table."""
    stats = {"filled": 0, "still_unresolved": 0, "unconvertible": 0}
    for inp in inputs:
        dim = inp.get("dimension")
        for u in inp.get("units") or []:
            if u.get("resolved"):
                continue
            hit = resolve(u.get("code", ""), dim)
            if hit:
                u["factor"], u["offset"] = hit
                u["resolved"] = True
                u["factor_source"] = "canonical"
                stats["filled"] += 1
                continue
            why = UNCONVERTIBLE.get(squash(u.get("code", "")))
            if why:
                u["factor"], u["offset"] = 1.0, 0.0
                u["resolved"] = True
                u["factor_source"] = "pass_through"
                u["note"] = why
                stats["unconvertible"] += 1
            else:
                stats["still_unresolved"] += 1
    return stats


def infer_dimension(base_unit: Optional[str], codes: list[str]) -> Optional[str]:
    """Guess a field's dimension from the unit codes its dropdown offers."""
    cands = [squash(c) for c in codes if c] + ([squash(base_unit)] if base_unit else [])
    best, best_hits = None, 0
    for dim, table in CONVERSIONS.items():
        hits = sum(1 for c in cands if c in table["units"])
        if hits > best_hits:
            best, best_hits = dim, hits
    return best if best_hits >= max(1, len(cands) // 2) else None
