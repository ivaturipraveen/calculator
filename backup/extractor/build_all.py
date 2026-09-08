#!/usr/bin/env python3
"""Build the complete calculator dataset from PDFs + the mockup HTML.

    python3 build_all.py <pdf_folder> [--html mockup.html] [--out dist/]

Output layout (dist/):

    calculators/<slug>.json     one complete, self-contained spec each
    index.json                  light list for the API's list endpoint
    units/registry.json         shared unit dimensions, deduplicated
    categories.json             category facet
    reports/build_report.json   per-calculator extraction record
    reports/review_queue.json   everything a human must look at
    reports/coverage.json       what each source contributed, field by field
"""

from __future__ import annotations

import argparse
import ast
import json
import shutil
import re
import subprocess
import sys
import traceback
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from extractor import build
from extractor.evaluator import ExpressionError, run_compute
from invariants import base_vectors as inv_base_vectors
from extractor.identity import squash
from extractor.jsparse import normalize_text
from extractor.matcher import MatchIndex, detect_collisions, unmatched_html
from extractor.pdf_extract import (
    extract,
    read_pdf_text,
    split_sections,
    strip_boilerplate,
)
from pipeline import dedupe_copies, looks_like_calculator

ROOT = Path(__file__).parent
C = {"crit": "\033[91m", "warn": "\033[93m", "ok": "\033[92m", "cy": "\033[96m",
     "dim": "\033[90m", "b": "\033[1m", "r": "\033[0m"}


_UNTERMINATED = re.compile(r"""(?P<q>['"])[^'"\n]*\Z""")


def truncated_export(sections: dict) -> str | None:
    """Describe how a PDF's text layer cut its own Scripts section short.

    Truncation is common here -- 56 of the 186 exports stop partway through a
    string -- and nearly always harmless, because it lands in the trailing
    ``test()`` / ``clrResults()`` helpers, long after the arithmetic. So this is
    NOT a health check for the corpus, and calling it on a spec that built fine
    would only cry wolf.

    It earns its keep in the one case where the cut takes the calculation with
    it. Both Rapid Sequence Intubation exports stop at
    ``dsstarthtm = dsstarthtm + '`` inside ``writeDoseSheet()`` -- the function
    that *is* the calculator -- so the crash-cart dose sheet is not in the file
    at all. Hence the single caller: a spec that already extracted to nothing,
    asking whether the content is missing or merely unparsed. Returns None when
    the section ends cleanly.
    """
    js = (sections or {}).get("Scripts") or ""
    if not js.strip():
        return None
    tail = js.rstrip()
    if not _UNTERMINATED.search(tail):
        return None
    # Name the function it died inside; that is what a re-export has to restore.
    fns = re.findall(r"function\s+(\w+)\s*\(", tail)
    where = f" inside {fns[-1]}()" if fns else ""
    return (f"source PDF's text layer stops mid-string{where} -- "
            f"the rest of the script is not in the file")


def slug_for(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower().replace("&", " and "))
    return s.strip("-")



def _borrow(field: dict, key: str, label: str, source: str, **over) -> dict:
    """Copy a field from another calculator, renamed and marked as borrowed."""
    out = dict(field)
    out["key"] = key
    out["label"] = label
    out["required"] = False
    out.update(over)
    out["source"] = {"kind": "borrowed_from_sibling", "slug": source,
                     "original_key": field["key"]}
    return out


def _expr_of(spec: dict, key: str) -> str:
    for coll in (spec["compute"].get("outputs") or [], spec["compute"].get("steps") or []):
        for item in coll:
            if item["key"] == key:
                return item["expr"]
    raise KeyError(key)


def _probe_vector(spec: dict, overrides: dict) -> dict:
    """A plausible value for every input, with `overrides` applied."""
    vec: dict = {}
    for inp in spec.get("inputs") or []:
        opts = [o.get("value") for o in (inp.get("options") or [])
                if o.get("value") not in (None, "")]
        if opts:
            vec[inp["key"]] = opts[0]
            continue
        c = inp.get("constraints") or {}
        lo, hi = c.get("min"), c.get("max")
        lo = 1.0 if lo is None else float(lo)
        hi = float(hi) if hi is not None else max(lo * 2, lo + 10, 10.0)
        vec[inp["key"]] = lo + (hi - lo) * 0.5 or 1.0
    vec.update(overrides)
    return vec


def _computes_without(spec: dict, vec: dict, key: str) -> bool:
    """Does the calculator still produce its answers with `key` left blank?"""
    probe = {k: v for k, v in vec.items() if k != key}
    tables = spec.get("tables") or {}
    try:
        run_compute(spec["compute"], probe, tables.get("lookups") or [],
                    tables.get("variants") or {})
    except ExpressionError:
        return False
    except Exception:                                    # noqa: BLE001
        return False
    return True


