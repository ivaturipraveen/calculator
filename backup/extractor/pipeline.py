#!/usr/bin/env python3
"""End-to-end corpus pipeline: PDFs -> verified, cross-checked calculator specs.

    python3 pipeline.py <pdf_folder> [--html mockup.html] [--out out/]
                        [--no-differential] [--only SLUG]

Stages, each auditable in the report it writes:

  1 INGEST     read every PDF
  2 IDENTIFY   recover doc_id / title / form name; cross-verify them, and
               verify the filename too when it carries information
  3 MATCH      pair with the mockup entry (tiered, collisions detected)
  4 EXTRACT    parse the embedded JS into a canonical spec
  5 VERIFY     metadata diff + numerical differential against the mockup
  6 EMIT       specs/, review_queue.json, corpus_report.json

Nothing is auto-resolved when signals conflict. Anything uncertain lands in the
review queue with the evidence attached.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from extractor import fieldmap
from extractor.diff import (
    _enforces_bound,
    compare_metadata,
    computational_source,
    run_differential,
)
from extractor.matcher import MatchIndex, detect_collisions, unmatched_html
from extractor.pdf_extract import extract

ROOT = Path(__file__).parent
C = {"crit": "\033[91m", "warn": "\033[93m", "ok": "\033[92m", "cy": "\033[96m",
     "dim": "\033[90m", "b": "\033[1m", "r": "\033[0m"}


def stage(n: int, name: str) -> None:
    print(f"\n{C['b']}{C['cy']}[{n}] {name}{C['r']}")


# The suffix an operating system adds to a second download: "(2)", "(3)".
# Bounded to one digit on purpose -- `\(\d+\)` also matched a YEAR, so
# "MELDNa Score (2016)" was folded into "MELDNa Score" and the 2016 UNOS
# revision, which is a different formula, was thrown away as a duplicate.
COPY_SUFFIX = re.compile(r"\s*\((\d)\)\s*$")


def dedupe_copies(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    """Separate `Foo (2).pdf` re-exports from their canonical `Foo.pdf`."""
    canonical: dict[str, Path] = {}
    copies: list[Path] = []
    for p in sorted(paths, key=lambda x: (len(x.stem), x.name)):
        base = COPY_SUFFIX.sub("", p.stem).strip().lower()
        if base in canonical:
            copies.append(p)
        else:
            canonical[base] = p
    return sorted(canonical.values()), sorted(copies)


def looks_like_calculator(spec: dict, sections_seen: bool,
                          sections: dict | None = None) -> bool:
    """Reject PDFs that are not calculator exports at all.

    The corpus contains an unrelated GitHub issue page saved to PDF; it has no
    Calculator/Scripts/Equation structure and must not become a spec.
    """
    has_io = bool(spec.get("inputs")) or bool(spec["compute"].get("outputs"))
    info = spec.get("info") or {}
    has_content = bool(info.get("equation")) or bool(info.get("data_input_disclaimer"))
    # A few exports carry no printed Calculator block at all -- the two Rapid
    # Sequence Intubation crash carts are pure script -- but a script with a
    # calculator entry point in it is a calculator, whatever the print view
    # managed to render.
    js = (sections or {}).get("Scripts") or ""
    has_entry = bool(re.search(r"function\s+(?:\w+_fx|calculate|createCrashCart)\s*\(", js))
    return has_io or has_content or has_entry or sections_seen


def load_overrides(path: Path, slug: str) -> dict:
    f = path / f"{slug}.json"
    return json.loads(f.read_text()) if f.exists() else {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf_folder", type=Path)
    ap.add_argument("--html", type=Path,
                    default=ROOT.parent / "inpharmd-clinical-calculators-mockup_15.html")
    ap.add_argument("--out", type=Path, default=ROOT / "out")
    ap.add_argument("--mappings", type=Path, default=ROOT / "mappings")
    ap.add_argument("--no-differential", action="store_true")
    ap.add_argument("--only", type=str, default=None,
                    help="process just the PDF whose filename contains this")
    ap.add_argument("--vectors", type=int, default=200)
    args = ap.parse_args()

    out = args.out
    (out / "specs").mkdir(parents=True, exist_ok=True)
    args.mappings.mkdir(parents=True, exist_ok=True)

    # ---- 1 INGEST -----------------------------------------------------
    stage(1, "INGEST")
    pdfs = sorted(p for p in args.pdf_folder.rglob("*.pdf"))
    if args.only:
        pdfs = [p for p in pdfs if args.only.lower() in p.name.lower()]
    if not pdfs:
        print(f"  {C['crit']}no PDFs found in {args.pdf_folder}{C['r']}")
        return 1
    print(f"  {len(pdfs)} PDF(s) found")

    # Drop "Foo (2).pdf" re-exports, keeping the canonical name. Some are
    # byte-identical, others differ only by export timestamp; either way they
    # are the same calculator and would otherwise collide at match time.
    pdfs, dupes = dedupe_copies(pdfs)
    if dupes:
        print(f"  {C['dim']}skipped {len(dupes)} duplicate re-export(s): "
              f"{', '.join(d.name for d in dupes[:4])}"
              f"{'…' if len(dupes) > 4 else ''}{C['r']}")
    print(f"  {len(pdfs)} unique PDF(s) to process")

    import subprocess
    harvest_path = out / "html_harvest.json"
    proc = subprocess.run(
        ["node", str(ROOT / "extractor" / "harvest_html.js"),
         str(args.html), str(harvest_path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"  {C['crit']}mockup harvest failed:{C['r']} {proc.stderr[:500]}")
        return 1
    print(f"  mockup: {proc.stderr.strip()}")
    harvest = json.loads(harvest_path.read_text())
    index = MatchIndex.from_harvest(harvest)
    print(f"  index : {len(index.entries)} mockup calculators")

    # ---- 2-5 per calculator -------------------------------------------
    records, failures, matches, review = [], [], [], []
    counts: Counter[str] = Counter()

    stage(2, "IDENTIFY + MATCH + EXTRACT + VERIFY")
    for path in pdfs:
        try:
            spec = extract(path)
        except Exception as exc:                                  # noqa: BLE001
            failures.append({"file": path.name, "error": str(exc),
                             "trace": traceback.format_exc()[-1200:]})
            print(f"  {C['crit']}✗ EXTRACT{C['r']} {path.name}: {exc}")
            counts["extract_failed"] += 1
            continue

        if not looks_like_calculator(spec, bool(spec["info"].get("copyright"))):
            counts["not_a_calculator"] += 1
            print(f"  {C['dim']}– SKIP{C['r']} {path.name[:56]} "
                  f"{C['dim']}(no calculator structure){C['r']}")
            review.append({
                "stage": "ingest", "file": path.name, "severity": "low",
                "title": spec.get("title"),
                "reason": "not a calculator export; excluded from specs",
            })
            continue

        ident = spec["provenance"]["identity"]
        counts["extracted"] += 1

        # -- identity gate
        if not ident["verified"]:
            counts["identity_unverified"] += 1
            review.append({
                "stage": "identity", "file": path.name, "slug": spec["slug"],
                "title": spec["title"], "severity": "high",
                "reason": "identity not verified",
                "flags": ident["flags"], "checks": ident["checks"],
                "confidence": ident["confidence"],
            })

        # -- match
        m = index.lookup(spec["title"])
        m.pdf_slug, m.pdf_title = spec["slug"], spec["title"]
        matches.append(m)
        entry = None
        if m.html_id:
            entry = (harvest.get("per_calculator") or {}).get(m.html_id)
            for e in index.entries:
                if entry is None and e.get("id") == m.html_id:
                    entry = e
        if m.needs_review:
            counts["match_fuzzy"] += 1
            review.append({
                "stage": "match", "file": path.name, "slug": spec["slug"],
                "title": spec["title"], "severity": "high",
                "reason": m.note, "matched_to": m.html_title, "score": m.score,
            })
        elif m.method == "none":
            counts["match_none"] += 1
        else:
            counts["match_ok"] += 1

        if entry and not spec.get("category"):
            spec["category"] = entry.get("category")

        # -- validation gaps (bounds present in PDF, unenforced in mockup)
        src = computational_source(entry.get("sources") or {}) if entry else ""
        gaps = []
        for inp in spec["inputs"]:
            c = inp.get("constraints") or {}
            miss = [n for n in ("min", "max")
                    if c.get(n) is not None and not _enforces_bound(src, c[n])]
            if miss:
                gaps.append({"field": inp["key"], "bounds": c, "missing": miss})
        counts["validation_gaps"] += len(gaps)

        # -- outputs with no recovered expression
        no_expr = [o["key"] for o in spec["compute"]["outputs"] if not o.get("expr")]
        if no_expr:
            counts["missing_expr"] += 1
            review.append({
                "stage": "extract", "file": path.name, "slug": spec["slug"],
                "title": spec["title"], "severity": "critical",
                "reason": f"no expression recovered for outputs: {no_expr}",
            })

        # -- differential
        diff_result = None
        if entry and not args.no_differential and not entry.get("generic") and no_expr == []:
            ov = load_overrides(args.mappings, spec["slug"])
            handles = fieldmap.js_handles(m.html_id)
            handles.update({k: v for k, v in ov.items() if k in handles})
            missing_h = fieldmap.verify_handles(handles, entry.get("sources") or {})

            fmap = ov.get("field_map")
            unresolved: list = []
            if not fmap:
                fmap, unresolved = fieldmap.derive(spec, entry.get("defaults") or {})

            # The result object only exists post-calculation, so probe the
            # mockup once with a valid vector to learn its shape, then match.
            omap = {k: v for k, v in (ov.get("output_map") or {}).items() if v}
            out_unresolved: list = []
            if not omap and not unresolved and not missing_h:
                rkeys = fieldmap.probe_result_keys(
                    args.html, handles["state_var"], handles["calc_fn"], fmap, spec
                )
                if rkeys:
                    omap, out_unresolved = fieldmap.derive_outputs(spec, rkeys)

            if missing_h:
                diff_result = {"ok": False, "skipped": f"missing handles: {missing_h}"}
            elif unresolved or out_unresolved or not omap:
                diff_result = {"ok": False, "skipped": "incomplete mapping"}
                stub = args.mappings / f"{spec['slug']}.json"
                if not stub.exists():
                    stub.write_text(json.dumps({
                        "_doc": "Complete field_map/output_map, then re-run to enable "
                                "the numerical differential test.",
                        "html_id": m.html_id,
                        **handles,
                        "field_map": fmap,
                        "unresolved_fields": unresolved,
                        "output_map": omap or {o["key"]: "" for o in spec["compute"]["outputs"]},
                        "unresolved_outputs": out_unresolved,
                    }, indent=2))
                counts["diff_needs_mapping"] += 1
                review.append({
                    "stage": "differential", "file": path.name, "slug": spec["slug"],
                    "title": spec["title"], "severity": "medium",
                    "reason": "field/output mapping incomplete",
                    "unresolved": unresolved + out_unresolved,
                    "edit": str((args.mappings / f"{spec['slug']}.json")),
                })
            else:
                try:
                    diff_result = run_differential(
                        spec, args.html, handles["state_var"], handles["calc_fn"],
                        fmap, omap, n=args.vectors,
                    )
                    if diff_result.get("ok"):
                        if diff_result["mismatch_count"]:
                            counts["diff_mismatch"] += 1
                            review.append({
                                "stage": "differential", "file": path.name,
                                "slug": spec["slug"], "title": spec["title"],
                                "severity": "critical",
                                "reason": f"{diff_result['mismatch_count']} numerical "
                                          f"mismatches vs the mockup",
                                "examples": diff_result["mismatches"][:3],
                            })
                        else:
                            counts["diff_agree"] += 1
                except Exception as exc:                          # noqa: BLE001
                    diff_result = {"ok": False, "error": str(exc)}

        (out / "specs" / f"{spec['slug']}.json").write_text(
            json.dumps(spec, indent=2, ensure_ascii=False)
        )

        records.append({
            "slug": spec["slug"], "doc_id": spec.get("doc_id"),
            "title": spec["title"], "file": path.name,
            "sha256": spec["provenance"]["source_sha256"],
            "identity": ident,
            "match": m.as_dict(),
            "renderer": spec["renderer"],
            "inputs": len(spec["inputs"]),
            "outputs": len(spec["compute"]["outputs"]),
            "refs": len(spec["info"]["references"]),
            "validation_gaps": gaps,
            "outputs_missing_expr": no_expr,
            "differential": diff_result,
        })

        badge = f"{C['ok']}✓{C['r']}" if ident["verified"] else f"{C['warn']}?{C['r']}"
        dbadge = ""
        if diff_result and diff_result.get("ok"):
            dbadge = (f" {C['ok']}diff=identical{C['r']}"
                      if not diff_result["mismatch_count"]
                      else f" {C['crit']}diff={diff_result['mismatch_count']} mismatch{C['r']}")
        elif diff_result and diff_result.get("skipped"):
            dbadge = f" {C['dim']}diff skipped{C['r']}"
        print(f"  {badge} {spec['title'][:44]:<44} "
              f"{C['dim']}id={spec.get('doc_id') or '-':<7} "
              f"match={m.method:<9}{C['r']}"
              f"{C['warn']}{f' gaps={len(gaps)}' if gaps else ''}{C['r']}{dbadge}")

    # ---- 6 EMIT --------------------------------------------------------
    stage(3, "EMIT")
    collisions = detect_collisions(matches)
    for hid, titles in collisions.items():
        review.append({"stage": "match", "severity": "critical",
                       "reason": f"{len(titles)} PDFs matched the same mockup entry "
                                 f"{hid!r}", "titles": titles})
    orphans = unmatched_html(index, matches)

    payload = {
        "summary": {
            "pdfs": len(pdfs),
            **{k: v for k, v in counts.items()},
            "collisions": len(collisions),
            "mockup_without_pdf": len(orphans),
            "review_items": len(review),
        },
        "calculators": records,
        "collisions": collisions,
        "mockup_without_pdf": orphans,
        "failures": failures,
    }
    (out / "corpus_report.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    (out / "review_queue.json").write_text(json.dumps(
        sorted(review, key=lambda r: {"critical": 0, "high": 1, "medium": 2}.get(
            r.get("severity"), 3)), indent=2, ensure_ascii=False))

    s = payload["summary"]
    print(f"\n{C['b']}══ Summary ══{C['r']}")
    print(f"  extracted            {C['ok']}{s.get('extracted',0)}{C['r']}/{s['pdfs']}"
          f"  failed {C['crit'] if s.get('extract_failed') else C['dim']}"
          f"{s.get('extract_failed',0)}{C['r']}")
    print(f"  identity verified    {s.get('extracted',0)-s.get('identity_unverified',0)}"
          f"  {C['warn']}unverified {s.get('identity_unverified',0)}{C['r']}")
    print(f"  matched              exact/alias {s.get('match_ok',0)}"
          f"  {C['warn']}fuzzy {s.get('match_fuzzy',0)}{C['r']}"
          f"  {C['dim']}none {s.get('match_none',0)}{C['r']}")
    if collisions:
        print(f"  {C['crit']}collisions           {len(collisions)}{C['r']}")
    print(f"  differential         {C['ok']}agree {s.get('diff_agree',0)}{C['r']}"
          f"  {C['crit']}mismatch {s.get('diff_mismatch',0)}{C['r']}"
          f"  {C['dim']}needs mapping {s.get('diff_needs_mapping',0)}{C['r']}")
    print(f"  validation gaps      {C['warn']}{s.get('validation_gaps',0)}{C['r']}"
          f" {C['dim']}(PDF bounds the mockup never enforces){C['r']}")
    print(f"  mockup without PDF   {s['mockup_without_pdf']}")
    print(f"\n  {C['b']}review queue: {len(review)} item(s){C['r']} → out/review_queue.json")
    for r in sorted(review, key=lambda r: {"critical":0,"high":1,"medium":2}.get(r.get("severity"),3))[:8]:
        col = C["crit"] if r["severity"] == "critical" else C["warn"]
        print(f"    {col}{r['severity']:<8}{C['r']} [{r['stage']}] "
              f"{r.get('title', r.get('reason',''))[:40]:<40} {r.get('reason','')[:44]}")
    print(f"\n{C['dim']}→ {out}/specs/  {out}/corpus_report.json  {out}/review_queue.json{C['r']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
