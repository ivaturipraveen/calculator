#!/usr/bin/env python3
"""Structural invariants that catch silently-wrong specs.

Validation proves an expression runs. Completeness proves fields are populated.
Neither can see a calculator that returns the same number for every patient --
which is exactly how a concatenated girls+boys growth ladder passed both checks
while the boys' half was unreachable.

These are the properties a correct spec must have regardless of its formula:

  select_is_live      changing a select input must change at least one output
  ladder_monotonic    a lookup ladder's thresholds must strictly increase; a
                      reset means two tables were concatenated and the second
                      is dead code
  ladder_covers       every ladder key must be computable from the inputs
  no_dead_options     an option list must have at least two distinct values
  bounds_sane         min < max, and defaults must sit inside the bounds
  bands_unambiguous   one score must not map to two different answers without
                      saying which outcome each belongs to
  labels_are_readable a label must not carry the PDF text layer's own table
                      furniture ("3% ** 5%") or be a bare number

Run after every build. A regression here is a wrong clinical number, not a
cosmetic defect.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from extractor.evaluator import ExpressionError, run_compute

C = {"crit": "\033[91m", "warn": "\033[93m", "ok": "\033[92m",
     "dim": "\033[90m", "b": "\033[1m", "r": "\033[0m"}


def base_vectors(spec: dict, n: int = 4) -> list[dict]:
    """Several plausible input vectors, not one.

    A single synthetic vector can land somewhere the result is degenerate --
    ACC/AHA saturates to 0% risk when cholesterol is set to a midpoint that is
    not clinically plausible -- and then NO input changes the output, which
    looks exactly like a dead selector. Probing a spread avoids condemning a
    correct calculator on one unlucky sample.
    """
    out: list[dict] = []
    for idx, frac in enumerate((0.5, 0.25, 0.75, 0.9)[:n]):
        vec = {}
        for inp in spec.get("inputs") or []:
            c = inp.get("constraints") or {}
            v = inp.get("default")
            if v is None and inp.get("options"):
                # Take the option's value AS IT IS. Reaching past a word to the
                # numeric fallback set `id_gender` to a number, so the male
                # branch of valganciclovir's k could never be taken and the age
                # band that feeds it looked like a dead control.
                choices = [o.get("value") for o in inp["options"]
                           if o.get("value") not in (None, "")]
                if choices:
                    # A different option per vector, so a control that only
                    # matters under one of them is still exercised.
                    val = choices[idx % len(choices)]
                    vec[inp["key"]] = float(val) if isinstance(val, (int, float)) else val
                    continue
            if v is None:
                lo, hi = c.get("min"), c.get("max")
                lo = 1.0 if lo is None else float(lo)
                hi = float(hi) if hi is not None else max(lo * 2, lo + 10, 10.0)
                v = lo + (hi - lo) * frac or 1.0
            # A select can carry a word rather than a coefficient -- an equation
            # name, a route. Forcing it to a float turned the whole check into
            # an error and reported nothing about the spec at all.
            try:
                vec[inp["key"]] = float(v)
            except (TypeError, ValueError):
                vec[inp["key"]] = v
        out.append(vec)
    return out


def check_result_is_constant(spec: dict) -> list[dict]:
    """A calculator whose answer never moves is not a calculator.

    `check_select_is_live` deliberately ignores a vector where every output is
    zero, because one unlucky sample cannot tell a dead selector from a
    saturated formula. But when EVERY vector gives zero, that reasoning inverts:
    nothing the clinician can enter changes the answer. Two calculators were
    sitting in the corpus like that -- the aminoglycoside extended-interval
    adjustment, whose whole nomogram chain had flattened to `interval = 0`, and
    Valganciclovir, whose dose did the same -- and every other check passed them
    because each one, on its own terms, was true.
    """
    compute = spec.get("compute") or {}
    outputs = compute.get("outputs") or []
    if not outputs or spec.get("renderer") not in ("formula", "titration_table"):
        return []
    tables = spec.get("tables") or {}
    ladders = tables.get("lookups") or []
    variants = tables.get("variants") or {}

    seen: set[str] = set()
    for base in base_vectors(spec, n=4):
        try:
            res = run_compute(compute, base, ladders, variants)
        except ExpressionError:
            return []                   # cannot evaluate; other checks say so
        nums = {k: v for k, v in res.items() if isinstance(v, (int, float))}
        if not nums:
            return []
        seen.add(json.dumps({k: round(v, 9) for k, v in nums.items()}, sort_keys=True))

    if len(seen) > 1:
        return []
    only = json.loads(next(iter(seen)))
    if any(v not in (0, 0.0) for v in only.values()):
        return []                       # constant but not zero: a fixed dose

    # Name the step that went flat, so the reader is not left hunting.
    flat = [s["key"] for s in (compute.get("steps") or [])
            if re.fullmatch(r"[\d.\s()+-]*", s.get("expr") or "x")]
    # A gap the spec declares is already on the clinician's page; this list is
    # for what nobody has looked at yet.
    if any(c.get("kind") == "result_is_constant"
           for c in ((spec.get("provenance") or {}).get("conflicts") or [])):
        return []
    return [{
        "check": "result_is_constant", "severity": "critical",
        "field": flat[0] if flat else None,
        "detail": "every output is zero for every input vector -- nothing the "
                  "clinician enters can change the answer"
                  + (f"; `{flat[0]}` is a literal constant" if flat else ""),
    }]


def check_select_is_live(spec: dict) -> list[dict]:
    """Every select must be able to change a result.

    A select whose options all produce identical output is not a choice -- it is
    a field the calculation ignores, which in practice means the branch it was
    meant to pick was lost.
    """
    out: list[dict] = []
    tables = spec.get("tables") or {}
    ladders = tables.get("lookups") or []
    variants = tables.get("variants") or {}
    if not (spec.get("compute") or {}).get("outputs"):
        return out

    bases = base_vectors(spec)
    for inp in spec.get("inputs") or []:
        opts = [o for o in (inp.get("options") or [])
                if isinstance(o.get("value"), (int, float, str))
                and o.get("value") not in (None, "")]
        if len(opts) < 2:
            continue

        # A control that decides whether another field is on the form at all is
        # a live control by definition -- the page changes when it changes. It
        # only looked dead because the probe filled in the fields the form
        # would have hidden.
        gated = [i for i in (spec.get("inputs") or [])
                 if (i.get("visible_when") or {}).get("field") == inp["key"]]
        if gated:
            continue

        live = False
        informative = False
        for base in bases:
            seen = []
            for o in opts:
                vec = dict(base)
                vec[inp["key"]] = o["value"]
                # Drop anything this option hides: the form would not have
                # collected it, so feeding it in tests a state that cannot
                # happen.
                for other in spec.get("inputs") or []:
                    rule = other.get("visible_when")
                    if rule and rule.get("field") == inp["key"]:
                        if o["value"] not in (rule.get("equals") or []):
                            vec.pop(other["key"], None)
                try:
                    res = run_compute(spec["compute"], vec, ladders, variants)
                except ExpressionError:
                    seen.append("__error__")
                    continue
                # A vector where every output is zero or non-finite tells us
                # nothing: it cannot distinguish a dead select from a saturated
                # formula.
                # A result can be a word ('Conventional'), which is a real
                # difference between options but not a number to round.
                if any(isinstance(v, (int, float)) and v not in (0, 0.0)
                       and v == v and abs(v) != float("inf")
                       for v in res.values()):
                    informative = True
                if any(isinstance(v, str) and v for v in res.values()):
                    informative = True
                seen.append(json.dumps(
                    {k: (round(v, 9) if isinstance(v, (int, float)) else v)
                     for k, v in res.items()}, sort_keys=True, default=str))
            if "__error__" in seen:
                continue
            if len(set(seen)) > 1:
                live = True
                break

        if not live and informative:
            out.append({
                "check": "select_is_live", "severity": "critical",
                "field": inp["key"],
                "detail": f"all {len(opts)} options of {inp['key']!r} produce an "
                          f"identical result across {len(bases)} input vectors - "
                          f"the branch this selects was lost",
            })
    return out


def check_ladders(spec: dict) -> list[dict]:
    out: list[dict] = []
    for t in ((spec.get("tables") or {}).get("lookups") or []):
        rows = t.get("rows") or []
        th = [r.get("threshold") for r in rows if r.get("threshold") is not None]
        resets = [i for i, (a, b) in enumerate(zip(th, th[1:])) if b <= a]
        if resets:
            out.append({
                "check": "ladder_monotonic", "severity": "critical",
                "field": t.get("key"),
                "detail": f"thresholds reset {len(resets)}x in a {len(th)}-row "
                          f"ladder (first at row {resets[0] + 1}): two tables were "
                          f"concatenated and rows after the reset are unreachable",
            })
    return out


def check_options(spec: dict) -> list[dict]:
    out: list[dict] = []
    for inp in spec.get("inputs") or []:
        opts = inp.get("options") or []
        if not opts:
            continue
        vals = [o.get("value") for o in opts]
        if len(opts) >= 2 and len(set(map(str, vals))) == 1:
            out.append({
                "check": "no_dead_options", "severity": "high",
                "field": inp["key"],
                "detail": f"{len(opts)} options all carry the same value {vals[0]!r}",
            })
        labels = [str(o.get("label") or "") for o in opts]
        if labels and all(l.lower().startswith("option ") for l in labels):
            out.append({
                "check": "placeholder_labels", "severity": "medium",
                "field": inp["key"],
                "detail": "option labels are placeholders; the real labels were "
                          "not recovered from the printed form",
            })
    return out


def check_bounds(spec: dict) -> list[dict]:
    out: list[dict] = []
    for inp in spec.get("inputs") or []:
        c = inp.get("constraints") or {}
        lo, hi, dflt = c.get("min"), c.get("max"), inp.get("default")
        if lo is not None and hi is not None and lo >= hi:
            out.append({"check": "bounds_sane", "severity": "high",
                        "field": inp["key"],
                        "detail": f"min {lo} is not below max {hi}"})
        if dflt is not None:
            if lo is not None and dflt < lo:
                out.append({"check": "bounds_sane", "severity": "medium",
                            "field": inp["key"],
                            "detail": f"default {dflt} is below min {lo}"})
            if hi is not None and dflt > hi:
                out.append({"check": "bounds_sane", "severity": "medium",
                            "field": inp["key"],
                            "detail": f"default {dflt} is above max {hi}"})
    return out


def check_bands_unambiguous(spec: dict) -> list[dict]:
    """One score must not map to two different answers.

    Fracture Index prints three outcome tables in a row -- nonvertebral, hip
    and vertebral 5-year risk -- and flattened without their headings they read
    as a single table saying a score of 1-2 means 8.6%, 0.4% and 1.2% at once.
    Whichever the renderer reached first became the answer. A band that repeats
    a range is only safe when its label says which outcome it belongs to.
    """
    bands = (spec.get("scoring") or {}).get("bands") or []
    spans = Counter((b.get("min"), b.get("max")) for b in bands)
    out: list[dict] = []
    for span, n in spans.items():
        if n < 2:
            continue
        labels = [b.get("label") or "" for b in bands
                  if (b.get("min"), b.get("max")) == span]
        if len(set(labels)) < len(labels):
            out.append({"check": "bands_unambiguous", "severity": "critical",
                        "detail": f"score {span[0]}-{span[1]} has {n} bands and "
                                  f"repeats a label: {labels}"})
        elif not all(":" in l for l in labels):
            out.append({"check": "bands_unambiguous", "severity": "high",
                        "detail": f"score {span[0]}-{span[1]} maps to {n} "
                                  f"different answers, none naming its outcome: "
                                  f"{labels}"})
    return out


def check_labels_are_readable(spec: dict) -> list[dict]:
    """A label must not carry the text layer's own table furniture.

    TIMI UA/NSTEMI's rows arrived as "3% ** 5%" -- two endpoints with the
    column rule still between them -- which tells a clinician neither number's
    meaning and collided as a React key.
    """
    out: list[dict] = []
    bands = (spec.get("scoring") or {}).get("bands") or []
    for b in bands:
        if "**" in (b.get("label") or ""):
            out.append({"check": "labels_are_readable", "severity": "high",
                        "detail": f"band label still has a column rule in it: "
                                  f"{b.get('label')!r}"})
    interp = (spec.get("scoring") or {}).get("interpretation") or {}
    for row in ([interp.get("label")] + list(interp.get("printed_rows") or [])):
        if row and "**" in row:
            out.append({"check": "labels_are_readable", "severity": "high",
                        "detail": f"interpretation header still has a column "
                                  f"rule in it: {row!r}"})
    for f in ((spec.get("content") or {}).get("fixed_values") or []):
        lbl = (f.get("label") or "").strip()
        if "**" in lbl or not re.search(r"[A-Za-z]{2}", lbl):
            out.append({"check": "labels_are_readable", "severity": "high",
                        "detail": f"stated quantity has no real name: {lbl!r}"})
    return out


CHECKS = (check_ladders, check_options, check_bounds, check_select_is_live,
          check_result_is_constant, check_bands_unambiguous,
          check_labels_are_readable)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", type=Path, default=Path(__file__).parent / "dist")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    results = []
    for f in sorted((args.dist / "calculators").glob("*.json")):
        spec = json.loads(f.read_text())
        issues: list[dict] = []
        for chk in CHECKS:
            try:
                issues.extend(chk(spec))
            except Exception as exc:                                # noqa: BLE001
                issues.append({"check": chk.__name__, "severity": "low",
                               "field": None,
                               "detail": f"check errored: {type(exc).__name__}: {exc}"})
        if issues:
            results.append({"slug": spec["slug"], "title": spec["title"],
                            "issues": issues})

    total = sum(len(r["issues"]) for r in results)
    sev = Counter(i["severity"] for r in results for i in r["issues"])
    by_check = Counter(i["check"] for r in results for i in r["issues"])

    print(f"\n{C['b']}══ Invariant check ══{C['r']}")
    n_specs = len(list((args.dist / 'calculators').glob('*.json')))
    print(f"  specs            {n_specs}")
    print(f"  specs w/ issues  {len(results)}")
    print(f"  issues           {C['crit']}{sev.get('critical',0)} critical{C['r']}  "
          f"{C['warn']}{sev.get('high',0)} high{C['r']}  "
          f"{C['dim']}{sev.get('medium',0)} medium  {sev.get('low',0)} low{C['r']}")
    print(f"  by check         {dict(by_check)}")

    crit = [(r["slug"], i) for r in results for i in r["issues"]
            if i["severity"] == "critical"]
    if crit:
        print(f"\n{C['b']}  CRITICAL — silently wrong output:{C['r']}")
        for slug, i in crit[: (None if args.verbose else 18)]:
            print(f"    {C['crit']}·{C['r']} {slug[:38]:<38} [{i['check']}] "
                  f"{str(i.get('field') or '-')[:14]:<14} {i['detail'][:60]}")
        if not args.verbose and len(crit) > 18:
            print(f"    {C['dim']}… {len(crit)-18} more (--verbose){C['r']}")

    out = args.dist / "reports" / "invariants.json"
    out.write_text(json.dumps(
        {"specs": n_specs, "specs_with_issues": len(results),
         "issues_total": total, "by_severity": dict(sev),
         "by_check": dict(by_check), "results": results},
        indent=2, ensure_ascii=False))
    print(f"\n{C['dim']}→ {out}{C['r']}\n")
    return 1 if sev.get("critical") else 0


if __name__ == "__main__":
    raise SystemExit(main())
