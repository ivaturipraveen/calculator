"""Cross-check a PDF-extracted spec against the mockup HTML's implementation.

Two independent derivations of the same calculator exist:
  * the PDF, carrying the vendor's own JavaScript verbatim
  * the mockup, hand-built from screenshots

Where they agree, confidence is high. Where they disagree, exactly one is wrong
and the item belongs in a human review queue. This module reports both, and
additionally runs a numerical differential test: identical inputs are pushed
through the PDF-extracted expressions and through the mockup's own function, and
the outputs compared.
"""

from __future__ import annotations

import json
import re
import random
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .evaluator import ExpressionError, run_compute

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


@dataclass
class Finding:
    severity: str
    kind: str
    field: Optional[str]
    message: str
    pdf_value: Any = None
    html_value: Any = None

    def as_dict(self) -> dict:
        return {
            "severity": self.severity,
            "kind": self.kind,
            "field": self.field,
            "message": self.message,
            "pdf": self.pdf_value,
            "html": self.html_value,
        }


@dataclass
class DiffReport:
    slug: str
    title: str
    findings: list[Finding] = field(default_factory=list)
    differential: dict = field(default_factory=dict)

    def add(self, *a, **kw) -> None:
        self.findings.append(Finding(*a, **kw))

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: SEVERITY_ORDER.get(f.severity, 9))


def _num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _close(a: Optional[float], b: Optional[float], tol: float = 1e-9) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def compare_metadata(pdf_spec: dict, html_entry: dict, mapping: dict[str, str]) -> DiffReport:
    """Compare field-level metadata. `mapping` maps spec key -> mockup state prefix."""
    rep = DiffReport(slug=pdf_spec["slug"], title=pdf_spec["title"])
    defaults = (html_entry or {}).get("defaults") or {}
    consts = (html_entry or {}).get("constants") or {}

    if html_entry is None:
        rep.add("info", "coverage", None, "No matching mockup entry; PDF is the only source.")
        return rep

    if html_entry.get("category") and not pdf_spec.get("category"):
        rep.add(
            "info", "category", None,
            "Category exists only in the mockup (editorial); PDF has none.",
            None, html_entry["category"],
        )

    for inp in pdf_spec["inputs"]:
        key = inp["key"]
        prefix = mapping.get(key)
        if prefix is None:
            rep.add("high", "missing_field", key,
                    f"Field {key!r} from the PDF has no counterpart in the mockup.")
            continue

        # default value
        pdf_default = _num(inp.get("default"))
        html_default = _num(defaults.get(prefix + "Val"))
        if not _close(pdf_default, html_default):
            rep.add("high", "default", key,
                    f"Default differs: PDF {pdf_default!r} vs mockup {html_default!r}.",
                    pdf_default, html_default)

        # default/base unit
        pdf_unit = inp.get("base_unit")
        html_unit = defaults.get(prefix + "Unit")
        if pdf_unit and html_unit and pdf_unit != html_unit:
            rep.add("medium", "unit", key,
                    f"Default unit differs: PDF {pdf_unit!r} vs mockup {html_unit!r}.",
                    pdf_unit, html_unit)

        # validation bounds -- the mockup systematically drops these.
        # Checked against the implementation's SOURCE, not its evaluated
        # constants: a bound like 1 or 0.01 is indistinguishable from a unit
        # conversion factor once it is just a number in a table, which silently
        # produces false negatives.
        c = inp.get("constraints") or {}
        src = computational_source(html_entry.get("sources") or {})
        missing = [
            name for name in ("min", "max")
            if c.get(name) is not None and not _enforces_bound(src, c[name])
        ]
        if missing:
            rep.add(
                "critical", "validation", key,
                "PDF defines bounds "
                f"[{c.get('min')}, {c.get('max')}] {inp.get('base_unit') or ''}".strip()
                + f" but the mockup enforces no {'/'.join(missing)}.",
                c, None,
            )

        # unit option lists exist only in the mockup
        if inp.get("units") is None:
            lists = [k for k in consts if k.endswith("_UNITS")]
            if lists:
                rep.add("info", "unit_list", key,
                        "Unit option list is unavailable in the PDF; must come from the mockup.",
                        None, lists)
    return rep


