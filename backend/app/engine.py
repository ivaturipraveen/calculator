"""Validation and evaluation, one entry point per renderer.

Every calculator in the corpus is one of eight shapes, and the difference is
real: a score sums the points on the options a clinician ticks, a titration
table evaluates one formula down a ladder of doses, a decision tree asks a
question at a time. Flattening them into a single "put numbers in, get a number
out" API would mean the client reimplementing each shape.

So the request is uniform and the response is a union: whichever of `outputs`,
`score`, `table`, `tree` or `conversion` this calculator produces is populated,
and the rest are absent. A client renders the one that is there.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Optional

from .units import to_base

from extractor.evaluator import (  # noqa: E402  (path set in config)
    ExpressionError,
    resolve_variants,
    run_compute,
)


class FieldError(Exception):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def _as_number(field: dict, raw: Any) -> float:
    if isinstance(raw, bool):
        return 1.0 if raw else 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text:
        raise FieldError(field["key"], f"{field.get('label') or field['key']} is required")
    dated = _date_to_epoch_ms(field, text)
    if dated is not None:
        return dated
    try:
        return float(text)
    except ValueError:
        raise FieldError(
            field["key"],
            f"{field.get('label') or field['key']} must be a number",
        ) from None


# What `<input type="date">` submits, and what the corpus's date arithmetic
# works in.
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _date_to_epoch_ms(field: dict, text: str) -> Optional[float]:
    """A date the browser sent, as the epoch milliseconds the formulas expect.

    Gestational Age subtracts two dates, and its script does that in epoch
    milliseconds -- which is what `base_unit: epoch_ms` records. The date picker
    submits "2026-03-15", so every one of its three date fields was rejected as
    "must be a number" and the calculator could not be used from the form at
    all. Midnight UTC, so the same date is the same number wherever it is
    entered: only the difference between two of them is ever used.
    """
    is_date = field.get("widget") == "date" or field.get("base_unit") == "epoch_ms"
    if not is_date:
        return None
    m = _ISO_DATE.match(text)
    if not m:
        return None
    try:
        d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    except ValueError:
        raise FieldError(
            field["key"],
            f"{field.get('label') or field['key']}: {text} is not a real date",
        ) from None
    return d.timestamp() * 1000.0


def _is_shown(field: dict, values: dict[str, Any]) -> bool:
    """Would the form be showing this field, given the answers so far?

    Valganciclovir asks for a Schwartz coefficient that applies to one sex and
    hides the other. Demanding both made the calculator impossible to finish.
    """
    rule = field.get("visible_when")
    if not rule:
        return True
    chosen = values.get(rule.get("field"))
    if chosen is None or (isinstance(chosen, str) and not chosen.strip()):
        return True                     # the deciding control is unanswered
    return any(str(v) == str(chosen) for v in (rule.get("equals") or []))


def _needed_now(field: dict, values: dict[str, Any]) -> bool:
    """Is a conditionally-required field required on the path being taken?

    Fentanyl reads its bolus dose only when a bolus is being given, and Calvert
    reads a measured GFR only when the clinician says one is known. Demanding
    them regardless made the form refuse to calculate over boxes the answer
    never touches -- and `required_when` records which choices actually consume
    the field, worked out by running the calculator without it.
    """
    rule = field.get("required_when")
    if not rule:
        return False
    chosen = values.get(rule.get("field"))
    if chosen is None or (isinstance(chosen, str) and not chosen.strip()):
        return False                    # the control itself is unanswered
    wanted = {str(v) for v in (rule.get("equals") or [])}
    return str(chosen) in wanted


def validate_inputs(
    spec: dict, values: dict[str, Any], units: Optional[dict[str, str]] = None
) -> tuple[dict[str, Any], list[dict]]:
    """Check each field on its own terms and convert it to its base unit.

    Returns the evaluation vector and a list of per-field errors. Errors are
    collected rather than raised on the first one: a form should light up every
    bad field at once, not lead the user through them one at a time.
    """
    units = units or {}
    vector: dict[str, Any] = {}
    errors: list[dict] = []

    for field in spec.get("inputs") or []:
        key = field["key"]
        label = field.get("label") or key
        raw = values.get(key)
        options = field.get("options") or []
        constraints = field.get("constraints") or {}

        missing = raw is None or (isinstance(raw, str) and not raw.strip())
        if missing:
            # A field the form is not showing cannot have been filled in, so it
            # is not an outstanding requirement. `visible_when` has to mean the
            # same thing here as it does in the client, or the two disagree
            # about whether the form is finished.
            shown = _is_shown(field, values)
            required = field.get("required") or _needed_now(field, values)
            if shown and required:
                errors.append({"field": key, "message": f"{label} is required"})
            # The default stands in only where it MEANS something: "not on this
            # opioid", "use Cockcroft-Gault". On a required field it is just the
            # number that happened to be in the box when the source page was
            # printed, and substituting it would answer with a value the
            # clinician never entered and cannot see.
            default = field.get("default")
            if default is not None and not required:
                vector[key] = default
            continue

        # A select carries its coefficient as the option's value, so a value not
        # on the list is not a choice this calculator can make.
        if options:
            match = _match_option(options, raw)
            if match is None:
                errors.append(
                    {"field": key, "message": f"{label}: {raw!r} is not one of its options"}
                )
                continue
            vector[key] = match.get("value")
            continue

        try:
            number = _as_number(field, raw)
        except FieldError as exc:
            errors.append({"field": exc.field, "message": exc.message})
            continue

        code = units.get(key)
        base = to_base(number, field, code)
        lo, hi = constraints.get("min"), constraints.get("max")
        # Bounds are stated in the base unit, so they are checked after
        # conversion -- a weight entered in pounds must not be compared with a
        # limit expressed in kilograms.
        # `if (weight <= 0) alert(...)` rejects the limit itself, so the limit is
        # a value the field must stay strictly clear of.
        lo_open = bool(constraints.get("exclusive_min"))
        hi_open = bool(constraints.get("exclusive_max"))
        if isinstance(lo, (int, float)) and (
                base <= float(lo) + 1e-9 if lo_open else base < float(lo) - 1e-9):
            word = "greater than" if lo_open else "at least"
            errors.append(
                {"field": key, "message": f"{label} must be {word} {_fmt(lo)}"
                                          f"{_unit_suffix(field)}"}
            )
        if isinstance(hi, (int, float)) and (
                base >= float(hi) - 1e-9 if hi_open else base > float(hi) + 1e-9):
            word = "less than" if hi_open else "at most"
            errors.append(
                {"field": key, "message": f"{label} must be {word} {_fmt(hi)}"
                                          f"{_unit_suffix(field)}"}
            )
        vector[key] = base

    return vector, errors


def _match_option(options: list[dict], raw: Any) -> Optional[dict]:
    for o in options:
        val = o.get("value")
        if val == raw:
            return o
        if isinstance(val, (int, float)) and isinstance(raw, (int, float, str)):
            try:
                if abs(float(val) - float(raw)) < 1e-9:
                    return o
            except (TypeError, ValueError):
                pass
        if str(val) == str(raw) or str(o.get("label")) == str(raw):
            return o
        if o.get("key") is not None and str(o["key"]) == str(raw):
            return o
    return None


def _fmt(v: Any) -> str:
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)


def _unit_suffix(field: dict) -> str:
    code = field.get("display_unit") or field.get("base_unit")
    return f" {code}" if code else ""


# --------------------------------------------------------------------------
# evaluation
# --------------------------------------------------------------------------

def _tables(spec: dict) -> tuple[list, dict]:
    tables = spec.get("tables") or {}
    return tables.get("lookups") or [], tables.get("variants") or {}


def _round(value: float, decimals: Optional[int]) -> float:
    if decimals is None or not isinstance(value, (int, float)):
        return value
    if not math.isfinite(value):
        return value
    return round(value, int(decimals))


def _output_rows(spec: dict, results: dict[str, float]) -> list[dict]:
    display = spec.get("display") or {}
    fallback = display.get("decimal_precision")
    rows = []
    for out in (spec.get("compute") or {}).get("outputs") or []:
        raw = results.get(out["key"])
        if raw is None:
            continue
        decimals = out.get("decimals")
        if decimals is None:
            decimals = fallback
        value = _round(raw, decimals) if isinstance(raw, (int, float)) else raw
        rows.append(
            {
                "key": out["key"],
                "label": out.get("label") or out["key"],
                "value": value,
                "unit": out.get("base_unit"),
                "decimals": decimals,
                "kind": out.get("kind") or "number",
                "finite": isinstance(value, (int, float)) and math.isfinite(value),
            }
        )
    return rows


def evaluate_expressions(
    spec: dict, vector: dict[str, Any], env_out: Optional[dict] = None
) -> dict[str, float]:
    lookups, variants = _tables(spec)
    env = dict(vector)
    env.update(resolve_variants(variants, vector))
    return run_compute(spec["compute"], env, lookups, variants, env_out=env_out)


def lookups_used(spec: dict, env: dict) -> list[dict]:
    """Which row of each reference table this calculation actually read.

    The client should not re-derive this. A growth chart's ladder is keyed on a
    COMPUTED value -- age in months, not the age in years that was typed -- so a
    client matching on the form's own fields would mark the wrong row, or none.
    The evaluator already found the row; this reports the one it found.
    """
    out: list[dict] = []
    for table in (spec.get("tables") or {}).get("lookups") or []:
        rows = table.get("rows") or []
        key = table.get("key") or ""
        value = None
        for cand in (key, key.lower(), key.replace("_", "").lower()):
            if cand in env:
                value = env[cand]
                break
        if value is None:
            continue
        # A ladder bound to a selector only applies for the chosen option.
        skey = table.get("select_key")
        if skey is not None and skey in env:
            want, got = table.get("select_value"), env[skey]
            same = want == got or (
                isinstance(want, (int, float))
                and isinstance(got, (int, float))
                and abs(float(want) - float(got)) < 1e-9
            )
            if not same:
                continue
        lt = str(table.get("operator") or "<").startswith("<")
        index = next(
            (
                i
                for i, r in enumerate(rows)
                if r.get("threshold") is not None
                and (value < r["threshold"] if lt else value <= r["threshold"])
            ),
            len(rows) - 1 if rows else -1,
        )
        if index >= 0:
            out.append(
                {
                    "key": key,
                    "row_index": index,
                    "lookup_value": value,
                    "row": rows[index],
                    "select_value": table.get("select_value"),
                }
            )
    return out


# -- score -----------------------------------------------------------------

def score_result(spec: dict, selections: dict[str, Any]) -> tuple[dict, list[dict]]:
    scoring = spec.get("scoring") or {}
    groups = scoring.get("groups") or []
    errors: list[dict] = []
    total = 0.0
    chosen: list[dict] = []

    for group in groups:
        gkey = group["key"]
        picked = selections.get(gkey)
        multiple = group.get("selection") == "multiple"
        keys = (
            [k for k in picked if k]
            if isinstance(picked, list)
            else ([picked] if picked not in (None, "") else [])
        )
        if not keys:
            if group.get("required") and not multiple:
                errors.append(
                    {"field": gkey, "message": f"{group.get('label') or gkey}: choose one"}
                )
            continue
        if not multiple and len(keys) > 1:
            errors.append(
                {"field": gkey, "message": f"{group.get('label') or gkey}: choose only one"}
            )
            keys = keys[:1]
        for k in keys:
            opt = next((o for o in group.get("options") or [] if o.get("key") == k), None)
            if opt is None:
                errors.append(
                    {"field": gkey, "message": f"{group.get('label') or gkey}: unknown option {k!r}"}
                )
                continue
            total += float(opt.get("points") or 0)
            chosen.append(
                {
                    "group": gkey,
                    "group_label": group.get("label"),
                    "option": k,
                    "label": opt.get("label"),
                    "points": opt.get("points"),
                }
            )

    band = band_for(scoring.get("bands") or [], total)
    totals = scoring.get("total") or {}
    return (
        {
            "total": total,
            "min": totals.get("min"),
            "max": totals.get("max"),
            "unit": totals.get("unit") or "points",
            "label": totals.get("label") or "Total score",
            "band": band,
            "interpretation_label": (scoring.get("interpretation") or {}).get("label"),
            "selected": chosen,
        },
        errors,
    )


def band_for(bands: list[dict], total: float) -> Optional[dict]:
    for b in bands:
        lo, hi = b.get("min"), b.get("max")
        if lo is not None and total < float(lo) - 1e-9:
            continue
        if hi is not None and total > float(hi) + 1e-9:
            continue
        return b
    return None


# -- titration / dose ladder -----------------------------------------------

def titration_table(spec: dict, vector: dict[str, Any]) -> Optional[dict]:
    tables = spec.get("tables") or {}
    ladder = tables.get("dose_ladder")
    compute = spec.get("compute") or {}
    row_var = compute.get("row_variable")
    if not ladder or not row_var:
        return None
    per_row = [s for s in (compute.get("steps") or []) if s.get("note") == "per-row formula"]
    if not per_row:
        return None

    lookups, variants = _tables(spec)
    base_env = dict(vector)
    base_env.update(resolve_variants(variants, vector))

    dose_field = next(
        (i for i in (spec.get("inputs") or []) if i["key"] == row_var), None
    )
    rows: list[dict] = []
    for value in ladder.get("values") or []:
        env = dict(base_env)
        env[row_var] = float(value)
        cells: dict[str, Any] = {}
        try:
            got = run_compute(
                {
                    "engine": "expr",
                    "steps": compute.get("steps") or [],
                    "outputs": [{"key": s["key"], "expr": s["key"]} for s in per_row],
                },
                env,
                lookups,
                variants,
            )
        except ExpressionError:
            got = {}
        for s in per_row:
            cells[s["key"]] = _round(got.get(s["key"]), 2)
        rows.append({"dose": value, **cells})

    return {
        "row_variable": row_var,
        "row_label": (dose_field or {}).get("label") or row_var.replace("_", " ").title(),
        "row_unit": (dose_field or {}).get("base_unit"),
        "columns": [
            {"key": s["key"], "label": s["key"].replace("_", " ").title()}
            for s in per_row
        ],
        "rows": rows,
        "truncated_in_pdf": bool(ladder.get("truncated_in_pdf")),
    }


# -- weight-based drug tables (ALS) ----------------------------------------

def drug_table(spec: dict, vector: dict[str, Any]) -> Optional[dict]:
    drugs = (spec.get("tables") or {}).get("drugs")
    if not isinstance(drugs, list) or not drugs or "routes" not in (drugs[0] or {}):
        return None
    weight = vector.get("weight")
    out = []
    for drug in drugs:
        routes = []
        for route in drug.get("routes") or []:
            doses = []
            for dose in route.get("doses") or []:
                amount = dose.get("amt")
                per_kg = bool(dose.get("perKg"))
                cap = dose.get("max")
                decimals = dose.get("decimals", 2)
                conc = route.get("conc")

                def scale(a: Any) -> Optional[float]:
                    if not isinstance(a, (int, float)):
                        return None
                    v = float(a) * float(weight or 0) if per_kg else float(a)
                    if isinstance(cap, (int, float)) and v > float(cap):
                        v = float(cap)
                    return _round(v, decimals)

                # An infusion is ordered as a range -- "0.1 to 0.5 mcg/kg/min" --
                # and the table states it as one. Handling only a single number
                # left every ranged dose showing a dash, which is the ones a
                # clinician most needs to see.
                if isinstance(amount, list):
                    values = [v for v in (scale(a) for a in amount) if v is not None]
                    value = values or None
                else:
                    value = scale(amount)

                volume: Any = None
                if isinstance(conc, (int, float)) and conc:
                    if isinstance(value, list):
                        volume = [_round(v / float(conc), 2) for v in value]
                    elif value is not None:
                        volume = _round(value / float(conc), 2)

                doses.append(
                    {
                        "phase": dose.get("phase"),
                        "per_kg": per_kg,
                        "amount": amount,
                        "unit": dose.get("unit"),
                        "value": value,
                        "volume_ml": volume,
                        "max": cap,
                        "note": dose.get("note"),
                    }
                )
            routes.append(
                {
                    "label": route.get("label"),
                    "concentration": route.get("conc"),
                    "concentration_unit": route.get("concUnit"),
                    "doses": doses,
                }
            )
        out.append({"name": drug.get("name"), "routes": routes})
    return {"weight": weight, "drugs": out}


# -- unit converter --------------------------------------------------------

def convert(spec: dict, pair_index: int, value: float, reverse: bool) -> dict:
    pairs = (spec.get("tables") or {}).get("pairs") or []
    if not pairs:
        raise FieldError("pair", "this converter has no conversion pairs")
    if pair_index < 0 or pair_index >= len(pairs):
        raise FieldError("pair", "no such conversion")
    pair = pairs[pair_index]
    factor = float(pair.get("factor") or 1.0)
    offset = float(pair.get("offset") or 0.0)
    if reverse:
        result = (value - offset) / factor if factor else value
        src, dst = pair.get("to"), pair.get("from")
    else:
        result = value * factor + offset
        src, dst = pair.get("from"), pair.get("to")
    return {
        "pair_index": pair_index,
        "from": src,
        "to": dst,
        "value": value,
        "result": _round(result, 6),
        "factor": factor,
        "offset": offset,
        "reversed": reverse,
    }


# -- decision tree ---------------------------------------------------------

def tree_step(spec: dict, answers: list[str]) -> dict:
    tables = spec.get("tables") or {}
    nodes = tables.get("nodes") or {}
    outcomes = tables.get("outcomes") or {}
    if not nodes:
        raise FieldError("tree", "this calculator has no decision tree")

    node_id = next(iter(nodes))
    trail: list[dict] = []
    for answer in answers:
        node = nodes.get(node_id)
        if not node or "q" not in node:
            break
        key = "yes" if str(answer).lower() in ("yes", "y", "true", "1") else "no"
        trail.append({"node": node_id, "question": node.get("q"), "answer": key})
        node_id = node.get(key)
        if not node_id:
            break

    outcome = outcomes.get(node_id)
    node = nodes.get(node_id)
    return {
        "node": node_id,
        "question": (node or {}).get("q") if node else None,
        "reconstructed": bool((node or {}).get("reconstructed")),
        "outcome": outcome,
        "trail": trail,
        "done": outcome is not None or node is None,
    }


# -- age/threshold nomograms ------------------------------------------------

def threshold_result(spec: dict, raw: dict[str, Any]) -> Optional[dict]:
    """Compare a measurement against an age-indexed treatment threshold.

    The newborn bilirubin tables are a nomogram printed at nine ages, and the
    published curve is continuous, so a value between two printed ages is
    interpolated rather than snapped to the row below it -- snapping would put
    a 36-hour-old on the 24-hour threshold and read 1.5 mg/dL low.
    """
    rows = (spec.get("tables") or {}).get("thresholds")
    if not isinstance(rows, list) or not rows or "h" not in (rows[0] or {}):
        return None

    fields = {f["key"]: f for f in (spec.get("inputs") or [])}
    age_key = next(
        (k for k in fields if "age" in k and "gestational" not in k), None
    )
    level_key = next((k for k in fields if "bilirubin" in k or "level" in k), None)
    if age_key is None:
        return None

    def _num(key: Optional[str]) -> Optional[float]:
        if key is None:
            return None
        try:
            return float(raw.get(key))
        except (TypeError, ValueError):
            return None

    hours = _num(age_key)
    level = _num(level_key)
    if hours is None:
        return None

    risk_keys = [
        k for k, f in fields.items()
        if (f.get("options") or []) and k not in (age_key, level_key)
    ]
    picked = []
    for k in risk_keys:
        val = raw.get(k)
        opt = _match_option(fields[k].get("options") or [], val) if val is not None else None
        if opt and str(opt.get("label", "")).strip().lower() == "yes":
            picked.append({"key": k, "label": fields[k].get("label")})
    column = "anyRisk" if picked else "noRisk"

    ordered = sorted(rows, key=lambda r: float(r["h"]))
    lo = ordered[0]
    hi = ordered[-1]
    for a, b in zip(ordered, ordered[1:]):
        if float(a["h"]) <= hours <= float(b["h"]):
            lo, hi = a, b
            break
    else:
        lo = hi = ordered[0] if hours < float(ordered[0]["h"]) else ordered[-1]

    span = float(hi["h"]) - float(lo["h"])
    t = 0.0 if span == 0 else (hours - float(lo["h"])) / span
    thresholds = {}
    for name in ("photo", "escalate", "exchange"):
        a = (lo.get(column) or {}).get(name)
        b = (hi.get(column) or {}).get(name)
        if a is None or b is None:
            continue
        thresholds[name] = _round(float(a) + (float(b) - float(a)) * t, 1)

    crossed = [
        name for name, limit in thresholds.items()
        if level is not None and level >= limit
    ]
    return {
        "age_hours": hours,
        "level": level,
        "risk_column": column,
        "risk_factors": picked,
        "thresholds": thresholds,
        "crossed": crossed,
        "interpolated": span > 0 and 0 < t < 1,
        "bracket": [float(lo["h"]), float(hi["h"])],
        "clamped": hours < float(ordered[0]["h"]) or hours > float(ordered[-1]["h"]),
    }
