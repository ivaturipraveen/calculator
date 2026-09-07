#!/usr/bin/env python3
"""Compare each spec's arithmetic against the formula its PDF prints.

Every other check tests the spec against itself: that it parses, that it runs,
that its selectors are live. None of them can tell whether the expression the
extractor recovered is the expression the document describes -- a formula can be
perfectly evaluable and still be the wrong formula.

The document usually states it in prose:

    Estimated_creatinine_clearance = Sex * ((140 - Age) / Serum_creatinine) * (Weight / 72)

The strongest evidence in that line is its NUMBERS. A coefficient is specific:
if the prose says 140 and 72 and the recovered expression contains neither, the
extraction took a different path through the script. Variable names are weaker
evidence -- the prose spells them out, the script abbreviates -- so they are
folded before comparing and reported separately.

This is a review queue, not a verdict. A constant that appears only in the prose
may be inside a helper that was inlined by value; a constant only in the code may
be a unit conversion the prose leaves implicit. It ranks what to read.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
C = {"ok": "\033[92m", "bad": "\033[91m", "warn": "\033[93m",
     "dim": "\033[90m", "b": "\033[1m", "r": "\033[0m"}

NUM = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)")
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Numbers that carry no information about which formula this is: they appear in
# every other line of prose and in every third expression.
COMMON = {"0", "1", "2", "100", "10", "1000", "60", "24", "12", "0.5"}

# Words the prose uses that are never variables.
STOPWORDS = {
    "the", "and", "or", "of", "in", "is", "are", "for", "to", "by", "with", "a",
    "an", "if", "then", "else", "where", "using", "used", "calculated", "value",
    "values", "equation", "formula", "note", "notes", "units", "unit", "kg",
    "mg", "ml", "cm", "in", "lb", "lbs", "yr", "years", "male", "female",
    "measured", "estimate", "estimated", "method", "methods", "see", "below",
    "above", "table", "tables", "patient", "dose", "doses", "x", "e", "ln",
    "log", "exp", "sqrt", "power", "pow", "round", "abs", "min", "max",
}


# The same three rules `reconcile.py` applies, imported rather than restated so
# the two tools cannot disagree about what counts as a missing coefficient.
from reconcile import _folded_into, _formula_lines, _range_edge   # noqa: E402


def numbers(text: str) -> Counter:
    """Informative numeric constants, at the precision the text states them.

    A citation year is not a coefficient. The prose around a formula is thick
    with them -- "(CDC [Dowell 2022])", "Cockcroft-Gault 1976" -- and every one
    of them looked like a number the code had lost.
    """
    # "mL/minute/1.73 m2" is the surface area a clearance is normalised to --
    # part of the unit, not a coefficient the code should hold.
    text = re.sub(r"(?<![\w.])1\.73\s*m\s*(?:2|\u00b2|\^2)?\b", " ", text or "")
    out: Counter = Counter()
    for raw in NUM.findall(text or ""):
        norm = raw.rstrip("0").rstrip(".") if "." in raw else raw
        if not norm or norm in COMMON or len(norm) < 2:
            continue
        if norm.isdigit() and len(norm) == 4 and 1900 <= int(norm) <= 2100:
            continue                        # a year, from a reference
        out[norm] += 1
    return out


def words(text: str) -> set[str]:
    return {
        w.lower().replace("_", "")
        for w in WORD.findall(text or "")
        if w.lower() not in STOPWORDS and len(w) > 2
    }


def expression_text(spec: dict) -> str:
    """Everything the spec computes with, as one blob."""
    compute = spec.get("compute") or {}
    parts = [s.get("expr") or "" for s in compute.get("steps") or []]
    parts += [o.get("expr") or "" for o in compute.get("outputs") or []]
    # A coefficient can live in a variant constant set or a lookup ladder rather
    # than in an expression; both are still "what this calculator computes with".
    tables = spec.get("tables") or {}
    for v in (tables.get("variants") or {}).values():
        for opt in v.get("options") or []:
            parts += [str(x) for x in (opt.get("constants") or {}).values()]
    for inp in spec.get("inputs") or []:
        parts += [str(o.get("value")) for o in (inp.get("options") or [])]
    for t in tables.get("lookups") or []:
        for row in (t.get("rows") or [])[:400]:
            parts += [str(x) for x in row.values()]
    return " ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calculators", type=Path,
                    default=ROOT.parent.parent / "calculators")
    ap.add_argument("--show", type=int, default=18)
    ap.add_argument("--json", type=Path,
                    default=ROOT / "dist" / "reports" / "formula_verification.json")
    args = ap.parse_args()

    if not args.calculators.is_dir():
        raise SystemExit(f"no specs at {args.calculators}")

    rows: list[dict] = []
    checked = skipped = clean = 0

    for path in sorted(args.calculators.glob("*.json")):
        if path.name in ("index.json", "categories.json"):
            continue
        spec = json.loads(path.read_text())
        printed = (spec.get("content") or {}).get("equation") or ""
        if not printed.strip():
            skipped += 1
            continue
        checked += 1

        code = expression_text(spec)
        # Only the lines that state arithmetic. The Formulas section runs into
        # narrative -- applicability, unit restatements, citations, strengths --
        # and every number in those looked like a lost coefficient.
        p_nums, c_nums = numbers(_formula_lines(printed)), numbers(code)
        missing = sorted(set(p_nums) - set(c_nums), key=lambda n: -len(n))
        # A constant the code multiplied into another (labetalol prints
        # `(Dose * 60) / (Drug Amount / 250)` and ships `dose * 15000`), and the
        # far edge of a band the code tests by its near edge, are both present.
        missing = [n for n in missing
                   if not _folded_into(n, printed, code)
                   and not _range_edge(n, printed, code)]
        extra = sorted(set(c_nums) - set(p_nums), key=lambda n: -len(n))

        p_words = words(printed)
        keys = {i["key"].replace("_", "") for i in spec.get("inputs") or []}
        keys |= {o["key"].replace("_", "")
                 for o in ((spec.get("compute") or {}).get("outputs") or [])}
        keys |= {s["key"].replace("_", "")
                 for s in ((spec.get("compute") or {}).get("steps") or [])}
        # A prose word counts as matched if any key contains it or it contains
        # a key: "serumcreatinine" against `sr_cr` will not match, and that is
        # reported rather than assumed away.
        unmatched = sorted(
            w for w in p_words
            if not any(w in k or k in w for k in keys if len(k) > 2)
        )

        if not missing and not unmatched[:1]:
            clean += 1
            continue

        rows.append({
            "slug": spec["slug"],
            "title": spec.get("title"),
            "printed_only_constants": missing[:8],
            "code_only_constants": extra[:6],
            "printed_words_without_a_field": unmatched[:8],
            "severity": "high" if missing else "low",
            "printed": re.sub(r"\s+", " ", printed)[:260],
        })

    rows.sort(key=lambda r: (r["severity"] != "high", -len(r["printed_only_constants"])))
    high = [r for r in rows if r["severity"] == "high"]

    print(f"\n{C['b']}══ Printed formula vs recovered arithmetic ══{C['r']}")
    print(f"  specs with a printed formula   {checked}")
    print(f"  no printed formula to check    {C['dim']}{skipped}{C['r']}")
    print(f"  agree                          {C['ok']}{clean}{C['r']}")
    print(f"  to read                        {C['warn']}{len(rows)}{C['r']}"
          f"  ({C['bad']}{len(high)} with a constant the code never uses{C['r']})")

    if high:
        print(f"\n{C['b']}  A number the document states that the code does not:{C['r']}")
        for r in high[: args.show]:
            print(f"    {C['bad']}·{C['r']} {r['slug'][:40]:<40} "
                  f"missing {r['printed_only_constants']}")
        if len(high) > args.show:
            print(f"    {C['dim']}… {len(high) - args.show} more{C['r']}")

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(
        {"checked": checked, "skipped": skipped, "agree": clean,
         "to_read": len(rows), "with_missing_constant": len(high), "rows": rows},
        indent=2, ensure_ascii=False))
    print(f"\n{C['dim']}→ {args.json}{C['r']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
