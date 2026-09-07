#!/usr/bin/env python3
"""Check the published `calculators/` folder against the build.

Publishing is a copy, and a copy is a place things can go missing. This asserts
that what the API serves is the whole build and nothing but it, and that each
spec still carries the parts a calculator is made of -- the fields, the
arithmetic, and the source document's own text.

Run after every build.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
C = {"ok": "\033[92m", "bad": "\033[91m", "warn": "\033[93m",
     "dim": "\033[90m", "b": "\033[1m", "r": "\033[0m"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", type=Path, default=ROOT / "dist")
    ap.add_argument("--published", type=Path, default=ROOT.parent.parent / "calculators")
    args = ap.parse_args()

    built = {p.stem: p for p in (args.dist / "calculators").glob("*.json")}
    pub = {
        p.stem: p
        for p in args.published.glob("*.json")
        if p.name not in ("index.json", "categories.json")
    }

    problems: list[str] = []
    print(f"\n{C['b']}══ Published specs ══{C['r']}")
    print(f"  built      {len(built)}")
    print(f"  published  {len(pub)}")

    missing = sorted(set(built) - set(pub))
    extra = sorted(set(pub) - set(built))
    if missing:
        problems.append(f"{len(missing)} built but not published: {missing[:5]}")
    if extra:
        problems.append(f"{len(extra)} published but not built: {extra[:5]}")

    # byte-for-byte: a stale copy is worse than a missing one, because it looks
    # fine and answers with last week's formula
    stale = [
        slug
        for slug in sorted(set(built) & set(pub))
        if built[slug].read_bytes() != pub[slug].read_bytes()
    ]
    if stale:
        problems.append(f"{len(stale)} published copies differ from the build: {stale[:5]}")

    for name in ("index.json", "categories.json"):
        if not (args.published / name).exists():
            problems.append(f"{name} was not published")
    if not (args.published / "units" / "registry.json").exists():
        problems.append("units/registry.json was not published")

    # ---- what each spec must carry --------------------------------------
    counts = Counter()
    thin: list[str] = []
    for slug, path in sorted(pub.items()):
        spec = json.loads(path.read_text())
        compute = spec.get("compute") or {}
        tables = spec.get("tables") or {}
        scoring = spec.get("scoring") or {}
        content = spec.get("content") or {}

        has_inputs = bool(spec.get("inputs")) or bool(scoring.get("groups")) or bool(
            tables.get("pairs") or tables.get("nodes")
        )
        has_math = bool(
            compute.get("outputs")
            or scoring.get("groups")
            or tables.get("pairs")
            or tables.get("nodes")
            or tables.get("drugs")
            or tables.get("thresholds")
        )
        has_source = bool(
            content.get("equation")
            or content.get("notes")
            or content.get("references")
            or scoring.get("bands")
        )

        counts["inputs"] += has_inputs
        counts["math"] += has_math
        counts["source_text"] += has_source
        counts["formula_text"] += bool(content.get("equation"))
        counts["references"] += bool(content.get("references"))
        counts["disclaimer"] += bool(content.get("data_input_disclaimer"))
        counts["provenance"] += bool(spec.get("provenance"))

        if not (has_inputs and has_math):
            thin.append(f"{slug} (inputs={has_inputs} math={has_math})")

    total = len(pub)
    print(f"\n  {C['b']}every spec carries:{C['r']}")
    for label, key in (
        ("something to enter", "inputs"),
        ("something to compute", "math"),
        ("text from the source document", "source_text"),
        ("the printed formula", "formula_text"),
        ("references", "references"),
        ("the input disclaimer", "disclaimer"),
        ("provenance", "provenance"),
    ):
        n = counts[key]
        mark = C["ok"] if n == total else C["dim"]
        print(f"    {mark}{n:>3}/{total}{C['r']}  {label}")

    if thin:
        problems.append(f"{len(thin)} specs have no inputs or no arithmetic: {thin[:5]}")

    print()
    if problems:
        for p in problems:
            print(f"  {C['bad']}✗{C['r']} {p}")
        print()
        return 1
    print(f"  {C['ok']}published set matches the build, and every spec is complete{C['r']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
