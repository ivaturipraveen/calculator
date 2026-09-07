#!/usr/bin/env python3
"""Reconcile every calculator against its own PDF, field by field.

The other checks report the corpus: 178 compute, coverage is 98%, no invariant
fires. Those numbers can all be green while one calculator quietly shows a
result in the wrong unit or offers a select that changes nothing -- which is
exactly what kept happening, and what kept being found by looking at a page
rather than at a total.

So this asks a different question. Not "how is the corpus doing" but "is THIS
calculator right", one at a time, with a named verdict per check and a per-file
report. A calculator is `verified` only when every check on it passes; anything
else lands in a queue with the reason attached.

The checks are deliberately narrow and each one is falsifiable from the source
document alone -- a bound that the script states, an option the form prints, a
constant the equation names. A check that cannot be decided from the PDF says
so rather than guessing, because a report full of maybes teaches the reader to
skip it.

    python3 reconcile.py                 # every calculator
    python3 reconcile.py --only dobutamine
    python3 reconcile.py --failing       # only what still needs reading
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from extractor.evaluator import ExpressionError, resolve_variants, run_compute
from extractor.identity import squash
from extractor.jsparse import _slice_function, normalize_text
from extractor.pdf_extract import (
    read_pdf_text,
    split_calculator_results,
    split_sections,
    strip_boilerplate,
)

C = {"ok": "\033[92m", "bad": "\033[91m", "warn": "\033[93m",
     "dim": "\033[90m", "b": "\033[1m", "r": "\033[0m"}


# Form furniture. None of it is a field, a result or a note: it is the widgets
# the page draws around the calculator, and every one of them appears verbatim
# on dozens of forms.
_FORM_CHROME = {
    "input", "inputs", "results", "result", "calculate", "reset", "reset form",
    "pull-down", "clear", "print", "close", "back", "next", "restart",
    "create chart", "show chart", "submit", "select drug", "name", "patient id",
    "birthdate", "birthdate ?", "birthdate (optional)", "birthdate (optional) ?",
    "name (optional)", "patient id (optional)", "created by", "date",
    # The date pickers print their three parts as separate lines.
    "mm", "dd", "yyyy", "yy",
}
_CHROME_PREFIX = (
    "total criteria point count",
    "set maximal display precision",
    "decimal precision",
    "important:",
    "resultsimportant",
    "results important",
)


PASS, FAIL, NA = "pass", "fail", "n/a"


class Report:
    """One calculator's verdict, check by check."""

    def __init__(self, slug: str, title: str) -> None:
        self.slug = slug
        self.title = title
        self.checks: list[dict] = []

    def add(self, name: str, status: str, detail: str = "",
            evidence: Any = None) -> None:
        self.checks.append({
            "check": name, "status": status, "detail": detail,
            "evidence": evidence,
        })

    def decide(self, name: str, ok: bool, detail: str = "",
               evidence: Any = None) -> None:
        self.add(name, PASS if ok else FAIL, detail, evidence)

    @property
    def failures(self) -> list[dict]:
        return [c for c in self.checks if c["status"] == FAIL]

    @property
    def verified(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict:
        return {
            "slug": self.slug,
            "title": self.title,
            "verified": self.verified,
            "checked": len(self.checks),
            "passed": sum(1 for c in self.checks if c["status"] == PASS),
            "not_applicable": sum(1 for c in self.checks if c["status"] == NA),
            "failures": self.failures,
            "checks": self.checks,
        }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _labels(spec: dict) -> set[str]:
    """Every name this spec answers to, folded."""
    out: set[str] = set()
    for i in spec.get("inputs") or []:
        out.add(squash(i.get("label") or ""))
        out.add(squash(i.get("key") or ""))
        for o in i.get("options") or []:
            out.add(squash(o.get("label") or ""))
        for u in i.get("units") or []:
            out.add(squash(u.get("code") or ""))
    for o in (spec.get("compute") or {}).get("outputs") or []:
        out.add(squash(o.get("label") or ""))
        out.add(squash(o.get("key") or ""))
        out.add(squash(o.get("base_unit") or ""))
    scoring = spec.get("scoring") or {}
    for g in scoring.get("groups") or []:
        out.add(squash(g.get("label") or ""))
        for o in g.get("options") or []:
            out.add(squash(o.get("label") or ""))
    for b in scoring.get("bands") or []:
        out.add(squash(b.get("label") or ""))
        out.add(squash(b.get("raw") or ""))
        # A band prints as "0 to 3 points:" over "Low probability", so each
        # half has to be recognised on its own.
        lo, hi = b.get("min"), b.get("max")
        for form in (f"{lo} to {hi} points:", f"score > {lo}:", f"score < {hi}:",
                     f"score >= {lo} and <= {hi}:", f"{lo} points:"):
            out.add(squash(form))
    out.add(squash((scoring.get("interpretation") or {}).get("label") or ""))
    content = spec.get("content") or {}
    for h in content.get("form_sections") or []:
        out.add(squash(h))
    for f in content.get("fixed_values") or []:
        out.add(squash(f.get("label")))
        out.add(squash(f"{f.get('label')} {f.get('value'):g} {f.get('unit')}"))
    for t in content.get("reference_tables") or []:
        out.add(squash(t.get("title")))
        for r in t.get("rows") or []:
            out.add(squash(r.get("label")))
            for v in r.get("values") or []:
                out.add(squash(f"{r.get('label')} {v:g}"))
    out.discard("")
    return out


def _vector(spec: dict, frac: float = 0.5) -> dict[str, Any]:
    """A plausible set of inputs, for probing that the thing actually runs."""
    vec: dict[str, Any] = {}
    fields = spec.get("inputs") or []
    for i, f in enumerate(fields):
        opts = f.get("options") or []
        if opts:
            vec[f["key"]] = opts[0].get("value")
            continue
        c = f.get("constraints") or {}
        lo, hi = c.get("min"), c.get("max")
        lo = 1.0 if lo is None else float(lo)
        hi = float(hi) if hi is not None else max(lo * 2, lo + 10, 10.0)
        spread = frac + 0.25 * (i / max(len(fields) - 1, 1))
        vec[f["key"]] = lo + (hi - lo) * min(spread, 0.95) or 1.0
    return vec


def _evaluate(spec: dict, vec: dict) -> dict[str, float]:
    tables = spec.get("tables") or {}
    env = dict(vec)
    env.update(resolve_variants(tables.get("variants") or {}, vec))
    return run_compute(spec["compute"], env,
                       tables.get("lookups") or [], tables.get("variants") or {})


# --------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------

# Words that head a column rather than name a field.
_COLUMN_WORDS = {"opioid", "drug", "dose", "doses", "units", "unit", "input",
                 "inputs", "result", "results", "value", "values", "amount",
                 "route", "frequency", "total"}


def _is_column_header(raw: str, known: set[str], blob: str) -> bool:
    """A table's header row, printed with its columns run together.

    MMED prints "OpioidDoseUnitsMorphine milligram equivalents per day (MMED)"
    -- four headings with the spaces between them lost. Split back at the case
    boundaries, every part is either a column word or something the spec holds,
    so the line is the header of a table the spec already carries.
    """
    parts = [p for p in re.split(r"(?<=[a-z\)])(?=[A-Z])", raw) if p.strip()]
    if len(parts) < 3:
        return False
    for part in parts:
        s = squash(part)
        if not s:
            continue
        if s in _COLUMN_WORDS or s in known or s in blob:
            continue
        return False
    return True


def check_printed_form(rep: Report, spec: dict, sec: dict) -> None:
    """Every line the form prints is accounted for somewhere in the spec."""
    calc_in, _ = split_calculator_results(sec.get("Calculator", ""))
    lines = [l.strip() for l in (calc_in or sec.get("Calculator", "")).split("\n")]
    known = _labels(spec)
    blob = re.sub(r"[^a-z0-9]+", "", json.dumps(spec, ensure_ascii=False).lower())

    missing = []
    for raw in lines:
        if not raw or len(raw) <= 2:
            continue
        low = raw.lower().strip(": ")
        if low in _FORM_CHROME:
            continue
        if any(low.startswith(pfx) for pfx in _CHROME_PREFIX):
            continue
        if raw.count(" ") >= 9 and not raw.endswith(":"):
            continue                                    # a sentence
        if raw[:1].islower() or raw[:1] in "(“‘":
            continue                                    # a continuation
        bare = re.sub(r"\(\s*[+-]?\d+(?:\.\d+)?\s*(?:points?)?\s*\)?\s*$", "", raw)
        s = squash(bare)
        if not s or s in known or s in blob:
            continue
        # The form wraps a field's first option onto the label's own line:
        # "Serum albumin less than 3.0 g/dL? Yes".
        stripped = re.sub(r"\s+(yes|no|unknown)$", "", bare, flags=re.I)
        if squash(stripped) in known or squash(stripped) in blob:
            continue
        if _is_column_header(raw, known, blob):
            continue
        missing.append(raw)
    rep.decide("printed_form_complete", not missing,
               f"{len(missing)} printed line(s) with nothing in the spec",
               missing[:6])


def check_bounds(rep: Report, spec: dict, js: str) -> None:
    """Bounds the script enforces are the bounds the spec enforces."""
    mmc = _slice_function(js, "minMaxCheck") or ""
    stated = set(re.findall(r"is\s+(-?\d+(?:\.\d+)?)\s", mmc))
    # A constant the script annotates as unused is not a limit it enforces:
    # ALS declares `var AGE_MIN = 29; //days -- not used in validation`.
    for m in re.finditer(
            r"(?:var|let|const)\s+[A-Z][A-Z0-9_]*?_(?:MIN|MAX)\s*=\s*"
            r"(-?\d*\.?\d+)\s*;?[ \t]*"
            r"(?://[ \t]*((?:(?!\b(?:var|let|const|function)\b)[^\n])*))?", js):
        if re.search(r"not\s+used", m.group(2) or "", re.I):
            continue
        stated.add(m.group(1))
    if not stated:
        rep.add("bounds_match_script", NA, "the script states no bounds")
        return

    held: set[str] = set()
    for i in spec.get("inputs") or []:
        for k in ("min", "max"):
            v = (i.get("constraints") or {}).get(k)
            if isinstance(v, (int, float)):
                held.add(f"{v:g}")
        for mode in i.get("constraints_by_mode") or []:
            for k in ("min", "max"):
                if isinstance(mode.get(k), (int, float)):
                    held.add(f"{mode[k]:g}")
        d = i.get("derived_constraints") or {}
        for k in ("min", "max"):
            if isinstance(d.get(k), (int, float)):
                held.add(f"{d[k]:g}")
    # A limit on a value the calculator works out is still a limit the spec
    # carries -- it just does not belong to a field.
    for lim in ((spec.get("content") or {}).get("stated_limits") or []):
        for k in ("min", "max"):
            if isinstance(lim.get(k), (int, float)):
                held.add(f"{lim[k]:g}")

    lost = sorted({f"{float(s):g}" for s in stated} - held)
    rep.decide("bounds_match_script", not lost,
               f"{len(lost)} bound(s) the script states that no field holds", lost[:8])


def check_options(rep: Report, spec: dict) -> None:
    """Each select offers distinct, usable choices."""
    problems = []
    for i in spec.get("inputs") or []:
        opts = i.get("options") or []
        if not opts:
            continue
        values = [str(o.get("value")) for o in opts]
        labels = [squash(o.get("label") or "") for o in opts]
        if len(set(values)) == 1 and len(opts) > 1:
            problems.append(f"{i['key']}: {len(opts)} options all worth {values[0]}")
        # A dependent list carries one set of options per variant, so the same
        # label legitimately appears once per variant -- Framingham prints
        # "No (0)" for each sex. It is only a repeat if nothing tells them
        # apart.
        variants = {str(o.get("variant_value")) for o in opts}
        if len(set(labels)) != len(labels) and not (
            i.get("options_depend_on") and len(variants) > 1
        ):
            dupe = [l for l, n in Counter(labels).items() if n > 1]
            problems.append(f"{i['key']}: repeated option label {dupe[:2]}")
        if any(l.startswith("option") and l[6:].isdigit() for l in labels):
            problems.append(f"{i['key']}: options are still placeholders")
    rep.decide("options_usable", not problems, "; ".join(problems[:3]), problems[:6])


def _live_under_another_choice(spec: dict, base: dict, field: dict) -> bool:
    """Does this select change the answer once some other select is moved?"""
    for other in spec.get("inputs") or []:
        if other is field:
            continue
        alts = [o for o in (other.get("options") or [])
                if o.get("value") not in (None, "")]
        if len(alts) < 2:
            continue
        for alt in alts[:6]:
            vec = dict(base)
            vec[other["key"]] = alt["value"]
            seen = set()
            for o in field.get("options") or []:
                if o.get("value") in (None, ""):
                    continue
                probe = dict(vec)
                probe[field["key"]] = o["value"]
                try:
                    got = _evaluate(spec, probe)
                except ExpressionError:
                    seen.add("__error__")
                    continue
                seen.add(json.dumps({k: round(v, 9) for k, v in got.items()
                                     if isinstance(v, (int, float))},
                                    sort_keys=True))
            if "__error__" not in seen and len(seen) > 1:
                return True
    return False


def check_selects_live(rep: Report, spec: dict) -> None:
    """Every select changes something, or gates a field that does."""
    if not (spec.get("compute") or {}).get("outputs"):
        rep.add("selects_change_the_answer", NA, "no expressions to move")
        return
    dead = []
    base = _vector(spec)
    for i in spec.get("inputs") or []:
        opts = [o for o in (i.get("options") or []) if o.get("value") not in (None, "")]
        if len(opts) < 2:
            continue
        if any((f.get("visible_when") or {}).get("field") == i["key"]
               for f in spec.get("inputs") or []):
            continue                                    # it gates a field
        rule = i.get("visible_when")
        if rule:
            chosen = base.get(rule.get("field"))
            if chosen is not None and chosen not in (rule.get("equals") or []):
                continue        # the form is not showing this one right now
        seen, informative = set(), False
        for o in opts:
            vec = dict(base)
            vec[i["key"]] = o["value"]
            for other in spec.get("inputs") or []:
                rule = other.get("visible_when")
                if rule and rule.get("field") == i["key"] \
                        and o["value"] not in (rule.get("equals") or []):
                    vec.pop(other["key"], None)
            try:
                got = _evaluate(spec, vec)
            except ExpressionError:
                seen.add("__error__")
                continue
            if any(isinstance(v, (int, float)) and v and math.isfinite(v)
                   for v in got.values()):
                informative = True
            seen.add(json.dumps({k: round(v, 9) for k, v in got.items()
                                 if isinstance(v, (int, float))}, sort_keys=True))
        if informative and "__error__" not in seen and len(seen) == 1:
            # A choice can matter only under another choice: ethanol's route
            # changes nothing while the output is in mg, and everything once it
            # is in mL. Retry against each alternative the other selects offer
            # before calling this one dead.
            if not _live_under_another_choice(spec, base, i):
                dead.append(i["key"])
    rep.decide("selects_change_the_answer", not dead,
               f"{len(dead)} select(s) the calculation ignores", dead)


def check_units(rep: Report, spec: dict) -> None:
    """A stated unit is a real unit, and a field's own unit is its identity."""
    problems = []
    for i in spec.get("inputs") or []:
        units = i.get("units") or []
        base = i.get("base_unit")
        if units and base:
            match = [u for u in units
                     if str(u.get("code", "")).lower() == str(base).lower()]
            if not match:
                problems.append(f"{i['key']}: base {base!r} is not in its own unit list")
            elif match[0].get("factor") != 1 or (match[0].get("offset") or 0) != 0:
                problems.append(
                    f"{i['key']}: base {base!r} carries factor {match[0].get('factor')}")
    for o in (spec.get("compute") or {}).get("outputs") or []:
        u = o.get("base_unit")
        if u and (len(str(u)) > 14 or re.search(r"[:<>=]", str(u))):
            problems.append(f"{o['key']}: {u!r} is prose, not a unit")
    rep.decide("units_coherent", not problems, "; ".join(problems[:3]), problems[:6])


def check_results_produced(rep: Report, spec: dict) -> None:
    """The calculator answers, for a patient inside its own stated range."""
    renderer = spec.get("renderer")
    if renderer in ("convert", "tree", "score", "dose_table"):
        rep.add("produces_a_result", NA, f"{renderer} is driven differently")
        return
    if not (spec.get("compute") or {}).get("outputs"):
        rep.decide("produces_a_result", False, "no outputs at all")
        return
    try:
        got = _evaluate(spec, _vector(spec))
    except Exception as exc:                             # noqa: BLE001
        rep.decide("produces_a_result", False, f"{type(exc).__name__}: {exc}")
        return
    bad = [k for k, v in got.items()
           if isinstance(v, (int, float)) and not math.isfinite(v)]
    rep.decide("produces_a_result", not bad,
               f"{len(bad)} output(s) are not finite for a mid-range patient", bad)


def check_tables(rep: Report, spec: dict) -> None:
    """A table the spec carries is usable: ordered, keyed, and reachable."""
    tables = spec.get("tables") or {}
    problems = []
    for t in tables.get("lookups") or []:
        rows = t.get("rows") or []
        if len(rows) < 2:
            problems.append(f"{t.get('key')}: {len(rows)} row(s)")
            continue
        th = [r.get("threshold") for r in rows if r.get("threshold") is not None]
        if any(b <= a for a, b in zip(th, th[1:])):
            problems.append(f"{t.get('key')}: thresholds do not increase")
        cols = set(t.get("columns") or [])
        if any(cols - set(r) for r in rows):
            problems.append(f"{t.get('key')}: a row is missing a column")
    ladder = tables.get("dose_ladder")
    if ladder and not ladder.get("values"):
        problems.append("dose ladder is empty")
    rep.decide("tables_usable", not problems, "; ".join(problems[:3]), problems[:6])


def check_source_text(rep: Report, spec: dict, sec: dict) -> None:
    """The document's own words -- disclaimer, notes, references -- are kept."""
    content = spec.get("content") or {}
    missing = []
    if sec.get("Data Input Disclaimer") and not content.get("data_input_disclaimer"):
        missing.append("data input disclaimer")
    if sec.get("Disclaimer") and not content.get("disclaimer"):
        missing.append("disclaimer")
    if sec.get("Copyright") and not content.get("copyright"):
        missing.append("copyright")
    refs = re.findall(r"\[PubMed\s+(\d+)\]", sec.get("References", "") or "")
    held = {r.get("pubmed_id") for r in content.get("references") or []}
    lost = sorted(set(refs) - held)
    if lost:
        missing.append(f"{len(lost)} PubMed id(s): {lost[:3]}")
    body = (sec.get("Notes") or "") + (sec.get("Additional Information") or "")
    if body.strip() and not (content.get("notes") or content.get("equation")
                             or content.get("instructions")):
        missing.append("notes / formula prose")
    rep.decide("source_text_kept", not missing, "; ".join(missing), missing)


def check_formula_constants(rep: Report, spec: dict) -> None:
    """A number the printed equation states is a number the code uses."""
    printed = (spec.get("content") or {}).get("equation") or ""
    if not printed.strip():
        rep.add("formula_constants_present", NA, "no printed equation")
        return
    compute = spec.get("compute") or {}
    code = " ".join(
        [s.get("expr") or "" for s in compute.get("steps") or []]
        + [o.get("expr") or "" for o in compute.get("outputs") or []]
        + [str(o.get("value")) for i in spec.get("inputs") or []
           for o in i.get("options") or []]
        + [str(x) for v in ((spec.get("tables") or {}).get("variants") or {}).values()
           for opt in v.get("options") or [] for x in (opt.get("constants") or {}).values()]
    )

    def nums(text: str) -> set[str]:
        # "mL/minute/1.73 m2" states the surface area a clearance is normalised
        # to -- it is part of the unit, not a coefficient the code should hold.
        text = re.sub(r"(?<![\w.])1\.73\s*m\s*(?:2|\u00b2|\^2)?\b", " ", text)
        out = set()
        for raw in re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)", text):
            n = raw.rstrip("0").rstrip(".") if "." in raw else raw
            if len(n) < 2 or n in {"10", "12", "24", "60", "100", "1000"}:
                continue
            if n.isdigit() and len(n) == 4 and 1900 <= int(n) <= 2100:
                continue                                 # a citation year
            out.add(n)
        return out

    lost = sorted(nums(_formula_lines(printed)) - nums(code))
    if lost:
        lost = [n for n in lost
                if not _folded_into(n, printed, code)
                and not _range_edge(n, printed, code)]
    rep.add("formula_constants_present", PASS if not lost else FAIL,
            f"{len(lost)} constant(s) the equation names and the code never uses",
            lost[:8])


