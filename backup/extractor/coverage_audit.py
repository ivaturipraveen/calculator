#!/usr/bin/env python3
"""Per-PDF content coverage: what the source contains vs what the JSON carries.

The completeness score answers "does this spec have the fields we expect?".
That is not the same question as "did we capture everything this PDF says?" --
a spec can score well while quietly dropping a section the document actually
had. This walks the other direction: it reads each PDF, enumerates what is
genuinely present, and checks whether each item survived into the spec.

Every item is one of:
  present   the source has it and the JSON carries it
  MISSING   the source has it and the JSON does not      <- the real gap
  n/a       the source does not have it, so nothing was lost
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from extractor.identity import squash
from extractor.jsparse import _slice_function, normalize_text
from extractor.pdf_extract import (
    read_pdf_text,
    split_calculator_results,
    split_sections,
    strip_boilerplate,
)

C = {"crit": "\033[91m", "warn": "\033[93m", "ok": "\033[92m",
     "dim": "\033[90m", "b": "\033[1m", "r": "\033[0m"}


def norm_words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 3}


def spec_text_blob(spec: dict) -> str:
    """Everything textual the spec carries, folded for containment checks.

    The needle is squashed -- case, spaces and punctuation removed -- so the
    haystack has to be squashed the same way or the check can never match. It
    never did, which is why a printed option that wraps across three lines was
    reported as three missing fields while sitting in the spec in full.
    """
    return re.sub(r"[^a-z0-9]+", "", json.dumps(spec, ensure_ascii=False).lower())


def audit_one(pdf: Path, spec: dict) -> list[dict]:
    text = normalize_text(strip_boilerplate(read_pdf_text(pdf)))
    sec = split_sections(text)
    js = sec.get("Scripts", "") or ""
    blob = spec_text_blob(spec)
    items: list[dict] = []

    def add(name, present_in_pdf, in_json, detail=""):
        items.append({
            "item": name,
            "in_pdf": bool(present_in_pdf),
            "in_json": bool(in_json),
            "status": ("n/a" if not present_in_pdf
                       else "present" if in_json else "MISSING"),
            "detail": detail,
        })

    content = spec.get("content") or {}

    # ---- prose sections -------------------------------------------------
    add("equation_text", bool(sec.get("Equation")), bool(content.get("equation")))
    add("data_input_disclaimer", bool(sec.get("Data Input Disclaimer")),
        bool(content.get("data_input_disclaimer")))
    add("disclaimer", bool(sec.get("Disclaimer")), bool(content.get("disclaimer")))
    add("copyright", bool(sec.get("Copyright")), bool(content.get("copyright")))
    add("calculation_details", bool(sec.get("Calculation Details")),
        bool(content.get("calc_details")))

    notes_src = (sec.get("Notes") or "") + (sec.get("Additional Information") or "")
    # The Additional Information block is sometimes nothing but the formula
    # statement, which is captured as `equation`. Demanding prose as well
    # reported three calculators as having lost a section they had not.
    add("notes", bool(notes_src.strip()),
        bool(content.get("notes") or content.get("instructions")
             or content.get("equation")))

    # ---- references: count them, not just presence -----------------------
    ref_src = sec.get("References", "") or ""
    pmids_pdf = set(re.findall(r"\[PubMed\s+(\d+)\]", ref_src))
    pmids_json = {r.get("pubmed_id") for r in (content.get("references") or [])
                  if r.get("pubmed_id")}
    if ref_src.strip():
        missing = pmids_pdf - pmids_json
        add("references", True, not missing and bool(content.get("references")),
            f"{len(pmids_json)}/{len(pmids_pdf)} PubMed ids"
            + (f", missing {sorted(missing)[:4]}" if missing else ""))
    else:
        add("references", False, False)

    # ---- decimal precision ----------------------------------------------
    dp_src = re.search(r"Decimal Precision\s+(\d+)", text)
    add("decimal_precision", bool(dp_src),
        (spec.get("display") or {}).get("decimal_precision") is not None)

    # ---- inputs: every printed field label must appear -------------------
    calc_in, calc_res = split_calculator_results(sec.get("Calculator", ""))
    printed_labels = [l.strip() for l in calc_in.split("\n")
                      if l.strip() and l.strip().lower() not in
                      {"input", "inputs", "calculate", "reset"}]
    # A printed line can legitimately be any of: an input label, an output
    # label, a dropdown option, a score option, or a unit. Comparing only
    # against input labels made all of those look like losses.
    spec_labels: set[str] = set()
    for i in spec.get("inputs") or []:
        spec_labels |= {squash(i.get("label") or ""), squash(i["key"])}
        for u in i.get("units") or []:
            spec_labels.add(squash(u.get("code") or ""))
        for o in i.get("options") or []:
            spec_labels.add(squash(o.get("label") or ""))
    for o in (spec.get("compute") or {}).get("outputs") or []:
        spec_labels |= {squash(o.get("label") or ""), squash(o["key"]),
                        squash(o.get("base_unit") or "")}
    for g in ((spec.get("scoring") or {}).get("groups") or []):
        spec_labels.add(squash(g.get("label") or ""))
        for o in g.get("options") or []:
            spec_labels.add(squash(o.get("label") or ""))
    # Interpretation bands are printed as ordinary lines in the form ("0 points:
    # 0% per year"), so without their text every captured band still counted as
    # a lost field.
    for h in (content.get("form_sections") or []):
        spec_labels.add(squash(h))
    for rt in (content.get("reference_tables") or []):
        spec_labels.add(squash(rt.get("title")))
        for r in rt.get("rows") or []:
            spec_labels.add(squash(r.get("label")))
            spec_labels.add(squash(f"{r.get('label')} {r['values'][0]:g}")
                            if r.get("values") else "")
    # A quantity the form STATES rather than asks for is captured too.
    for f in (content.get("fixed_values") or []):
        spec_labels.add(squash(f.get("label")))
        spec_labels.add(squash(f"{f.get('label')} {f.get('value'):g} {f.get('unit')}"))
    _interp = ((spec.get("scoring") or {}).get("interpretation") or {})
    spec_labels.add(squash(_interp.get("label") or ""))
    for r in _interp.get("printed_rows") or []:
        spec_labels.add(squash(r))
    for b in ((spec.get("scoring") or {}).get("bands") or []):
        spec_labels.add(squash(b.get("label") or ""))
        spec_labels.add(squash(b.get("raw") or ""))
    spec_labels.discard("")

    # The printed form decorates labels: a "?" help marker, a "(0.85)" coefficient,
    # a "(2 points)" score. Strip those before comparing.
    def bare(line: str) -> str:
        t = re.sub(r"\(\s*[+-]?\d+(?:\.\d+)?\s*(?:points?)?\s*\)\s*$", "", line)
        # The point marker can wrap: "... within 30 to 100 days (1" with the
        # word "point)" on the line below. The stub is not part of the label.
        t = re.sub(r"\(\s*[+-]?\d+(?:\.\d+)?\s*$", "", t)
        t = re.sub(r"\(\s*optional\s*\)", "", t, flags=re.I)
        return squash(t.rstrip("? ").strip())

    # Identifier fields are not part of the calculation and carry no data.
    IDENT = {"nameoptional", "patientidoptional", "birthdateoptional",
             "name", "patientid", "birthdate", "createdby", "date"}
    # Form chrome carries no calculator data: buttons, collapsed <select>
    # placeholders, and the running-total / precision widgets the print view
    # renders inline. Counting these as lost content overstates the gap.
    CHROME = {"resetform", "pulldown", "createchart", "selectdrug", "submit",
              "clear", "print", "close", "back", "next", "showchart",
              "restart"}
    CHROME_PREFIX = ("total criteria point count", "set maximal display precision",
                     "decimal precision", "important:", "resultsimportant",
                     "results important")
    # The date pickers print their three parts as separate lines; a spec that
    # models the date as one field has not lost them.
    DATE_PARTS = {"mm", "dd", "yyyy", "yy", "mmddyyyy"}

    unmatched = []
    previous = ""
    for l in printed_labels:
        # The page drawn twice repeats a line verbatim. The spec keeps one copy
        # on purpose -- two identical score options are not two choices -- so
        # the second printing is not content that went missing.
        if squash(l) and squash(l) == previous:
            continue
        previous = squash(l)
        b = bare(l)
        low = l.strip().lower()
        if not b or b in IDENT or b in CHROME or b in spec_labels:
            continue
        if any(low.startswith(pfx) for pfx in CHROME_PREFIX):
            continue
        if re.fullmatch(r"-?\d+(\.\d+)?", l.strip()):
            continue
        if len(l.strip()) <= 2:
            continue
        # The Calculator block also carries the calculator's own instructions.
        # A sentence is prose, not a field: counting it as lost content put
        # paragraph fragments in the gap list and buried the real misses.
        s_ = l.strip()
        if s_.count(" ") >= 9 and not s_.endswith(":") and "(" not in s_:
            continue
        if b in DATE_PARTS:
            continue
        # A fragment that opens in lower case or mid-parenthesis is the tail of
        # the sentence on the line before it, not a field of its own.
        if s_[0].islower() or s_[0] in "(“‘":
            continue
        # A heading ends in a colon and labels the block beneath it.
        if s_.endswith(":") and not re.search(r"\d", s_):
            continue
        # a partial match counts: "Loading Dose (LD)" vs output "loading_dose"
        if any(b in sl or sl in b for sl in spec_labels if len(sl) >= 4):
            continue
        if b in blob:
            continue
        # A band line may be split across two printed lines ("1 point:" then
        # "1.3% per year"); either half matching a captured band is enough.
        if re.match(r"^\s*[<>]?=?\s*[\d.]+(\s*(?:to|-|–)\s*[\d.]+)?\s*points?\s*:?\s*$",
                    l.strip(), re.I):
            continue
        unmatched.append(l.strip())
    add("printed_fields", bool(printed_labels), not unmatched,
        f"{len(unmatched)} printed line(s) absent from the spec"
        + (f": {unmatched[:4]}" if unmatched else ""))

    # ---- bounds: every minMaxCheck bound must be enforced -----------------
    mmc = _slice_function(js, "minMaxCheck") or ""
    bound_vals = set(re.findall(r"is\s+(-?\d+(?:\.\d+)?)\s", mmc))
    # `constraints` also carries `exclusive_min`/`exclusive_max` flags; only the
    # numbers are bounds.
    spec_bounds = {str(v) for i in (spec.get("inputs") or [])
                   for k, v in (i.get("constraints") or {}).items()
                   if k in ("min", "max") and isinstance(v, (int, float))}
    spec_bounds |= {str(v) for i in (spec.get("inputs") or [])
                    for mode in (i.get("constraints_by_mode") or [])
                    for k, v in mode.items()
                    if k in ("min", "max") and v is not None}
    spec_bounds |= {str(int(float(v))) for v in list(spec_bounds)
                    if float(v) == int(float(v))}
    miss_b = {b for b in bound_vals
              if b not in spec_bounds and str(float(b)) not in spec_bounds}
    add("validation_bounds", bool(bound_vals), not miss_b,
        f"{len(bound_vals) - len(miss_b)}/{len(bound_vals)} bounds"
        + (f", missing {sorted(miss_b)[:4]}" if miss_b else ""))

    # ---- help / tooltip text ---------------------------------------------
    # Only alerts about THIS calculator count. The shared include shows the
    # same handful on every page -- "Negative values are not allowed" -- and
    # counting them made a converter with no fields look like it had lost its
    # tooltips.
    alerts = [
        a for a in re.findall(r"alert\s*\(\s*['\"](.{12,}?)['\"]", js, re.S)
        if not any(b in re.sub(r"\s+", " ", a).lower() for b in (
            "data panel values cannot be transferred",
            "negative values are not allowed",
            "birthdate must be no later",
            "only digits 0 to 9",
            "improperly formatted",
        ))
        # The PDF loses the closing quote on some strings, so the alert arrives
        # cut off: "Desired Peak (Cdp) for ". There is no message there to
        # attach, and counting it makes a complete extraction look incomplete.
        and not (len(a.strip()) < 40 and not a.strip().endswith((".", "!", "?")))
    ]
    help_n = sum(1 for i in (spec.get("inputs") or []) if i.get("help"))
    help_n += 1 if (spec.get("help") or {}).get("general") else 0
    add("help_messages", bool(alerts), bool(help_n),
        f"{help_n} field(s) carry help; PDF has {len(alerts)} alert string(s)")

    # ---- outputs ---------------------------------------------------------
    n_out = len((spec.get("compute") or {}).get("outputs") or [])
    has_result_rows = bool(calc_res.strip() or sec.get("Results"))
    if spec.get("renderer") in ("score", "convert", "tree", "dose_table"):
        add("outputs", has_result_rows, True, "non-formula renderer")
    else:
        add("outputs", has_result_rows or bool(sec.get("Equation")), n_out > 0,
            f"{n_out} output(s)")

    # ---- formula actually executable -------------------------------------
    exprs = [o.get("expr") for o in ((spec.get("compute") or {}).get("outputs") or [])]
    if spec.get("renderer") in ("formula", "lms", "titration_table"):
        add("formula_expressions", True, bool(exprs) and all(exprs),
            f"{sum(1 for e in exprs if e)}/{len(exprs)} outputs have an expression")
    else:
        add("formula_expressions", False, False, "non-formula renderer")

    # ---- score criteria ---------------------------------------------------
    if spec.get("renderer") == "score":
        groups = (spec.get("scoring") or {}).get("groups") or []
        # Count the options the form OFFERS, not the times they were printed:
        # the page drawn twice repeats the last one, and the spec keeps a
        # single copy because two identical choices are not two choices.
        _seen: set[str] = set()
        _once: list[str] = []
        for _l in (sec.get("Calculator", "") or "").split("\n"):
            _s = squash(_l)
            if not _s or _s in _seen:
                continue
            _seen.add(_s)
            _once.append(_l)
        pdf_opts = len(re.findall(r"\(\s*[+-]?\d+(?:\.\d+)?\s*points?\s*\)",
                                  "\n".join(_once), re.I))
        spec_opts = sum(len(g.get("options") or []) for g in groups)
        add("score_options", bool(pdf_opts), spec_opts >= pdf_opts,
            f"{spec_opts}/{pdf_opts} options")
        # Not every score has an interpretation: SOFA prints its criteria and
        # a total and stops there. Asserting bands exist made a faithful
        # extraction look incomplete.
        _band_in_pdf = bool(re.search(
            r"\bpoints?\s*:|\binterpretation\b|score\s*[<>]=?\s*\d",
            "\n".join(filter(None, (sec.get("Calculator"), sec.get("Results"),
                                    sec.get("Additional Information")))),
            re.I))
        add("score_bands", _band_in_pdf,
            bool((spec.get("scoring") or {}).get("bands")))
    return items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", type=Path, default=Path(__file__).parent / "dist")
    ap.add_argument("--pdfs", type=Path,
                    default=Path(__file__).parent.parent / "source-pdfs")
    ap.add_argument("--show", type=int, default=14)
    args = ap.parse_args()

    # A missing folder used to report "0 gaps" and read as a pass. Moving the
    # PDFs during a restructure did exactly that, and the audit looked perfect
    # while comparing nothing against nothing.
    if not args.pdfs.is_dir():
        raise SystemExit(f"no source PDFs at {args.pdfs} — pass --pdfs")
    if not (args.dist / "calculators").is_dir():
        raise SystemExit(f"no built specs at {args.dist}/calculators — run build_all.py")


    rows = []
    per_item = defaultdict(Counter)
    for f in sorted((args.dist / "calculators").glob("*.json")):
        spec = json.loads(f.read_text())
        src = (spec.get("provenance") or {}).get("pdf", {}).get("source_file")
        pdf = args.pdfs / src if src else None
        if not pdf or not pdf.exists():
            continue
        try:
            items = audit_one(pdf, spec)
        except Exception as exc:                                    # noqa: BLE001
            items = [{"item": "audit_error", "in_pdf": True, "in_json": False,
                      "status": "MISSING", "detail": f"{type(exc).__name__}: {exc}"}]
        for it in items:
            per_item[it["item"]][it["status"]] += 1
        missing = [i for i in items if i["status"] == "MISSING"]
        rows.append({"slug": spec["slug"], "title": spec["title"],
                     "renderer": spec["renderer"],
                     "missing": missing, "items": items})

    total_missing = sum(len(r["missing"]) for r in rows)
    complete = [r for r in rows if not r["missing"]]

    print(f"\n{C['b']}══ PDF → JSON content coverage ══{C['r']}")
    print(f"  calculators            {len(rows)}")
    print(f"  fully captured         {C['ok']}{len(complete)}{C['r']}")
    print(f"  with at least one gap  {C['warn']}{len(rows)-len(complete)}{C['r']}")
    print(f"  total gaps             {total_missing}\n")

    print(f"  {'item':<24} {'present':>8} {'MISSING':>8} {'n/a':>6}   capture rate")
    for item, c in sorted(per_item.items(),
                          key=lambda kv: -kv[1]["MISSING"]):
        p, m, n = c["present"], c["MISSING"], c["n/a"]
        rate = f"{100*p/(p+m):.0f}%" if (p + m) else "-"
        col = C["crit"] if m > 20 else C["warn"] if m else C["ok"]
        print(f"  {item:<24} {p:>8} {col}{m:>8}{C['r']} {n:>6}   {rate}")

    worst = sorted(rows, key=lambda r: -len(r["missing"]))[: args.show]
    if worst and worst[0]["missing"]:
        print(f"\n{C['b']}  Most incomplete:{C['r']}")
        for r in worst:
            if not r["missing"]:
                break
            names = ", ".join(m["item"] for m in r["missing"])
            print(f"    {r['slug'][:40]:<40} {len(r['missing'])}  {names[:52]}")

    out = args.dist / "reports" / "coverage_audit.json"
    out.write_text(json.dumps(
        {"calculators": len(rows), "fully_captured": len(complete),
         "total_gaps": total_missing,
         "by_item": {k: dict(v) for k, v in per_item.items()},
         "rows": rows}, indent=2, ensure_ascii=False))
    print(f"\n{C['dim']}→ {out}{C['r']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
