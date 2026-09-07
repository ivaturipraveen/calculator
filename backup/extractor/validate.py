#!/usr/bin/env python3
"""Exercise every built spec, so a spec that cannot compute never ships.

Static extraction can produce a spec that looks complete and still fails at
runtime -- an expression referencing a variable nothing defines, a lookup key
that is never computed, a score with no reachable total. This runs each spec the
way the API will and records what actually happens.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from extractor.evaluator import ExpressionError, free_names, run_compute

C = {"crit": "\033[91m", "warn": "\033[93m", "ok": "\033[92m",
     "dim": "\033[90m", "b": "\033[1m", "r": "\033[0m"}


def sample_inputs(spec: dict) -> dict[str, float]:
    """One plausible value per field, and never two the same.

    A select has no numeric default -- the user must choose -- so its first
    option stands in. For everything else the sample walks across the field's
    range rather than sitting on the midpoint: identical values create
    artificial singularities, and a pharmacokinetic rate written as
    `ln(peak/trough) / (t_trough - t_peak)` divides by zero the instant both
    times receive the same number. This is a test fixture, not a default.
    """
    vec: dict[str, float] = {}
    for idx, inp in enumerate(spec.get("inputs") or []):
        c = inp.get("constraints") or {}
        v = inp.get("default")

        if v is None and inp.get("options"):
            val = inp["options"][0].get("value")
            if isinstance(val, (int, float)):
                vec[inp["key"]] = float(val)
                continue
            if isinstance(val, str):
                vec[inp["key"]] = val
                continue

        if v is None:
            lo = c.get("min")
            hi = c.get("max")
            lo = 1.0 if lo is None else float(lo)
            hi = float(hi) if hi is not None else max(lo * 2, lo + 10, 10.0)
            span = hi - lo
            v = lo + span * (0.31 + 0.11 * (idx % 6))
        try:
            v = float(v)
        except (TypeError, ValueError):
            vec[inp["key"]] = v
            continue
        if v == 0:
            v = 1.0 + idx
        vec[inp["key"]] = v
    return vec



def check(spec: dict) -> dict:
    slug, renderer = spec["slug"], spec["renderer"]
    res = {"slug": slug, "renderer": renderer, "ok": False, "problems": []}

    if renderer == "score":
        groups = (spec.get("scoring") or {}).get("groups") or []
        if not groups:
            res["problems"].append("no criterion groups")
        elif any(not g.get("options") for g in groups):
            res["problems"].append("a group has no options")
        else:
            total = spec["scoring"]["total"]
            res["ok"] = True
            res["detail"] = (f"{len(groups)} groups, "
                             f"range {total['min']}-{total['max']}")
            if not spec["scoring"].get("bands"):
                res["problems"].append("no interpretation bands")
        return res

    if renderer == "titration_table":
        ladder = (spec.get("tables") or {}).get("dose_ladder") or {}
        outs = (spec.get("compute") or {}).get("outputs") or []
        if not ladder.get("values"):
            res["problems"].append("no dose ladder")
        elif not outs:
            res["problems"].append("no per-row expression")
        else:
            vec = sample_inputs(spec)
            vec["dose"] = ladder["values"][0]
            try:
                vals = run_compute(spec["compute"], vec, None)
                res["ok"] = True
                res["detail"] = (f"{ladder['count']} rows; "
                                 + ", ".join(f"{k}={v:.4g}" for k, v in list(vals.items())[:2]))
                if ladder.get("truncated_in_pdf"):
                    res["problems"].append("dose ladder truncated in the PDF")
            except Exception as exc:                                # noqa: BLE001
                res["problems"].append(f"row eval: {exc}")
        return res

    # Data-driven renderers carry a table instead of an expression; they are
    # complete when that table is present and non-empty.
    DATA_KEYS = ("pairs", "drugs", "nodes", "thresholds", "admit_dx", "sections")
    if renderer in ("convert", "dose_table", "tree"):
        tbl = spec.get("tables") or {}
        present = [(k, tbl[k]) for k in DATA_KEYS if tbl.get(k)]
        if present:
            res["ok"] = True
            res["detail"] = ", ".join(f"{len(v)} {k}" for k, v in present)
        else:
            res["problems"].append(f"no data table (looked for {', '.join(DATA_KEYS)})")
        return res

    outs = (spec.get("compute") or {}).get("outputs") or []
    if not outs:
        res["problems"].append("no outputs")
        return res

    # every identifier an expression uses must be resolvable
    defined = {i["key"] for i in spec.get("inputs") or []}
    defined |= {s["key"] for s in (spec["compute"].get("steps") or [])}
    # Outputs are evaluated in order and may legitimately reference an earlier
    # one (percentile uses z_score; a delta gap uses the anion gap).
    defined |= {o["key"] for o in outs}
    tbls = (spec.get("tables") or {}).get("lookups") or []
    table = tbls or (spec.get("tables") or {}).get("lookup")
    for t in (tbls or []):
        defined |= set(t.get("columns") or [])
    variants = (spec.get("tables") or {}).get("variants") or {}
    for vspec in variants.values():
        defined |= set(vspec.get("variables") or [])
    for o in outs:
        if not o.get("expr"):
            res["problems"].append(f"output {o['key']} has no expression")
            continue
        try:
            missing = free_names(o["expr"]) - defined - {o["key"]}
        except ExpressionError as exc:
            res["problems"].append(f"output {o['key']}: {exc}")
            continue
        if missing:
            res["problems"].append(f"output {o['key']} references undefined {sorted(missing)}")

    if res["problems"]:
        return res

    try:
        vals = run_compute(spec["compute"], sample_inputs(spec), table, variants)
        bad = [k for k, v in vals.items()
               if v is None or isinstance(v, bool) or v != v or abs(v) == float("inf")]
        if bad:
            res["problems"].append(f"non-finite outputs: {bad}")
        else:
            res["ok"] = True
            res["detail"] = ", ".join(f"{k}={v:.4g}" for k, v in list(vals.items())[:3])
    except ExpressionError as exc:
        res["problems"].append(f"runtime: {exc}")
    except Exception as exc:                                       # noqa: BLE001
        res["problems"].append(f"runtime: {type(exc).__name__}: {exc}")
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", type=Path, default=Path(__file__).parent / "dist")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    files = sorted((args.dist / "calculators").glob("*.json"))
    results = [check(json.loads(f.read_text())) for f in files]

    by_renderer: dict[str, Counter] = {}
    for r in results:
        by_renderer.setdefault(r["renderer"], Counter())[
            "ok" if r["ok"] else "fail"] += 1

    ok = sum(1 for r in results if r["ok"])
    print(f"\n{C['b']}══ Spec validation ══{C['r']}")
    print(f"  {C['ok']}{ok}{C['r']}/{len(results)} specs compute successfully\n")
    for rend, c in sorted(by_renderer.items()):
        total = c["ok"] + c["fail"]
        col = C["ok"] if c["fail"] == 0 else C["warn"]
        print(f"  {rend:<12} {col}{c['ok']:>3}/{total}{C['r']}")

    fails = [r for r in results if not r["ok"]]
    if fails:
        print(f"\n{C['b']}  failing:{C['r']}")
        for r in fails[: (None if args.verbose else 20)]:
            print(f"    {C['crit']}✗{C['r']} {r['slug'][:46]:<46} "
                  f"{C['dim']}{'; '.join(r['problems'])[:60]}{C['r']}")
        if not args.verbose and len(fails) > 20:
            print(f"    {C['dim']}… {len(fails)-20} more (--verbose){C['r']}")

    (args.dist / "reports" / "validation.json").write_text(
        json.dumps({"ok": ok, "total": len(results), "results": results},
                   indent=2, ensure_ascii=False))
    print(f"\n{C['dim']}→ {args.dist}/reports/validation.json{C['r']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