def _all_numbers(text: str) -> set[float]:
    out = set()
    for raw in re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)", text or ""):
        try:
            out.add(float(raw))
        except ValueError:
            pass
    return out


def _folded_into(missing: str, printed: str, code: str) -> bool:
    """Did the code multiply this constant into another one?

    Labetalol prints `Infuse Rate = (Dose * 60) / (Drug Amount / 250)` and the
    script ships `dose * 15000 / drugAmount`. 250 is not absent -- it is
    60 x 250, folded at the source. Reported as a missing coefficient it sent a
    reader to check a formula that was already right.
    """
    try:
        m = float(missing)
    except ValueError:
        return False
    if m == 0:
        return False
    others = _all_numbers(printed) | {60.0, 24.0, 100.0, 1000.0, 10.0, 12.0}
    others.discard(m)
    for c in _all_numbers(code):
        for q in others:
            if q == 0:
                continue
            for folded in (m * q, m / q, q / m):
                if abs(c - folded) <= 1e-9 * max(1.0, abs(c)):
                    return True
    return False


# A line that states an equation, or maps a band to a value ("Clcr 40-59 ...:
# 36 hours"). Everything else in a Formulas section is prose about the formula.
_BAND_LINE = re.compile(r"^[^=\n]*\d[^=\n]*:\s*\S*\d")


