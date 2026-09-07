#!/usr/bin/env python3
"""Build one self-contained verification bundle per calculator.

A reviewer -- human or model -- can only judge an extraction against its
sources. This writes, for each calculator, a single compact file holding the
three views side by side:

  1. SOURCE OF TRUTH   the PDF's own equation, form table, bounds and script
  2. EXTRACTED SPEC    what we produced from it
  3. SECOND OPINION    the mockup's independent hand-built implementation

Bundles are trimmed so the whole comparison fits in one reading: reference
tables are summarised to their shape and a few sample rows rather than dumped,
since a 215-row LMS ladder cannot be eyeballed anyway and would crowd out the
parts that can.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from extractor.jsparse import _slice_function, normalize_text
from extractor.pdf_extract import (
    read_pdf_text,
    split_sections,
    strip_boilerplate,
)

MAX_SCRIPT = 6000
MAX_SECTION = 3000


def trim(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [{len(text) - limit} more chars omitted]"


def summarise_tables(spec: dict) -> str:
    """Describe bulk reference data by shape rather than dumping every row."""
    tables = spec.get("tables") or {}
    if not tables:
        return "(none)"
    out = []
    for name, val in tables.items():
        if name == "lookups" and isinstance(val, list):
            for t in val:
                rows = t.get("rows") or []
                out.append(
                    f"  lookup ladder: key={t.get('key')} {t.get('operator')} "
                    f"cols={t.get('columns')} rows={t.get('row_count')}\n"
                    f"    first: {rows[0] if rows else '-'}\n"
                    f"    last:  {rows[-1] if rows else '-'}"
                )
        elif isinstance(val, list):
            out.append(f"  {name}: {len(val)} entries; first = "
                       f"{json.dumps(val[0], ensure_ascii=False)[:300]}")
        elif isinstance(val, dict):
            keys = list(val.keys())[:8]
            out.append(f"  {name}: dict with {len(val)} keys {keys}")
    return "\n".join(out) or "(none)"


def spec_view(spec: dict) -> str:
    s = {
        "slug": spec["slug"],
        "title": spec["title"],
        "category": spec.get("category"),
        "renderer": spec.get("renderer"),
        "engine": spec.get("engine"),
        "inputs": [
            {
                "key": i["key"],
                "label": i.get("label"),
                "widget": i.get("widget"),
                "default": i.get("default"),
                "base_unit": i.get("base_unit"),
                "constraints": i.get("constraints"),
                "units": [
                    {"code": u["code"], "factor": u.get("factor"),
                     "offset": u.get("offset")}
                    for u in (i.get("units") or [])
                ],
                "options": i.get("options"),
                "help": i.get("help"),
            }
            for i in spec.get("inputs") or []
        ],
        "compute": spec.get("compute"),
        "scoring": spec.get("scoring"),
        "display": spec.get("display"),
        "content": {
            "equation": (spec.get("content") or {}).get("equation"),
            "notes": (spec.get("content") or {}).get("notes"),
            "reference_count": len((spec.get("content") or {}).get("references") or []),
        },
    }
    return json.dumps(s, indent=2, ensure_ascii=False)


def html_view(harvest: dict, html_id: str | None) -> str:
    if not html_id:
        return "(no matching mockup entry)"
    entry = (harvest.get("per_calculator") or {}).get(html_id)
    if not entry:
        for bucket in ("scores", "formulas", "converts"):
            for item in (harvest.get("new_calcs") or {}).get(bucket) or []:
                if item.get("id") == html_id:
                    return ("GENERIC MOCKUP ENTRY:\n"
                            + json.dumps(item, indent=2, ensure_ascii=False)[:MAX_SECTION])
        return f"(mockup id {html_id!r} not found in harvest)"

    parts = [f"mockup id: {entry.get('id')}   category: {entry.get('category')}"]
    if entry.get("defaults"):
        parts.append("DEFAULTS: " + json.dumps(entry["defaults"], ensure_ascii=False)[:800])
    consts = entry.get("constants") or {}
    if consts:
        brief = {k: (f"<{len(v)} entries>" if isinstance(v, (list, dict)) and len(v) > 12
                     else v)
                 for k, v in consts.items()}
        parts.append("CONSTANTS: " + json.dumps(brief, ensure_ascii=False)[:1500])
    srcs = entry.get("sources") or {}
    calc = {k: v for k, v in srcs.items() if k.lower().startswith("calc")}
    for name, body in list(calc.items())[:2]:
        parts.append(f"--- mockup {name}() ---\n{trim(body, 2500)}")
    return "\n".join(parts)


def build_bundle(spec: dict, pdf_dir: Path, harvest: dict) -> str:
    prov = spec.get("provenance") or {}
    pdf_meta = prov.get("pdf") or {}
    fname = pdf_meta.get("source_file")
    pdf_path = pdf_dir / fname if fname else None

    sections: dict[str, str] = {}
    if pdf_path and pdf_path.exists():
        sections = split_sections(
            normalize_text(strip_boilerplate(read_pdf_text(pdf_path)))
        )

    js = sections.get("Scripts", "")
    fx = _slice_function(js, r"\w+_fx") or _slice_function(js, "calculate") or ""
    mmc = _slice_function(js, "minMaxCheck") or ""

    parts = [
        "=" * 78,
        f"CALCULATOR: {spec['title']}",
        f"slug: {spec['slug']}   renderer: {spec.get('renderer')}   "
        f"engine: {spec.get('engine')}",
        f"source pdf: {fname}   sha256: {pdf_meta.get('source_sha256', '')[:16]}",
        "=" * 78,
        "",
        "########## 1. PDF — SOURCE OF TRUTH ##########",
        "",
        "--- Equation section ---",
        trim(sections.get("Equation", "") or "(no Equation section)", MAX_SECTION),
        "",
        "--- Calculator / Input table (printed) ---",
        trim(sections.get("Calculator", "") or "(none)", MAX_SECTION),
        "",
        "--- Results (printed) ---",
        trim(sections.get("Results", "") or "(none)", 1200),
        "",
        "--- Additional Information / Notes ---",
        trim((sections.get("Additional Information", "") or "")
             + "\n" + (sections.get("Notes", "") or ""), MAX_SECTION),
        "",
        "--- calculator function (verbatim JS) ---",
        trim(fx, MAX_SCRIPT),
        "",
        "--- minMaxCheck (verbatim JS) ---",
        trim(mmc, 2500),
        "",
        "########## 2. EXTRACTED SPEC (what we produced) ##########",
        "",
        spec_view(spec),
        "",
        "--- bulk tables in the spec (summarised) ---",
        summarise_tables(spec),
        "",
        "########## 3. MOCKUP HTML — INDEPENDENT SECOND OPINION ##########",
        "",
        html_view(harvest, (prov.get("html") or {}).get("id")),
        "",
    ]
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", type=Path, default=Path(__file__).parent / "dist")
    ap.add_argument("--pdfs", type=Path,
                    default=Path(__file__).parent.parent / "source-pdfs")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    out_dir = args.out or (args.dist / "bundles")
    out_dir.mkdir(parents=True, exist_ok=True)
    harvest = json.loads((args.dist / "reports" / "html_harvest.json").read_text())

    files = sorted((args.dist / "calculators").glob("*.json"))
    sizes = []
    for f in files:
        spec = json.loads(f.read_text())
        text = build_bundle(spec, args.pdfs, harvest)
        (out_dir / f"{spec['slug']}.txt").write_text(text)
        sizes.append(len(text))

    print(f"wrote {len(files)} bundles to {out_dir}")
    print(f"  size: mean {sum(sizes)//len(sizes):,} chars, max {max(sizes):,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