def relax_conditional_requirements(specs: list[dict]) -> int:
    """Stop demanding a value the chosen path never uses.

    Fentanyl asks for a drug amount and an infusate volume, but only reads them
    when the concentration is entered as "other"; the form refused to calculate
    until all seven boxes were full, five of which were all it needed. A field
    is only required for the options that actually consume it, and that is found
    by running the calculator with the field left out -- not by reading the
    expression and guessing.
    """
    relaxed = 0
    for spec in specs:
        if spec.get("renderer") not in ("formula", "titration_table"):
            continue
        inputs = spec.get("inputs") or []
        selects = [i for i in inputs
                   if len({str(o.get("value")) for o in (i.get("options") or [])
                           if o.get("value") not in (None, "")}) >= 2]
        if not selects:
            continue
        for field in inputs:
            if not field.get("required") or field.get("visible_when"):
                continue
            if field is in_selects(selects, field):
                continue
            for sel in selects:
                opts = [o.get("value") for o in (sel.get("options") or [])
                        if o.get("value") not in (None, "")]
                needed, spare = [], []
                for o in opts:
                    vec = _probe_vector(spec, {sel["key"]: o})
                    (spare if _computes_without(spec, vec, field["key"])
                     else needed).append(o)
                if needed and spare:
                    field["required"] = False
                    field["required_when"] = {"field": sel["key"],
                                              "equals": needed}
                    relaxed += 1
                    break
    return relaxed


def in_selects(selects: list[dict], field: dict):
    return field if any(s is field for s in selects) else None


def _branch_for(spec: dict, expr: str, value: str) -> str | None:
    """The arm of a conditional expression that applies when `value` is chosen.

    Usually that is the arm guarded by `field == value`. When the donor's own
    control has exactly two choices, the OTHER method is the else arm -- which
    is how the aminoglycoside initial-dose calculator writes its Jelliffe
    clearance: `... if (method == 'CockcroftGault') else <Jelliffe>`.
    """
    want = squash(value)
    opts = {
        i["key"]: [squash(str(o.get("value"))) for o in (i.get("options") or [])]
        for i in (spec.get("inputs") or [])
    }
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.IfExp):
            continue
        t = node.test
        if not (isinstance(t, ast.Compare) and len(t.ops) == 1
                and isinstance(t.ops[0], ast.Eq)
                and isinstance(t.left, ast.Name)
                and isinstance(t.comparators[0], ast.Constant)):
            continue
        named = squash(str(t.comparators[0].value))
        if named == want:
            return ast.unparse(node.body)
        choices = opts.get(t.left.id) or []
        if len(choices) == 2 and want in choices and named in choices:
            return ast.unparse(node.orelse)
    return None


_ZEROED = re.compile(r"\(\s*0(?:\.0+)?\s*\)\s*if\s*\(\s*(\w+)\s*==\s*'([^']+)'\s*\)")


def fix_zeroed_method_branches(specs: list[dict]) -> list[tuple[str, str, str]]:
    """Restore a named method whose branch the extraction reduced to zero.

    The aminoglycoside empiric-dosing script calls the shared include for its
    Jelliffe clearance; the PDF does not carry the include, so the branch came
    out as the literal 0 -- picking "Jelliffe" produced a creatinine clearance
    of zero and, from it, a dose. Another calculator built from the SAME corpus
    computes that branch, over fields of the same name, so the arithmetic is
    taken from there rather than typed in from memory.
    """
    fixed: list[tuple[str, str, str]] = []
    for spec in specs:
        keys = {i["key"] for i in spec.get("inputs") or []}
        items = ((spec.get("compute") or {}).get("steps") or []) + \
                ((spec.get("compute") or {}).get("outputs") or [])
        for item in items:
            for m in list(_ZEROED.finditer(item.get("expr") or "")):
                field, method = m.group(1), m.group(2)
                if field not in keys:
                    continue
                donor = donor_expr = None
                for other in specs:
                    if other is spec:
                        continue
                    o_items = ((other.get("compute") or {}).get("steps") or []) + \
                              ((other.get("compute") or {}).get("outputs") or [])
                    for o in o_items:
                        cand = _branch_for(other, o.get("expr") or "", method)
                        if not cand or re.fullmatch(r"[\d.()\s]*", cand):
                            continue
                        # A quoted option value is not a variable: counting
                        # 'female' as one made every donor look unusable.
                        bare = re.sub(r"'[^']*'|\"[^\"]*\"", "''", cand)
                        names = set(re.findall(
                            r"(?<![\w.])([a-z_]\w*)(?![\w(])", bare))
                        if names - keys - {"e", "pi", "if", "else", "and",
                                           "or", "not", "True", "False"}:
                            continue
                        donor, donor_expr = other, cand
                        break
                    if donor:
                        break
                if not donor:
                    continue
                item["expr"] = (item["expr"][:m.start()]
                                + f"({donor_expr}) if ({field} == '{method}')"
                                + item["expr"][m.end():])
                item["note"] = (f"the {method} branch is borrowed from "
                                f"{donor['slug']}, which computes it over the "
                                f"same fields; this PDF calls it from an include "
                                f"it does not carry")
                # The method must be offerable, or the branch is unreachable.
                for inp in spec.get("inputs") or []:
                    if inp["key"] != field:
                        continue
                    opts = inp.get("options") or []
                    if not any(str(o.get("value")) == method for o in opts):
                        opts.append({"label": method, "value": method,
                                     "source": "script comparison"})
                        inp["options"] = opts
                spec.setdefault("provenance", {}).setdefault("conflicts", []).append({
                    "kind": "method_branch_borrowed", "severity": "medium",
                    "detail": f"the {method} option was computing zero because the "
                              f"PDF does not carry the shared routine it calls; the "
                              f"arithmetic shown is {donor['slug']}'s, over the same "
                              f"fields -- check it before relying on that option",
                })
                fixed.append((spec["slug"], field, method))
    return fixed