def _formula_lines(printed: str) -> str:
    """Just the lines that state arithmetic.

    The Formulas section runs into narrative -- applicability ("intended for
    patients younger than 18"), unit restatements ("60 inches (152 cm)"),
    citations, strengths ("a 98% solution"). Every number in those looked like a
    coefficient the code had lost, and each one sent a reader to re-check a
    formula that was already right.
    """
    keep = []
    for line in (printed or "").split("\n"):
        if "=" in line or _BAND_LINE.match(line):
            keep.append(line)
    return "\n".join(keep)


def _range_edge(missing: str, printed: str, code: str) -> bool:
    """The far edge of a band the code tests by its near edge.

    "Clcr 40-59 mL/min: 36 hours" is written `clcr >= 40` -- 59 is the same
    boundary said twice, not a constant that went missing.
    """
    for lo, hi in re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)\s*[-\u2013]\s*"
                             r"(\d+(?:\.\d+)?)(?![\w.])", printed or ""):
        if missing not in (lo, hi):
            continue
        other = hi if missing == lo else lo
        if re.search(r"(?<![\w.])" + re.escape(other) + r"(?![\d])", code or ""):
            return True
        # The band below ends one unit under the band above's floor.
        try:
            near = float(other)
        except ValueError:
            continue
        for delta in (1.0, -1.0, 0.1, -0.1):
            probe = f"{near + delta:g}"
            if re.search(r"(?<![\w.])" + re.escape(probe) + r"(?![\d])", code or ""):
                return True
    return False


