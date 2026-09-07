#!/usr/bin/env python3
"""Batch-extract a folder of Lexicomp PDFs and cross-check against the mockup.

    python3 run_batch.py <pdf_folder> [--html <mockup.html>] [--out <dir>]

Produces:
    out/specs/<slug>.json      one canonical spec per calculator
    out/corpus_report.json     machine-readable extraction + diff report
    stdout                     a human summary with a triage queue

Auto-matches each PDF to a mockup entry by normalised title, so no per-file
configuration is needed. Titles that fail to match are listed as PDF-only
(genuinely new) or mockup-only (present in the mockup but no PDF supplied).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import traceback
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from extractor.diff import compare_metadata, computational_source, _enforces_bound
from extractor.pdf_extract import extract

ROOT = Path(__file__).parent
C = {"crit": "\033[91m", "warn": "\033[93m", "ok": "\033[92m",
     "dim": "\033[90m", "b": "\033[1m", "r": "\033[0m"}


def norm_title(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[®™©]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf_folder", type=Path)
    ap.add_argument("--html", type=Path,
                    default=ROOT.parent / "inpharmd-clinical-calculators-mockup_15.html")
    ap.add_argument("--out", type=Path, default=ROOT / "out")
    args = ap.parse_args()

    out_dir = args.out
    (out_dir / "specs").mkdir(parents=True, exist_ok=True)

    # refresh the mockup harvest
    harvest_path = out_dir / "html_harvest.json"
    print(f"{C['dim']}harvesting mockup…{C['r']}")
    proc = subprocess.run(
        ["node", str(ROOT / "extractor" / "harvest_html.js"),
         str(args.html), str(harvest_path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"{C['crit']}harvest failed:{C['r']} {proc.stderr}")
        return 1
    print(f"{C['dim']}{proc.stderr.strip()}{C['r']}")
    harvest = json.loads(harvest_path.read_text())

    by_title = {
        norm_title(e.get("title", "")): e for e in harvest["per_calculator"].values()
    }
    for bucket in ("scores", "formulas", "converts"):
        for item in (harvest.get("new_calcs") or {}).get(bucket) or []:
            by_title.setdefault(norm_title(item.get("title", "")), {
                "id": item.get("id"), "title": item.get("title"),
                "category": item.get("cat"), "generic": True,
                "constants": {}, "sources": {}, "defaults": {},
            })

    pdfs = sorted(p for p in args.pdf_folder.rglob("*.pdf"))
    if not pdfs:
        print(f"{C['crit']}no PDFs found in {args.pdf_folder}{C['r']}")
        return 1
    print(f"{C['b']}extracting {len(pdfs)} PDFs…{C['r']}\n")

    report, failures, matched = [], [], 0
    kind_counts: Counter[str] = Counter()

    for path in pdfs:
        try:
            spec = extract(path)
        except Exception as exc:                       # noqa: BLE001
            failures.append({"file": path.name, "error": str(exc),
                             "trace": traceback.format_exc()[-1500:]})
            print(f"  {C['crit']}FAIL{C['r']} {path.name}: {exc}")
            continue

        entry = by_title.get(norm_title(spec["title"]))
        if entry:
            matched += 1
            spec["category"] = spec["category"] or entry.get("category")

        # Validation gaps are reported without a per-field mapping: we only need
        # to know whether the mockup enforces the bound anywhere in its code.
        src = computational_source(entry.get("sources") or {}) if entry else ""
        gaps = []
        for inp in spec["inputs"]:
            c = inp.get("constraints") or {}
            miss = [n for n in ("min", "max")
                    if c.get(n) is not None and not _enforces_bound(src, c[n])]
            if miss:
                gaps.append({"field": inp["key"], "bounds": c, "missing": miss})

        n_bounds = sum(
            1 for i in spec["inputs"]
            if (i.get("constraints") or {}).get("min") is not None
            or (i.get("constraints") or {}).get("max") is not None
        )
        kind_counts[spec["renderer"]] += 1

        (out_dir / "specs" / f"{spec['slug']}.json").write_text(
            json.dumps(spec, indent=2, ensure_ascii=False)
        )
        report.append({
            "slug": spec["slug"], "title": spec["title"], "file": path.name,
            "sha256": spec["provenance"]["source_sha256"],
            "renderer": spec["renderer"],
            "inputs": len(spec["inputs"]),
            "outputs": len(spec["compute"]["outputs"]),
            "steps": len(spec["compute"]["steps"]),
            "refs": len(spec["info"]["references"]),
            "fields_with_bounds": n_bounds,
            "exprs_missing": [o["key"] for o in spec["compute"]["outputs"]
                              if not o.get("expr")],
            "mockup_match": entry.get("id") if entry else None,
            "validation_gaps": gaps,
        })

    pdf_titles = {norm_title(r["title"]) for r in report}
    mockup_only = sorted(
        e.get("title") for t, e in by_title.items() if t and t not in pdf_titles
    )

    payload = {
        "summary": {
            "pdfs": len(pdfs), "extracted": len(report), "failed": len(failures),
            "matched_to_mockup": matched,
            "renderers": dict(kind_counts),
            "total_validation_gaps": sum(len(r["validation_gaps"]) for r in report),
            "specs_missing_an_expression": [
                r["slug"] for r in report if r["exprs_missing"]
            ],
            "mockup_only_titles": mockup_only,
        },
        "calculators": report,
        "failures": failures,
    }
    (out_dir / "corpus_report.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False)
    )

    s = payload["summary"]
    print(f"\n{C['b']}══ Corpus summary ══{C['r']}")
    print(f"  extracted        {C['ok']}{s['extracted']}{C['r']}/{s['pdfs']}"
          f"   failed {C['crit'] if s['failed'] else C['dim']}{s['failed']}{C['r']}")
    print(f"  matched to mockup {s['matched_to_mockup']}")
    print(f"  renderers         {dict(kind_counts)}")
    print(f"  validation gaps   {C['warn']}{s['total_validation_gaps']}{C['r']} "
          f"{C['dim']}(bounds in the PDF that the mockup never enforces){C['r']}")
    if s["specs_missing_an_expression"]:
        print(f"  {C['warn']}needs review{C['r']}      "
              f"{len(s['specs_missing_an_expression'])} specs have an output with no "
              f"recovered expression")
    if mockup_only:
        print(f"  {C['dim']}mockup-only       {len(mockup_only)} titles have no PDF "
              f"(send these, or keep the mockup as their source){C['r']}")
    if failures:
        print(f"\n{C['crit']}Failures:{C['r']}")
        for f in failures[:15]:
            print(f"  · {f['file']}: {f['error']}")
    print(f"\n{C['dim']}→ {out_dir}/specs/*.json, {out_dir}/corpus_report.json{C['r']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
