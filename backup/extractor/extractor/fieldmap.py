"""Derive the bridge between a PDF spec's field keys and the mockup's state keys.

The mockup names its state ad hoc (`ageVal`, `fio2Val`, `rqVal`) while the PDF
yields canonical keys (`age`, `percent_inspired_o2`, `resp_quot`). Some pairs are
lexically obvious, others are pure domain abbreviation:

    percent_inspired_o2 -> fio2      (semantic, not lexical)
    resp_quot           -> rq        (abbreviation)

So this derives what it safely can and refuses the rest. Unresolved fields are
written to a per-calculator overrides file for a human to complete; the
differential test only runs once a mapping is total, because a partial map would
silently compare the wrong columns.
"""

from __future__ import annotations

import difflib
import re
from typing import Optional

from .identity import squash

# Domain abbreviations the mockup uses. Extend as review surfaces more.
SEMANTIC_HINTS: dict[str, tuple[str, ...]] = {
    "fio2": ("percent_inspired_o2", "fraction_inspired_o2", "inspired_o2", "fio2"),
    "rq": ("resp_quot", "respiratory_quotient"),
    "temp": ("patient_temp", "temperature", "temp"),
    "elev": ("elevation", "altitude"),
    "wt": ("weight", "body_weight"),
    "ht": ("height", "length"),
    "scr": ("serum_creatinine", "creatinine"),
    "cr": ("creatinine", "serum_creatinine"),
    "alb": ("albumin",),
    "bili": ("bilirubin", "total_bilirubin"),
    "inr": ("inr",),
    "hct": ("hematocrit",),
    "hgb": ("hemoglobin",),
}

STATE_SUFFIXES = ("Val", "Value", "_val")


def state_prefixes(defaults: dict) -> list[str]:
    """Recover the field prefixes the mockup's state object uses."""
    out = []
    for key in defaults or {}:
        for suf in STATE_SUFFIXES:
            if key.endswith(suf) and len(key) > len(suf):
                out.append(key[: -len(suf)])
                break
    return out


def _candidates(pdf_key: str, prefixes: list[str]) -> list[tuple[str, float, str]]:
    pk = squash(pdf_key)
    scored: list[tuple[str, float, str]] = []
    for pre in prefixes:
        p = squash(pre)
        if not p:
            continue
        if p == pk:
            scored.append((pre, 1.0, "exact"))
            continue
        hints = SEMANTIC_HINTS.get(p.lower())
        if hints and any(squash(h) == pk for h in hints):
            scored.append((pre, 0.97, "semantic"))
            continue
        if pk.startswith(p) or p.startswith(pk):
            # require the shared stem to be substantial, so 'p' ~ 'ph' is not a match
            n = min(len(p), len(pk))
            if n >= 3:
                scored.append((pre, 0.90, "prefix"))
                continue
        if p in pk or pk in p:
            if min(len(p), len(pk)) >= 4:
                scored.append((pre, 0.85, "substring"))
                continue
        ratio = difflib.SequenceMatcher(None, pk, p).ratio()
        if ratio >= 0.80:
            scored.append((pre, ratio * 0.8, "fuzzy"))
    return sorted(scored, key=lambda x: -x[1])


def derive(
    spec: dict, defaults: dict, min_score: float = 0.85
) -> tuple[dict[str, str], list[dict]]:
    """Return (field_map, unresolved).

    A prefix is consumed once claimed, so two PDF fields cannot both map onto the
    same mockup field.
    """
    prefixes = state_prefixes(defaults)
    taken: set[str] = set()
    field_map: dict[str, str] = {}
    unresolved: list[dict] = []

    keys = [i["key"] for i in spec.get("inputs", [])]
    # Resolve highest-confidence pairings first so they claim their prefix.
    ranked: list[tuple[float, str, str, str]] = []
    for key in keys:
        for pre, score, how in _candidates(key, prefixes):
            ranked.append((score, key, pre, how))
    ranked.sort(key=lambda x: -x[0])

    for score, key, pre, how in ranked:
        if key in field_map or pre in taken or score < min_score:
            continue
        field_map[key] = pre
        taken.add(pre)

    for key in keys:
        if key not in field_map:
            cands = [
                {"prefix": p, "score": round(s, 3), "how": h}
                for p, s, h in _candidates(key, prefixes)
                if p not in taken
            ][:4]
            unresolved.append({"field": key, "candidates": cands})

    return field_map, unresolved