def _fix_calvert_gfr(spec: dict, cg: dict, jl: dict) -> None:
    """Give Calvert back the GFR estimation its script performs."""
    keys = {i["key"] for i in spec["inputs"]}
    if "gfr_age" in keys:
        return

    cg_by = {i["key"]: i for i in cg["inputs"]}
    jl_by = {i["key"]: i for i in jl["inputs"]}

    # Cockcroft-Gault's sex coefficient is 0.85; Jelliffe's is 0.9. One control
    # cannot carry both, so the option value is the sex and each equation
    # applies its own coefficient.
    borrowed = [
        {
            "key": "gfr_equation", "label": "GFR equation", "widget": "select",
            "required": False, "default": "Cockcroft-Gault", "constraints": {},
            "options": [
                {"label": "Cockcroft-Gault", "value": "Cockcroft-Gault"},
                {"label": "Jelliffe", "value": "Jelliffe"},
            ],
            "help": ["Used only when GFR is not known. Jelliffe reports "
                     "mL/min/1.73m²."],
            "source": {"kind": "borrowed_from_sibling", "slug": cg["slug"]},
        },
        _borrow(cg_by["age"], "gfr_age", "Age (for GFR estimate)", cg["slug"]),
        _borrow(cg_by["weight"], "gfr_weight", "Weight (for GFR estimate)",
                cg["slug"], base_unit="kg", display_unit="kg"),
        _borrow(cg_by["serum_creatinine"], "gfr_srcr",
                "Serum creatinine (for GFR estimate)", cg["slug"]),
        {
            "key": "gfr_sex", "label": "Sex (for GFR estimate)", "widget": "select",
            "required": False, "constraints": {},
            "options": [{"label": "Female", "value": 0}, {"label": "Male", "value": 1}],
            "source": {"kind": "borrowed_from_sibling", "slug": cg["slug"]},
        },
    ]
    # None of these are asked for when a measured GFR is available.
    for f in borrowed:
        f["visible_when"] = {"field": "gfr_known", "equals": ["No"]}
    spec["inputs"].extend(borrowed)

    cg_expr = _expr_of(cg, "estimated_creatinine_clearance")
    jl_expr = _expr_of(jl, "crcl_normalized")
    for a, b in (("serum_creatinine", "gfr_srcr"), ("serum_creat", "gfr_srcr"),
                 ("weight", "gfr_weight"), ("age", "gfr_age")):
        cg_expr = re.sub(rf"(?<![\w.]){a}(?![\w])", b, cg_expr)
        jl_expr = re.sub(rf"(?<![\w.]){a}(?![\w])", b, jl_expr)
    cg_expr = re.sub(r"(?<![\w.])sex(?![\w])", "(0.85 if gfr_sex == 0 else 1)", cg_expr)
    jl_expr = re.sub(r"(?<![\w.])sex(?![\w])", "(0.9 if gfr_sex == 0 else 1)", jl_expr)

    # One expression, not three steps: a conditional is evaluated lazily, so
    # answering "GFR known: Yes" must not require the estimation's fields. As
    # separate steps they had to resolve first, and the calculator refused to
    # work at all without an age and a creatinine it did not need.
    estimate = (
        f"(({cg_expr}) if (gfr_equation == 'Cockcroft-Gault') else ({jl_expr}))"
    )
    # The form offers a ceiling; the script applies it only when one is set.
    capped = f"(min({estimate}, max_cr_cl_input) if (max_cr_cl_input > 0) else {estimate})"

    steps = spec["compute"].setdefault("steps", [])
    steps.append({
        "key": "gfr_used",
        "expr": f"calvert_gfr if (gfr_known == 'Yes') else {capped}",
        "note": f"the GFR the dose is built on; the estimate is borrowed from "
                f"{cg['slug']} / {jl['slug']}",
    })

    for item in steps + (spec["compute"].get("outputs") or []):
        if item["key"] == "dosage":
            item["expr"] = "calvert_auc * (25 + gfr_used)"
        elif item["key"] == "calvert_gfroutput":
            item["expr"] = "gfr_used"

    # The branch IS recovered now, so the warning that it was missing would be
    # two warnings about one thing, one of them no longer true.
    conflicts = spec.setdefault("provenance", {}).setdefault("conflicts", [])
    conflicts[:] = [c for c in conflicts if c.get("kind") != "library_helper_missing"]
    conflicts.append({
        "kind": "gfr_estimation_borrowed", "severity": "medium",
        "detail": "when GFR is not known it is estimated with the Cockcroft-Gault "
                  "or Jelliffe equation taken from this corpus's own calculators, "
                  "because the shared library the PDF calls is not in the PDF; "
                  "confirm against the live app before relying on it",
    })


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf_folder", type=Path)
    ap.add_argument("--html", type=Path,
                    default=ROOT.parent / "inpharmd-clinical-calculators-mockup_15.html")
    ap.add_argument("--out", type=Path, default=ROOT / "dist")
    ap.add_argument(
        "--publish",
        type=Path,
        default=ROOT.parent.parent / "calculators",
        help="where the served specs are copied; the API reads this folder",
    )
    ap.add_argument("--no-publish", action="store_true")
    args = ap.parse_args()

    dist = args.out
    # `calculators/` and `units/` are wholly regenerated, so they are emptied
    # first. Left to accumulate, a spec that stopped being built stayed on disk
    # and went on being published -- which is how two Rapid Sequence Intubation
    # pages survived a build that had excluded them. `reports/` is kept, so a
    # previous run's report is still there to diff against.
    for sub in ("calculators", "units"):
        shutil.rmtree(dist / sub, ignore_errors=True)
    for sub in ("calculators", "units", "reports"):
        (dist / sub).mkdir(parents=True, exist_ok=True)

    # ---- harvest the mockup -------------------------------------------
    print(f"{C['b']}{C['cy']}[1] HARVEST MOCKUP{C['r']}")
    harvest_path = dist / "reports" / "html_harvest.json"
    proc = subprocess.run(
        ["node", str(ROOT / "extractor" / "harvest_html.js"),
         str(args.html), str(harvest_path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"  {C['crit']}failed:{C['r']} {proc.stderr[:400]}")
        return 1
    print(f"  {proc.stderr.strip()}")
    harvest = json.loads(harvest_path.read_text())
    index = MatchIndex.from_harvest(harvest)

    # ---- ingest --------------------------------------------------------
    print(f"\n{C['b']}{C['cy']}[2] INGEST{C['r']}")
    pdfs = sorted(args.pdf_folder.rglob("*.pdf"))
    pdfs, dupes = dedupe_copies(pdfs)
    print(f"  {len(pdfs)} unique PDFs ({len(dupes)} duplicate re-exports skipped)")

    # ---- build ---------------------------------------------------------
    print(f"\n{C['b']}{C['cy']}[3] BUILD{C['r']}")
    specs, records, review, matches, failures = [], [], [], [], []
    counts: Counter[str] = Counter()
    unit_registry: dict[str, dict] = {}

    for path in pdfs:
        try:
            pdf_spec = extract(path)
            sections = split_sections(
                normalize_text(strip_boilerplate(read_pdf_text(path)))
            )
        except Exception as exc:                                   # noqa: BLE001
            failures.append({"file": path.name, "error": str(exc),
                             "trace": traceback.format_exc()[-1000:]})
            counts["failed"] += 1
            print(f"  {C['crit']}✗{C['r']} {path.name}: {exc}")
            continue

        if not looks_like_calculator(
                pdf_spec, bool(pdf_spec["info"].get("copyright")), sections):
            counts["not_a_calculator"] += 1
            review.append({"severity": "low", "stage": "ingest", "file": path.name,
                           "reason": "not a calculator export; excluded"})
            continue

        engine = build.detect_engine(sections)
        counts[f"engine_{engine}"] += 1

        m = index.lookup(pdf_spec["title"])
        m.pdf_slug, m.pdf_title = pdf_spec["slug"], pdf_spec["title"]
        matches.append(m)
        entry = (harvest.get("per_calculator") or {}).get(m.html_id) if m.html_id else None
        if entry is None and m.html_id:
            entry = next((e for e in index.entries if e.get("id") == m.html_id), None)

        spec = build.build_spec(
            pdf_spec, sections, entry, engine,
            data_globals=harvest.get("data_globals") or {},
        )

        # accumulate the shared unit registry
        for stem, u in build.units_from_constants(
            (entry or {}).get("constants") or {}
        ).items():
            key = "|".join(sorted(x["code"] for x in u["units"]))
            unit_registry.setdefault(key, {
                "dimension": u.get("dimension"), "base_unit": u.get("base_unit"),
                "units": u["units"], "used_by": [],
            })["used_by"].append(spec["slug"])

        # A spec that can neither ask for anything nor produce anything is not a
        # calculator page -- it is a blank one with a title on it. Shipping an
        # empty page that looks like a working calculator is not honest; naming
        # the gap is. Where the cause is a truncated source export, say so --
        # "no engine parses this" would send the next maintainer off to write an
        # engine for content that is not in the file to begin with.
        if not (spec.get("inputs") or (spec.get("compute") or {}).get("outputs")
                or (spec.get("tables") or {}) or (spec.get("scoring") or {}).get("groups")):
            counts["empty_after_extraction"] += 1
            cut = truncated_export(sections)
            review.append({
                "severity": "high", "stage": "build", "file": path.name,
                "reason": "extracted to nothing -- no inputs, outputs or tables; "
                          "excluded rather than published as a blank page",
                "cause": cut or "content present but in a shape no engine parses",
                "recoverable": "not from this file -- needs a complete re-export"
                               if cut else "possibly, with a new engine",
            })
            continue

        # ---- per-spec quality gates
        sev = None
        if spec["renderer"] == "unknown":
            sev = ("critical", "engine not recognised")
        elif spec["renderer"] == "score" and not (spec["scoring"] or {}).get("groups"):
            sev = ("critical", "score calculator with no criteria parsed")
        elif spec["renderer"] in ("formula",) and not spec["compute"]["outputs"]:
            sev = ("critical", "formula calculator with no outputs")
        elif spec["renderer"] == "formula" and any(
            not o.get("expr") for o in spec["compute"]["outputs"]
        ):
            missing = [o["key"] for o in spec["compute"]["outputs"] if not o.get("expr")]
            sev = ("critical", f"outputs without an expression: {missing}")
        if sev:
            counts["needs_review"] += 1
            review.append({"severity": sev[0], "stage": "build", "slug": spec["slug"],
                           "title": spec["title"], "engine": engine,
                           "reason": sev[1], "file": path.name})
        for c in spec["provenance"]["conflicts"]:
            review.append({"severity": c.get("severity", "medium"), "stage": "merge",
                           "slug": spec["slug"], "title": spec["title"],
                           "reason": c.get("kind"), "detail": c.get("detail"),
                           "field": c.get("field")})

        specs.append(spec)
        (dist / "calculators" / f"{spec['slug']}.json").write_text(
            json.dumps(spec, indent=2, ensure_ascii=False)
        )
        records.append({
            "slug": spec["slug"], "title": spec["title"], "file": path.name,
            "engine": engine, "renderer": spec["renderer"],
            "identity_verified": pdf_spec["provenance"]["identity"]["verified"],
            "match": m.method, "html_id": m.html_id,
            "inputs": len(spec["inputs"]),
            "outputs": len(spec["compute"]["outputs"]),
            "score_groups": len((spec["scoring"] or {}).get("groups", [])),
            "fields_with_bounds": sum(
                1 for i in spec["inputs"] if i.get("constraints")),
            "fields_with_units": sum(1 for i in spec["inputs"] if i.get("units")),
            "references": len(spec["content"]["references"]),
            "conflicts": len(spec["provenance"]["conflicts"]),
        })
        counts["built"] += 1

    # ---- complete the four-dimension metric converter --------------------
    # Its script converts temperature, weight, volume and length by calling the
    # shared library, so the PDF prints no factors at all and the mockup entry
    # carries only two of the four dimensions. The missing pairs are in this
    # same corpus -- the single-dimension converters state them -- so they are
    # taken from there rather than left as a hole or invented.
    _metric = next(
        (s for s in specs if s["slug"] == "unit-conversion-metric-conversions"), None)
    if _metric:
        _by_slug = {s["slug"]: s for s in specs}
        merged = list((_metric.get("tables") or {}).get("pairs") or [])
        seen = {(p.get("from"), p.get("to")) for p in merged}
        for dim, src in (("Temperature", "unit-conversions-temperature"),
                         ("Weight", "unit-conversions-weight"),
                         ("Volume", "unit-conversions-volume"),
                         ("Length", "unit-conversions-length")):
            donor = _by_slug.get(src)
            for pair in ((donor or {}).get("tables") or {}).get("pairs") or []:
                labelled = dict(pair)
                labelled["from"] = f"{dim}: {pair['from']}"
                labelled["source_slug"] = src
                if (labelled["from"], labelled["to"]) in seen:
                    continue
                seen.add((labelled["from"], labelled["to"]))
                merged.append(labelled)
        _metric.setdefault("tables", {})["pairs"] = merged
        _metric.setdefault("provenance", {})["pairs_completed_from"] = [
            "unit-conversions-temperature", "unit-conversions-weight",
            "unit-conversions-volume", "unit-conversions-length",
        ]
        (dist / "calculators" / f"{_metric['slug']}.json").write_text(
            json.dumps(_metric, indent=2, ensure_ascii=False))

    # ---- emit index / units / categories --------------------------------
    print(f"\n{C['b']}{C['cy']}[4] EMIT{C['r']}")
    idx = [{
        "slug": s["slug"], "title": s["title"], "category": s["category"],
        "renderer": s["renderer"], "status": s["status"],
        "inputs": len(s["inputs"]),
    } for s in sorted(specs, key=lambda x: x["title"].lower())]
    (dist / "index.json").write_text(json.dumps(
        {"count": len(idx), "generated": date.today().isoformat(), "calculators": idx},
        indent=2, ensure_ascii=False))

    # ---- Calvert: recover the GFR it estimates for itself ----------------
    # Answering "GFR known: No" makes the script estimate the GFR from age,
    # weight, creatinine and sex, by calling Cockcroft-Gault or Jelliffe out of
    # the shared include -- which the PDF does not carry. Neither the inputs nor
    # the arithmetic survived, so that path dosed carboplatin against a GFR of
    # zero. Both equations are in THIS corpus as calculators of their own, so
    # they are taken from there rather than typed in from memory, and each
    # borrowed field records where it came from.
    _calvert = next(
        (s for s in specs if s["slug"] == "calvert-formula-carboplatin-dosing"), None)
    _by_slug = {s["slug"]: s for s in specs}
    _cg = _by_slug.get("creatinine-clearance-by-cockcroft-gault-age-16-years")
    _jl = _by_slug.get("creatinine-clearance-by-jelliffe")
    if _calvert and _cg and _jl:
        _fix_calvert_gfr(_calvert, _cg, _jl)
        (dist / "calculators" / f"{_calvert['slug']}.json").write_text(
            json.dumps(_calvert, indent=2, ensure_ascii=False))

    # ---- a method whose branch the include took with it -----------------
    _borrowed = fix_zeroed_method_branches(specs)
    for _slug in {s for s, _f, _m in _borrowed}:
        (dist / "calculators" / f"{_slug}.json").write_text(
            json.dumps(_by_slug[_slug], indent=2, ensure_ascii=False))

    # ---- offer every field the units its dimension has ------------------
    # A field states its base unit but usually not the alternatives; those live
    # in the mockup, per calculator, and most calculators do not carry them. The
    # corpus as a whole does: once every calculator has been read, the shared
    # registry knows that mass is kg/lbs and length is cm/in, so a field whose
    # base unit belongs to a known dimension can be given the same choices. The
    # source is the corpus, not a table invented here, and each unit records
    # where it came from.
    _by_code: dict[str, dict] = {}
    for v in unit_registry.values():
        for u in v["units"]:
            _by_code.setdefault(squash(u["code"]), v)

    def _one_per_scale(units: list[dict], base_code: str) -> list[dict]:
        """One entry per actual unit, whatever the corpus calls it.

        The registry is built from every calculator's own spelling, so a length
        arrives as `meters`, `m`, `metre` and a temperature as `degC` and
        `Degrees C`. Offering all of them puts the same unit in the list twice
        and makes the picker look broken. Two codes that convert identically
        ARE the same unit; keep the one this field already names, else the
        longest spelling, which is the one a clinician reads without pausing.
        """
        want = squash(base_code)
        best: dict[tuple, dict] = {}
        for u in units:
            key = (round(float(u["factor"]), 12), round(float(u.get("offset") or 0), 12))
            cur = best.get(key)
            if cur is None:
                best[key] = u
                continue
            if squash(u["code"]) == want:
                best[key] = u
            elif squash(cur["code"]) != want and len(u["code"]) > len(cur["code"]):
                best[key] = u
        return list(best.values())

    def _compose(units: list[dict], base_code: str) -> list[dict]:
        """Re-express a dimension's units relative to THIS field's base unit."""
        base = next((u for u in units if squash(u["code"]) == squash(base_code)), None)
        if not base or not base.get("factor"):
            return []
        fb, ob = float(base["factor"]), float(base.get("offset") or 0.0)
        out = []
        for u in units:
            f, o = u.get("factor"), float(u.get("offset") or 0.0)
            if not f:
                continue
            out.append({
                "code": u["code"], "label": u.get("label") or u["code"],
                "factor": float(f) / fb, "offset": (o - ob) / fb,
                "resolved": True, "source": "corpus unit registry",
            })
        return _one_per_scale(out, base_code)

    # A `<field>_unit_list` select whose options are unit codes IS the field's
    # unit picker -- the script just never reads it, because the conversion
    # happens in an onchange handler the PDF does not carry. Left as its own
    # field it is a control that changes nothing: the invariant check called it
    # a dead select, and it was right. Turning it into the host's unit list
    # makes the choice do what the form promises.
    _promoted = 0
    for spec in specs:
        by_key = {i["key"]: i for i in spec.get("inputs") or []}
        used: set[str] = set()
        for coll in (spec["compute"].get("steps") or [],
                     spec["compute"].get("outputs") or []):
            for it in coll:
                used |= set(re.findall(r"(?<![\w.'\"])([A-Za-z_]\w*)(?![\w(])",
                                      it.get("expr") or ""))
        drop: set[str] = set()
        for inp in list(spec.get("inputs") or []):
            m = re.fullmatch(r"(.+?)_unit(?:_list)?", inp["key"])
            if not m or inp["key"] in used:
                continue
            host = by_key.get(m.group(1))
            codes = [str(o.get("value")) for o in (inp.get("options") or [])]
            if host is None or host.get("units") or len(codes) < 2:
                continue
            dim = next((d for d in (_by_code.get(squash(c)) for c in codes) if d), None)
            if not dim:
                continue
            # Compose against the field's OWN base unit, not simply the first
            # code the selector happens to list. Acetylcysteine's selector
            # offers lbs first, so its weights were composed against pounds
            # while the field still called kilograms its base -- every weight
            # entered in kg was multiplied by 2.2.
            declared = host.get("base_unit")
            base = next(
                (c for c in codes if declared and squash(c) == squash(declared)),
                next((c for c in codes if squash(c) in _by_code
                      and _by_code[squash(c)] is dim), codes[0]),
            )
            composed = _compose(dim["units"], base)
            picked = [u for u in composed if squash(u["code"]) in {squash(c) for c in codes}]
            if len(picked) < 2:
                continue
            host["units"] = picked
            host["base_unit"] = base
            host["display_unit"] = host.get("display_unit") or base
            host["units_source"] = f"unit selector {inp['key']!r} + corpus registry"
            drop.add(inp["key"])
            _promoted += 1
        if drop:
            spec["inputs"] = [i for i in spec["inputs"] if i["key"] not in drop]
            (dist / "calculators" / f"{spec['slug']}.json").write_text(
                json.dumps(spec, indent=2, ensure_ascii=False))
    counts["unit_selectors_promoted"] = _promoted

    _augmented = 0
    for spec in specs:
        touched = False
        for inp in spec.get("inputs") or []:
            if inp.get("units") or inp.get("options"):
                continue
            code = inp.get("base_unit") or inp.get("unit")
            if not code:
                continue
            dim = _by_code.get(squash(code))
            if not dim or len(dim["units"]) < 2:
                continue
            composed = _compose(dim["units"], code)
            if len(composed) < 2:
                continue
            inp["units"] = composed
            inp["display_unit"] = inp.get("display_unit") or code
            inp.setdefault("dimension", dim.get("dimension"))
            inp["units_source"] = "corpus unit registry"
            touched = True
            _augmented += 1
        if touched:
            (dist / "calculators" / f"{spec['slug']}.json").write_text(
                json.dumps(spec, indent=2, ensure_ascii=False))
    counts["fields_given_registry_units"] = _augmented

    # ---- an answer that never moves is not an answer --------------------
    # The aminoglycoside extended-interval adjustment reads its interval off a
    # nomogram GRID that the script builds as HTML, so the arithmetic here is
    # only the `var interval = 0` that grid was going to overwrite. Every other
    # check passes it -- the fields are real, the bounds are real, the notes are
    # real -- and it returns 0 to every patient. Say so on the page.
    _flat = 0
    for spec in specs:
        if spec.get("renderer") not in ("formula", "titration_table"):
            continue
        if not (spec.get("compute") or {}).get("outputs"):
            continue
        tables = spec.get("tables") or {}
        seen = set()
        try:
            for vec in inv_base_vectors(spec, n=4):
                res = run_compute(spec["compute"], vec,
                                  tables.get("lookups") or [], tables.get("variants") or {})
                nums = {k: v for k, v in res.items() if isinstance(v, (int, float))}
                if not nums:
                    seen = {"?"}
                    break
                seen.add(tuple(sorted((k, round(v, 9)) for k, v in nums.items())))
        except Exception:                                        # noqa: BLE001
            continue
        if len(seen) == 1 and "?" not in seen and all(
                v == 0 for _k, v in next(iter(seen))):
            spec.setdefault("provenance", {}).setdefault("conflicts", []).append({
                "kind": "result_is_constant", "severity": "critical",
                "detail": "this calculator returns zero whatever is entered: its "
                          "answer is read off a nomogram grid the source builds as "
                          "HTML, and only the initial value survived extraction -- "
                          "use the source calculator for this one",
            })
            (dist / "calculators" / f"{spec['slug']}.json").write_text(
                json.dumps(spec, indent=2, ensure_ascii=False))
            _flat += 1
    counts["results_that_never_move"] = _flat

    # ---- ask only for what the chosen path uses -------------------------
    _relaxed = relax_conditional_requirements(specs)
    counts["fields_required_only_on_some_paths"] = _relaxed
    for spec in specs:
        if any(i.get("required_when") for i in spec.get("inputs") or []):
            (dist / "calculators" / f"{spec['slug']}.json").write_text(
                json.dumps(spec, indent=2, ensure_ascii=False))

    # ---- one entry per unit, however many spellings reached it ----------
    # Whichever pass supplied the list -- the field's own script, a promoted
    # selector, the corpus registry -- the same unit can arrive twice under two
    # spellings ("hrs" and "hr", "Degrees C" and "degC"), and the picker then
    # offers the clinician the same choice twice.
    _deduped = 0
    for spec in specs:
        touched = False
        for inp in spec.get("inputs") or []:
            units = inp.get("units") or []
            if len(units) < 2:
                continue
            trimmed = _one_per_scale(
                [u for u in units if u.get("factor")],
                inp.get("base_unit") or inp.get("display_unit") or "")
            if len(trimmed) != len(units):
                # Keep the printed order, so the base unit stays where it was.
                keep = {id(u) for u in trimmed}
                inp["units"] = [u for u in units if id(u) in keep]
                _deduped += len(units) - len(inp["units"])
                touched = True
        if touched:
            (dist / "calculators" / f"{spec['slug']}.json").write_text(
                json.dumps(spec, indent=2, ensure_ascii=False))
    counts["duplicate_units_removed"] = _deduped

    # A page drawn twice can cut a ladder row in half -- "S = = 0.048..." --
    # leaving a row that carries L and M but no S. The complete copy of the
    # same row is right beside it, so the incomplete one is dropped rather than
    # left to make one age's z-score uncomputable.
    _mended = 0
    for spec in specs:
        touched = False
        for table in ((spec.get("tables") or {}).get("lookups") or []):
            cols = set(table.get("columns") or [])
            rows = table.get("rows") or []
            if not cols:
                continue
            complete = {}
            for r in rows:
                th = r.get("threshold")
                if th is not None and not (cols - set(r)):
                    complete.setdefault(th, r)
            kept = []
            for r in rows:
                if cols - set(r):
                    twin = complete.get(r.get("threshold"))
                    if twin is not None:
                        _mended += 1
                        touched = True
                        continue            # the whole row is already here
                    for missing in cols - set(r):
                        r[missing] = None
                    _mended += 1
                    touched = True
                kept.append(r)
            if touched:
                table["rows"] = kept
                table["row_count"] = len(kept)
        if touched:
            (dist / "calculators" / f"{spec['slug']}.json").write_text(
                json.dumps(spec, indent=2, ensure_ascii=False))
    if _mended:
        counts["ladder_rows_mended"] = _mended

    # A field's base unit must be the identity in its own list -- that is what
    # "base" means, and every bound and formula is written in it. Anything else
    # is a silent multiplier on a clinical value, so it is repaired here rather
    # than shipped.
    _rebased = 0
    for spec in specs:
        touched = False
        for inp in spec.get("inputs") or []:
            units, base = inp.get("units") or [], inp.get("base_unit")
            if not units or not base:
                continue
            hit = next((u for u in units
                        if squash(u.get("code")) == squash(base)), None)
            if hit is None or (hit.get("factor") == 1 and not (hit.get("offset") or 0)):
                continue
            fb, ob = float(hit["factor"]), float(hit.get("offset") or 0.0)
            for u in units:
                if not u.get("factor"):
                    continue
                u["factor"] = float(u["factor"]) / fb
                u["offset"] = (float(u.get("offset") or 0.0) - ob) / fb
            inp["units_rebased_to"] = base
            touched = True
            _rebased += 1
        if touched:
            (dist / "calculators" / f"{spec['slug']}.json").write_text(
                json.dumps(spec, indent=2, ensure_ascii=False))
    if _rebased:
        counts["unit_lists_rebased"] = _rebased

    registry_out = []
    for i, (key, v) in enumerate(sorted(unit_registry.items())):
        registry_out.append({
            "id": f"dim_{i:03d}", "dimension": v["dimension"],
            "base_unit": v["base_unit"], "units": v["units"],
            "used_by_count": len(v["used_by"]), "used_by": sorted(set(v["used_by"]))[:20],
        })
    (dist / "units" / "registry.json").write_text(json.dumps(
        {"count": len(registry_out), "dimensions": registry_out}, indent=2, ensure_ascii=False))

    cats = Counter(s["category"] for s in specs if s["category"])
    (dist / "categories.json").write_text(json.dumps(
        {"count": len(cats),
         "categories": [{"name": k, "calculators": v} for k, v in sorted(cats.items())]},
        indent=2, ensure_ascii=False))

    # coverage: what each source contributed
    cov = {
        "fields_total": sum(len(s["inputs"]) for s in specs),
        "fields_with_bounds_from_pdf": sum(
            1 for s in specs for i in s["inputs"] if i.get("constraints")),
        "fields_with_units_from_html": sum(
            1 for s in specs for i in s["inputs"] if i.get("units")),
        "specs_with_category_from_html": sum(1 for s in specs if s["category"]),
        "specs_with_references": sum(1 for s in specs if s["content"]["references"]),
        "specs_with_equation_text": sum(1 for s in specs if s["content"]["equation"]),
        "specs_with_lms_tables": sum(1 for s in specs if (s.get("tables") or {}).get("lms")),
    }
    (dist / "reports" / "coverage.json").write_text(json.dumps(cov, indent=2))

    collisions = detect_collisions(matches)
    orphans = unmatched_html(index, matches)
    (dist / "reports" / "build_report.json").write_text(json.dumps({
        "summary": dict(counts), "collisions": collisions,
        "mockup_without_pdf": orphans, "failures": failures,
        "calculators": records,
    }, indent=2, ensure_ascii=False))
    review.sort(key=lambda r: {"critical": 0, "high": 1, "medium": 2}.get(r["severity"], 3))
    (dist / "reports" / "review_queue.json").write_text(
        json.dumps(review, indent=2, ensure_ascii=False))

    # ---- summary --------------------------------------------------------
    eng = {k.removeprefix("engine_"): v for k, v in counts.items() if k.startswith("engine_")}
    print(f"\n{C['b']}══ Build summary ══{C['r']}")
    print(f"  built                {C['ok']}{counts['built']}{C['r']}"
          f"  failed {C['crit'] if counts['failed'] else C['dim']}{counts['failed']}{C['r']}")
    print(f"  engines              {eng}")
    print(f"  renderers            {dict(Counter(s['renderer'] for s in specs))}")
    print(f"  unit dimensions      {len(registry_out)} "
          f"{C['dim']}(deduplicated from every calculator){C['r']}")
    print(f"  categories           {len(cats)}")
    print(f"  fields               {cov['fields_total']}"
          f"  {C['dim']}bounds {cov['fields_with_bounds_from_pdf']}"
          f"  units {cov['fields_with_units_from_html']}{C['r']}")
    sevs = Counter(r["severity"] for r in review)
    print(f"  review queue         {C['crit']}{sevs.get('critical',0)} critical{C['r']}"
          f"  {C['warn']}{sevs.get('high',0)} high{C['r']}"
          f"  {C['dim']}{sevs.get('medium',0)} medium  {sevs.get('low',0)} low{C['r']}")
    # ---- publish the served copy ----------------------------------------
    # The build writes its working output plus every audit report into `dist`.
    # What the API serves is only the specs and their indexes, so those are
    # copied out to one folder that holds nothing else. Copying is the whole
    # release step: there is no second source anyone can edit, because the copy
    # is overwritten wholesale on every build.
    if not args.no_publish:
        pub = args.publish
        shutil.rmtree(pub, ignore_errors=True)
        (pub / "units").mkdir(parents=True, exist_ok=True)
        for f in (dist / "calculators").glob("*.json"):
            shutil.copy2(f, pub / f.name)
        for name in ("index.json", "categories.json"):
            if (dist / name).exists():
                shutil.copy2(dist / name, pub / name)
        for f in (dist / "units").glob("*.json"):
            shutil.copy2(f, pub / "units" / f.name)
        n = len(list(pub.glob("*.json"))) - 2
        print(f"  published            {C['ok']}{n}{C['r']} specs {C['dim']}→ {pub}/{C['r']}")

    print(f"\n{C['dim']}→ {dist}/{C['r']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