def check_conditional_fields(rep: Report, spec: dict) -> None:
    """A field shown only under a condition points at a control that exists."""
    keys = {i["key"] for i in spec.get("inputs") or []}
    problems = []
    for i in spec.get("inputs") or []:
        rule = i.get("visible_when")
        if not rule:
            continue
        ctrl = next((f for f in spec.get("inputs") or []
                     if f["key"] == rule.get("field")), None)
        if ctrl is None:
            problems.append(f"{i['key']}: gated by {rule.get('field')!r}, which is not a field")
            continue
        offered = {str(o.get("value")) for o in ctrl.get("options") or []}
        want = {str(v) for v in rule.get("equals") or []}
        if offered and not (want & offered):
            problems.append(f"{i['key']}: visible for {sorted(want)}, which {ctrl['key']} never offers")
    for i in spec.get("inputs") or []:
        dep = i.get("options_depend_on")
        if dep and dep not in keys:
            problems.append(f"{i['key']}: options depend on {dep!r}, which is not a field")
    rep.decide("conditional_fields_consistent", not problems,
               "; ".join(problems[:3]), problems[:6])


def check_no_phantom_fields(rep: Report, spec: dict) -> None:
    """The form asks for what it needs, and nothing it works out for itself."""
    steps = {s["key"] for s in (spec.get("compute") or {}).get("steps") or []}
    problems = []
    for i in spec.get("inputs") or []:
        if i["key"] in steps:
            problems.append(f"{i['key']} is computed, not entered")
        if re.fullmatch(r".+_unit(_list)?", i["key"]):
            host = next((f for f in spec.get("inputs") or []
                         if f["key"] == re.sub(r"_unit(_list)?$", "", i["key"])), None)
            if host and len(host.get("units") or []) > 1:
                problems.append(f"{i['key']} duplicates {host['key']}'s unit picker")
    labels = Counter(squash(i.get("label") or "") for i in spec.get("inputs") or [])
    for lab, n in labels.items():
        if lab and n > 1:
            problems.append(f"{n} fields share the label {lab!r}")
    rep.decide("no_phantom_fields", not problems, "; ".join(problems[:3]), problems[:6])