def derive_outputs(
    spec: dict, result_keys: list[str], min_score: float = 0.85
) -> tuple[dict[str, str], list[dict]]:
    """Map spec output keys onto the mockup's `result` object keys.

    The result object does not exist until a calculation has run, so its shape
    is discovered by probing (see `probe_result_keys`) rather than read from
    defaults. Ordering is used as a tie-break: when a calculator has exactly as
    many results as outputs and name matching is ambiguous, declaration order in
    the source JS reliably mirrors the printed Results table.
    """
    out_keys = [o["key"] for o in spec.get("compute", {}).get("outputs", [])]
    taken: set[str] = set()
    mapping: dict[str, str] = {}
    unresolved: list[dict] = []

    ranked: list[tuple[float, str, str]] = []
    for key in out_keys:
        for cand, score, _how in _candidates(key, result_keys):
            ranked.append((score, key, cand))
    ranked.sort(key=lambda x: -x[0])
    for score, key, cand in ranked:
        if key in mapping or cand in taken or score < min_score:
            continue
        mapping[key] = cand
        taken.add(cand)

    # positional fallback when counts line up exactly
    leftover_out = [k for k in out_keys if k not in mapping]
    leftover_res = [k for k in result_keys if k not in taken]
    if leftover_out and len(leftover_out) == len(leftover_res):
        for k, r in zip(leftover_out, leftover_res):
            mapping[k] = r
            taken.add(r)

    for key in out_keys:
        if key not in mapping:
            unresolved.append({
                "output": key,
                "candidates": [
                    {"key": c, "score": round(s, 3), "how": h}
                    for c, s, h in _candidates(key, result_keys) if c not in taken
                ][:4],
            })
    return mapping, unresolved


def probe_result_keys(
    html_path, state_var: str, calc_fn: str, field_map: dict[str, str], spec: dict
) -> list[str]:
    """Run the mockup's calculator once to discover its result object's shape."""
    import json as _json
    import subprocess
    import tempfile
    from pathlib import Path as _Path

    vec = {}
    for inp in spec.get("inputs", []):
        pre = field_map.get(inp["key"])
        if not pre:
            continue
        c = inp.get("constraints") or {}
        lo, hi = c.get("min"), c.get("max")
        val = inp.get("default")
        if val is None:
            lo = 1.0 if lo is None else float(lo)
            hi = 100.0 if hi is None else float(hi)
            val = max(lo, min(hi, (lo + hi) / 2)) or 1.0
        vec[pre + "Val"] = str(val)

    job = {"id": spec["slug"], "stateVar": state_var, "calcFn": calc_fn,
           "resultPath": "result", "vectors": [vec]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        _json.dump(job, fh)
        job_path = fh.name
    proc = subprocess.run(
        ["node", str(_Path(__file__).parent / "difftest.js"), str(html_path), job_path],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        return []
    try:
        res = _json.loads(proc.stdout)["results"][0].get("result")
    except (KeyError, IndexError, ValueError):
        return []
    return [k for k, v in (res or {}).items() if isinstance(v, (int, float))]


def js_handles(html_id: str) -> dict[str, str]:
    """Conventional JS names for a bespoke calculator id ('pdf2' -> calcPdf2)."""
    camel = re.sub(r"_([a-z0-9])", lambda m: m.group(1).upper(), html_id)
    pascal = camel[:1].upper() + camel[1:]
    return {
        "state_var": f"{html_id}State",
        "calc_fn": f"calc{pascal}",
        "render_fn": f"render{pascal}",
        "ensure_fn": f"ensure{pascal}State",
    }


def verify_handles(handles: dict[str, str], sources: dict) -> list[str]:
    """Which conventional handles actually exist in the harvested mockup."""
    missing = []
    for role in ("calc_fn", "ensure_fn"):
        if handles[role] not in (sources or {}):
            missing.append(f"{role}={handles[role]}")
    return missing
