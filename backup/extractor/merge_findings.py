#!/usr/bin/env python3
"""Consolidate the verification agents' findings and attach them to each spec.

Twelve reviewers each audited a slice of the corpus against the PDF and the
mockup. Their findings are merged here into one ranked report and stamped onto
the specs they concern, so a spec always travels with the objections raised
against it rather than leaving them in a separate file nobody reads.

A finding is a claim, not a verdict: it records what a reviewer saw, and the
severity they assigned. Nothing is auto-applied to a formula -- a clinical
change needs a human, and this only makes the queue visible and ordered.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
C = {"crit": "\033[91m", "warn": "\033[93m", "ok": "\033[92m",
     "dim": "\033[90m", "b": "\033[1m", "r": "\033[0m"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", type=Path, default=Path(__file__).parent / "dist")
    ap.add_argument("--apply", action="store_true",
                    help="stamp findings onto each calculator spec")
    args = ap.parse_args()

    vdir = args.dist / "verify"
    files = sorted(vdir.glob("findings_*.json"))
    if not files:
        print(f"{C['warn']}no findings files in {vdir}{C['r']}")
        return 1

    all_findings: list[dict] = []
    reviewed = 0
    batches_seen = []
    for f in files:
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError as exc:
            print(f"{C['crit']}unparseable {f.name}: {exc}{C['r']}")
            continue
        batches_seen.append(data.get("batch", f.stem))
        rev = data.get("reviewed") or 0
        reviewed += len(rev) if isinstance(rev, list) else int(rev)
        for item in data.get("findings") or []:
            item.setdefault("severity", "medium")
            item.setdefault("category", "other")
            item["batch"] = data.get("batch")
            all_findings.append(item)

    all_findings.sort(key=lambda x: (SEV_ORDER.get(x["severity"], 9),
                                     x.get("slug", "")))

    by_slug: dict[str, list[dict]] = defaultdict(list)
    for f in all_findings:
        if f.get("slug"):
            by_slug[f["slug"]].append(f)

    report = {
        "generated": date.today().isoformat(),
        "batches": sorted(batches_seen),
        "calculators_reviewed": reviewed,
        "findings_total": len(all_findings),
        "by_severity": dict(Counter(f["severity"] for f in all_findings)),
        "by_category": dict(Counter(f["category"] for f in all_findings)),
        "calculators_with_findings": len(by_slug),
        "findings": all_findings,
    }
    out = args.dist / "reports" / "verification_report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    if args.apply:
        stamped = 0
        for slug, items in by_slug.items():
            path = args.dist / "calculators" / f"{slug}.json"
            if not path.exists():
                continue
            spec = json.loads(path.read_text())
            spec.setdefault("provenance", {})["verification"] = {
                "reviewed": True,
                "findings": [
                    {k: v for k, v in i.items() if k != "slug"} for i in items
                ],
                "worst_severity": min(
                    (i["severity"] for i in items),
                    key=lambda s: SEV_ORDER.get(s, 9),
                ),
            }
            path.write_text(json.dumps(spec, indent=2, ensure_ascii=False))
            stamped += 1
        # mark the clean ones too, so "no findings" is explicit rather than absent
        for path in (args.dist / "calculators").glob("*.json"):
            spec = json.loads(path.read_text())
            if "verification" in (spec.get("provenance") or {}):
                continue
            spec.setdefault("provenance", {})["verification"] = {
                "reviewed": True, "findings": [], "worst_severity": None,
            }
            path.write_text(json.dumps(spec, indent=2, ensure_ascii=False))
        print(f"  stamped findings onto {stamped} specs "
              f"(others marked reviewed-clean)")

    sev = report["by_severity"]
    print(f"\n{C['b']}══ Verification report ══{C['r']}")
    print(f"  batches            {len(batches_seen)}/{12}")
    print(f"  calculators        {reviewed} reviewed")
    print(f"  findings           {C['crit']}{sev.get('critical',0)} critical{C['r']}  "
          f"{C['warn']}{sev.get('high',0)} high{C['r']}  "
          f"{C['dim']}{sev.get('medium',0)} medium  {sev.get('low',0)} low{C['r']}")
    print(f"  affected           {len(by_slug)} calculators")
    print(f"  by category        {report['by_category']}")

    crit = [f for f in all_findings if f["severity"] == "critical"]
    if crit:
        print(f"\n{C['b']}  CRITICAL — would produce a wrong clinical number:{C['r']}")
        for f in crit[:15]:
            print(f"    {C['crit']}·{C['r']} {f.get('slug','?')[:38]:<38} "
                  f"[{f.get('category')}] {f.get('summary','')[:60]}")
        if len(crit) > 15:
            print(f"    {C['dim']}… {len(crit)-15} more{C['r']}")
    print(f"\n{C['dim']}→ {out}{C['r']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