def check_score(rep: Report, spec: dict) -> None:
    """A score's criteria, points and bands agree with each other."""
    if spec.get("renderer") != "score":
        rep.add("score_consistent", NA, "not a score")
        return
    scoring = spec.get("scoring") or {}
    groups = scoring.get("groups") or []
    problems = []
    if not groups:
        problems.append("no criteria")
    for g in groups:
        opts = g.get("options") or []
        if len(opts) >= 2:
            continue
        # A criterion with one choice worth points is a tick, not a broken list:
        # REVEAL scores pericardial effusion 1 point if present and nothing if
        # not. One choice worth NOTHING is the broken case -- the palliative
        # score's three ladders, of which the PDF printed only the 0-point rung.
        if opts and opts[0].get("points"):
            continue
        # A gap the spec already declares is reported to the clinician on the
        # page; this queue is for what nobody has looked at yet.
        if any(c.get("kind") == "score_criteria_frozen"
               and str(g.get("key")) in (c.get("fields") or [])
               for c in ((spec.get("provenance") or {}).get("conflicts") or [])):
            continue
        problems.append(
            f"{g.get('key')}: one choice, worth no points -- nothing can move it")
    lo = sum(min((o["points"] for o in g["options"]), default=0) for g in groups)
    hi = sum(max((o["points"] for o in g["options"]), default=0) for g in groups)
    bands = scoring.get("bands") or []
    if bands:
        covered = [b for b in bands
                   if (b.get("min") is None or b["min"] <= hi)
                   and (b.get("max") is None or b["max"] >= lo)]
        if not covered:
            problems.append(f"no band covers the reachable range {lo}-{hi}")
        # The document's own bands are the yardstick. Several scores print no
        # interpretation below zero even though a negative criterion exists, so
        # a total under the lowest printed band is the source being silent, not
        # the extraction losing something.
        floor = min((b["min"] for b in bands if b.get("min") is not None),
                    default=lo)
        for total in (max(lo, floor), hi):
            hit = [b for b in bands
                   if (b.get("min") is None or total >= b["min"])
                   and (b.get("max") is None or total <= b["max"])]
            if not hit:
                problems.append(f"a total of {total:g} falls in no band")
    rep.decide("score_consistent", not problems, "; ".join(problems[:3]), problems[:6])