_COMPARISON = re.compile(r"[<>]=?")
_STRINGS = re.compile(r"`(?:\\.|[^`\\])*`|'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"", re.S)


def strip_string_literals(source: str) -> str:
    """Blank out quoted and template strings.

    The mockup builds its UI with template literals full of HTML, so the `<`
    and `>` of every tag would otherwise read as comparison operators and make
    any nearby number look like an enforced bound.
    """
    return _STRINGS.sub(lambda m: " " * len(m.group(0)), source)


def computational_source(sources: dict[str, str]) -> str:
    """Only the functions that compute, not the ones that render.

    Range checks live in `calc*` / `ensure*` / `set*`; `render*` is markup.
    """
    keep = {k: v for k, v in (sources or {}).items() if not k.lower().startswith("render")}
    return strip_string_literals(" ".join(keep.values()))


def _enforces_bound(source: str, value: Any) -> bool:
    """True if the implementation actually range-checks against `value`.

    Requires the literal to appear near a comparison operator, so a number that
    merely sits in a unit-conversion table does not count as enforcement.
    """
    if value is None or not source:
        return False
    forms = {repr(float(value)), str(value)}
    if float(value) == int(float(value)):
        forms.add(str(int(float(value))))
    for form in forms:
        for m in re.finditer(r"(?<![\w.])" + re.escape(form) + r"(?![\w.])", source):
            window = source[max(0, m.start() - 60) : m.end() + 60]
            if _COMPARISON.search(window):
                return True
    return False


def _iter_numbers(obj: Any):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_numbers(v)
    elif isinstance(obj, (int, float)):
        yield obj


def random_vectors(pdf_spec: dict, n: int, seed: int = 7) -> list[dict[str, float]]:
    """Sample input vectors inside each field's declared bounds."""
    rng = random.Random(seed)
    vectors = []
    for _ in range(n):
        vec = {}
        for inp in pdf_spec["inputs"]:
            c = inp.get("constraints") or {}
            lo = c.get("min", 1.0)
            hi = c.get("max", 100.0)
            if lo is None:
                lo = 1.0
            if hi is None:
                hi = max(lo + 100.0, 100.0)
            lo, hi = float(lo), float(hi)
            if lo <= 0 < hi:
                lo = max(lo, 1e-3)      # keep divisors away from zero
            vec[inp["key"]] = round(rng.uniform(lo, hi), 4)
        vectors.append(vec)
    return vectors


def run_differential(
    pdf_spec: dict,
    html_path: Path,
    state_var: str,
    calc_fn: str,
    field_map: dict[str, str],
    output_map: dict[str, str],
    n: int = 200,
    tol: float = 1e-6,
) -> dict:
    """Push identical inputs through both implementations and compare."""
    vectors = random_vectors(pdf_spec, n)

    # PDF side -- evaluate the extracted expressions
    pdf_results, pdf_errors = [], 0
    for vec in vectors:
        try:
            pdf_results.append(run_compute(pdf_spec["compute"], vec))
        except ExpressionError as exc:
            pdf_results.append({"__error__": str(exc)})
            pdf_errors += 1

    # HTML side -- drive the mockup's own function
    js_vectors = []
    for vec in vectors:
        jv = {}
        for key, prefix in field_map.items():
            jv[prefix + "Val"] = str(vec[key])
        js_vectors.append(jv)

    job = {
        "id": pdf_spec["slug"],
        "stateVar": state_var,
        "calcFn": calc_fn,
        "resultPath": "result",
        "vectors": js_vectors,
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(job, fh)
        job_path = fh.name

    proc = subprocess.run(
        ["node", str(Path(__file__).parent / "difftest.js"), str(html_path), job_path],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip()[:2000]}

    html_out = json.loads(proc.stdout)

    mismatches, compared, html_nulls = [], 0, 0
    for i, (pres, hres) in enumerate(zip(pdf_results, html_out["results"])):
        hv = hres.get("result")
        if not hv:
            html_nulls += 1
            continue
        for pdf_key, html_key in output_map.items():
            a = _num(pres.get(pdf_key))
            b = _num(hv.get(html_key))
            if a is None or b is None:
                continue
            compared += 1
            if not _close(a, b, tol):
                mismatches.append(
                    {"vector": vectors[i], "output": pdf_key,
                     "pdf": a, "html": b, "abs_diff": abs(a - b)}
                )

    return {
        "ok": True,
        "vectors": len(vectors),
        "comparisons": compared,
        "pdf_eval_errors": pdf_errors,
        "html_null_results": html_nulls,
        "mismatches": mismatches[:10],
        "mismatch_count": len(mismatches),
        "agreement": (
            round(100.0 * (compared - len(mismatches)) / compared, 4) if compared else None
        ),
        "tolerance": tol,
    }
