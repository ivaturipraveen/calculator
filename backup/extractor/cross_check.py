#!/usr/bin/env python3
"""Deterministic completeness audit: every spec vs its PDF and HTML harvest.

A reviewer can disagree about a coefficient. They cannot disagree about whether
a field, unit list, tooltip, or printed equation is present in the spec. This
walks all three sources and records every gap, so the remaining LLM review
only has to judge clinical correctness.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from extractor.identity import squash
from extractor.jsparse import normalize_text
from extractor.pdf_extract import read_pdf_text, split_sections, strip_boilerplate

C = {"crit": "\033[91m", "warn": "\033[93m", "ok": "\033[92m",
     "dim": "\033[90m", "b": "\033[1m", "r": "\033[0m"}


def pdf_for(pdfs: Path, spec: dict) -> Path | None:
    name = ((spec.get("provenance") or {}).get("pdf") or {}).get("source_file")
    if name:
        hit = pdfs / name
        if hit.exists():
            return hit
    slug = spec["slug"]
    for p in pdfs.glob("*.pdf"):
        s = re.sub(r"[^a-z0-9]+", "-", p.stem.lower()).strip("-")
        if s == slug:
            return p
    return None


def html_entry(harvest: dict, spec: dict) -> dict | None:
    hid = ((spec.get("provenance") or {}).get("html") or {}).get("id")
    if not hid:
        return None
    per = harvest.get("per_calculator") or {}
    if hid in per:
        return per[hid]
    for e in harvest.get("calculators") or harvest.get("entries") or []:
        if isinstance(e, dict) and e.get("id") == hid:
            return e
    return None


def audit_one(spec: dict, pdfs: Path, harvest: dict) -> dict:
    slug = spec["slug"]
    findings: list[dict] = []
    sources = {"pdf": False, "html": False}

    path = pdf_for(pdfs, spec)
    sections: dict[str, str] = {}
    if path:
        sources["pdf"] = True
        try:
            sections = split_sections(
                normalize_text(strip_boilerplate(read_pdf_text(path)))
            )
        except Exception as exc:  # noqa: BLE001
            findings.append({
                "severity": "high", "category": "source",
                "summary": f"PDF could not be re-read: {exc}",
            })
    else:
        findings.append({
            "severity": "high", "category": "source",
            "summary": "source PDF not found next to the spec",
        })

    html = html_entry(harvest, spec)
    sources["html"] = html is not None

    inputs = spec.get("inputs") or []
    compute = spec.get("compute") or {}
    content = spec.get("content") or {}
    scoring = spec.get("scoring") or {}

    # ---- fields vs printed Calculator block ----
    calc = sections.get("Calculator") or ""
    if calc and spec.get("renderer") in ("formula", "lms", "titration_table"):
        missing_labels = []
        for inp in inputs:
            label = inp.get("label") or ""
            if inp.get("widget") == "quantity" and label and squash(label) not in squash(calc):
                # unit-selector phantoms often have "Unit List" in the label
                if "unit" in squash(label) and "list" in squash(label):
                    findings.append({
                        "severity": "medium", "category": "inputs",
                        "summary": f"phantom unit-selector input {inp['key']!r} "
                                   f"({label}) is not a patient field",
                    })
                elif squash(label) not in squash(calc):
                    missing_labels.append(label)
        if missing_labels:
            findings.append({
                "severity": "medium", "category": "inputs",
                "summary": f"input labels not found in the printed form: "
                           f"{missing_labels[:6]}",
            })

    # ---- equation ----
    eq = content.get("equation") or ""
    pdf_eq = sections.get("Equation") or ""
    if pdf_eq and not eq:
        findings.append({
            "severity": "high", "category": "formula",
            "summary": "PDF has an Equation section that the spec did not keep",
        })
    outs = compute.get("outputs") or []
    if spec.get("renderer") == "formula" and not outs:
        findings.append({
            "severity": "critical", "category": "formula",
            "summary": "formula calculator has no outputs",
        })
    for o in outs:
        if not o.get("expr"):
            findings.append({
                "severity": "critical", "category": "formula",
                "summary": f"output {o.get('key')} has no expression",
            })

    # ---- units ----
    for inp in inputs:
        if inp.get("widget") != "quantity":
            continue
        units = inp.get("units") or []
        if inp.get("base_unit") and not units:
            findings.append({
                "severity": "high", "category": "units",
                "summary": f"{inp['key']} has a base unit but no option list",
            })
        for u in units:
            if not u.get("resolved"):
                findings.append({
                    "severity": "high", "category": "units",
                    "summary": f"{inp['key']} unit {u.get('code')!r} has no factor",
                })

    # ---- dropdowns ----
    for inp in inputs:
        if inp.get("options_incomplete"):
            findings.append({
                "severity": "high" if inp.get("widget") == "select" else "medium",
                "category": "inputs",
                "summary": f"{inp['key']} option list is incomplete "
                           f"(source={inp.get('options_source')})",
            })

    # ---- help / tooltips ----
    if spec.get("renderer") in ("formula", "titration_table"):
        helped = (spec.get("help") or {}).get("fields_with_help") or 0
        js = sections.get("Scripts") or ""
        has_help_fn = bool(re.search(r"function\s+show\w+Help\s*\(", js))
        if has_help_fn and helped == 0:
            findings.append({
                "severity": "medium", "category": "help",
                "summary": "script defines show*Help() but no field help was attached",
            })

    # ---- HTML second opinion: field count ----
    if html:
        hspec = html.get("spec") or {}
        hfields = hspec.get("fields") or hspec.get("inputs") or []
        if isinstance(hfields, list) and hfields and spec.get("renderer") == "formula":
            if len(inputs) + 2 < len(hfields):
                findings.append({
                    "severity": "high", "category": "inputs",
                    "summary": f"HTML mockup has {len(hfields)} fields, "
                               f"spec has {len(inputs)}",
                })

    # ---- score groups ----
    if spec.get("renderer") == "score":
        groups = scoring.get("groups") or []
        if not groups:
            findings.append({
                "severity": "critical", "category": "formula",
                "summary": "score calculator has no criterion groups",
            })
        if not scoring.get("bands"):
            findings.append({
                "severity": "medium", "category": "other",
                "summary": "no interpretation bands",
            })

    # ---- provenance conflicts already on the spec ----
    for c in (spec.get("provenance") or {}).get("conflicts") or []:
        if c.get("severity") in ("critical", "high"):
            findings.append({
                "severity": c.get("severity") or "high",
                "category": c.get("kind") or "other",
                "summary": c.get("detail") or c.get("kind"),
            })

    return {
        "slug": slug,
        "title": spec.get("title"),
        "renderer": spec.get("renderer"),
        "sources": sources,
        "completeness": (spec.get("completeness") or {}).get("score"),
        "findings": findings,
        "worst": min(
            (f["severity"] for f in findings),
            key=lambda s: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(s, 9),
            default=None,
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", type=Path, default=Path(__file__).parent / "dist")
    ap.add_argument("--pdfs", type=Path,
                    default=Path(__file__).parent.parent / "source-pdfs")
    args = ap.parse_args()

    # A missing folder used to report "0 gaps" and read as a pass. Moving the
    # PDFs during a restructure did exactly that, and the audit looked perfect
    # while comparing nothing against nothing.
    if not args.pdfs.is_dir():
        raise SystemExit(f"no source PDFs at {args.pdfs} — pass --pdfs")
    if not (args.dist / "calculators").is_dir():
        raise SystemExit(f"no built specs at {args.dist}/calculators — run build_all.py")


    harvest_path = args.dist / "reports" / "html_harvest.json"
    harvest = json.loads(harvest_path.read_text()) if harvest_path.exists() else {}

    audits = []
    for f in sorted((args.dist / "calculators").glob("*.json")):
        spec = json.loads(f.read_text())
        audits.append(audit_one(spec, args.pdfs, harvest))

    by_sev = Counter(a["worst"] for a in audits if a["worst"])
    n_clean = sum(1 for a in audits if not a["findings"])
    report = {
        "calculators": len(audits),
        "clean": n_clean,
        "by_worst_severity": dict(by_sev),
        "findings_total": sum(len(a["findings"]) for a in audits),
        "audits": audits,
    }
    out = args.dist / "reports" / "completeness_audit.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    print(f"\n{C['b']}══ Completeness audit ══{C['r']}")
    print(f"  calculators   {len(audits)}")
    print(f"  clean         {C['ok']}{n_clean}{C['r']}")
    print(f"  worst         {C['crit']}{by_sev.get('critical',0)} critical{C['r']}  "
          f"{C['warn']}{by_sev.get('high',0)} high{C['r']}  "
          f"{C['dim']}{by_sev.get('medium',0)} medium{C['r']}")
    print(f"  findings      {report['findings_total']}")
    print(f"{C['dim']}→ {out}{C['r']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