CHECKS = (
    check_printed_form,
    check_bounds,
    check_options,
    check_selects_live,
    check_units,
    check_results_produced,
    check_tables,
    check_source_text,
    check_formula_constants,
    check_conditional_fields,
    check_no_phantom_fields,
    check_score,
)

# Checks that take the PDF sections or the script as well as the spec.
NEEDS_SECTIONS = {check_printed_form, check_source_text}
NEEDS_JS = {check_bounds}


def reconcile(pdf: Path, spec: dict) -> Report:
    rep = Report(spec["slug"], spec.get("title") or spec["slug"])
    sec = split_sections(normalize_text(strip_boilerplate(read_pdf_text(pdf))))
    js = sec.get("Scripts", "") or ""
    for fn in CHECKS:
        try:
            if fn in NEEDS_SECTIONS:
                fn(rep, spec, sec)
            elif fn in NEEDS_JS:
                fn(rep, spec, js)
            else:
                fn(rep, spec)
        except Exception as exc:                          # noqa: BLE001
            rep.add(fn.__name__, FAIL, f"the check itself failed: "
                                       f"{type(exc).__name__}: {exc}")
    return rep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calculators", type=Path,
                    default=ROOT.parent.parent / "calculators")
    ap.add_argument("--pdfs", type=Path, default=ROOT.parent / "source-pdfs")
    ap.add_argument("--out", type=Path, default=ROOT / "dist" / "reports" / "reconcile")
    ap.add_argument("--only", help="one slug or a fragment of one")
    ap.add_argument("--failing", action="store_true", help="print only failures")
    ap.add_argument("--show", type=int, default=30)
    args = ap.parse_args()

    if not args.pdfs.is_dir():
        raise SystemExit(f"no source PDFs at {args.pdfs}")
    if not args.calculators.is_dir():
        raise SystemExit(f"no specs at {args.calculators}")

    by_source: dict[str, Path] = {p.name: p for p in args.pdfs.rglob("*.pdf")}
    args.out.mkdir(parents=True, exist_ok=True)

    reports: list[Report] = []
    for path in sorted(args.calculators.glob("*.json")):
        if path.name in ("index.json", "categories.json"):
            continue
        spec = json.loads(path.read_text())
        if args.only and args.only not in spec["slug"]:
            continue
        source = ((spec.get("provenance") or {}).get("pdf") or {}).get("source_file")
        pdf = by_source.get(source or "")
        if pdf is None:
            rep = Report(spec["slug"], spec.get("title") or spec["slug"])
            rep.decide("source_pdf_found", False, f"no PDF named {source!r}")
            reports.append(rep)
            continue
        rep = reconcile(pdf, spec)
        reports.append(rep)
        (args.out / f"{spec['slug']}.json").write_text(
            json.dumps(rep.as_dict(), indent=2, ensure_ascii=False))

    verified = [r for r in reports if r.verified]
    failing = [r for r in reports if not r.verified]
    by_check = Counter(f["check"] for r in failing for f in r.failures)

    print(f"\n{C['b']}══ Per-calculator reconciliation ══{C['r']}")
    print(f"  calculators   {len(reports)}")
    print(f"  verified      {C['ok']}{len(verified)}{C['r']}"
          f"   {C['dim']}every check passed{C['r']}")
    print(f"  to read       {C['warn'] if failing else C['dim']}{len(failing)}{C['r']}")
    if by_check:
        print(f"\n{C['b']}  by check:{C['r']}")
        for name, n in by_check.most_common():
            print(f"    {C['bad']}{n:>4}{C['r']}  {name}")

    if failing and not args.failing:
        print(f"\n{C['b']}  calculators to read:{C['r']}")
        for r in failing[: args.show]:
            first = r.failures[0]
            print(f"    {C['bad']}·{C['r']} {r.slug[:40]:<40} "
                  f"[{first['check']}] {first['detail'][:52]}")
        if len(failing) > args.show:
            print(f"    {C['dim']}… {len(failing) - args.show} more{C['r']}")

    if args.failing:
        for r in failing:
            print(f"\n{C['b']}{r.slug}{C['r']}  {C['dim']}{r.title}{C['r']}")
            for f in r.failures:
                print(f"    {C['bad']}✗{C['r']} {f['check']}: {f['detail']}")
                if f.get("evidence"):
                    print(f"        {C['dim']}{f['evidence']}{C['r']}")

    summary = args.out.parent / "reconcile.json"
    summary.write_text(json.dumps({
        "calculators": len(reports),
        "verified": len(verified),
        "to_read": len(failing),
        "by_check": dict(by_check),
        "results": [r.as_dict() for r in reports],
    }, indent=2, ensure_ascii=False))
    print(f"\n{C['dim']}→ {summary}  (one file per calculator in {args.out.name}/){C['r']}\n")
    return 1 if failing else 0


if __name__ == "__main__":
    raise SystemExit(main())
