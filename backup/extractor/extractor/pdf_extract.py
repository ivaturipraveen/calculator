"""Extract a canonical calculator spec from a Lexicomp/EBMcalc print-to-PDF.

The print view is highly templated, which is what makes one parser cover the
whole corpus:

    <Title>
    Include Documents / <doc id>
    Scripts            <- the calculator's real JavaScript, verbatim
    Equation           <- human-readable formula (absent for pure score calcs)
    Data Input Disclaimer
    Calculator / Input <- field labels, DEFAULT values, display units
    Results            <- output labels + units
    Decimal Precision <n>
    Additional Information / Notes / References / Disclaimer / Copyright

Authority split, since the two halves disagree in useful ways:
  * the SCRIPT is authoritative for formulas, field order and min/max bounds
  * the PRINTED FORM is authoritative for default values and display units
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Optional

import fitz

from . import dedup
from .identity import extract_identity
from .jsparse import (
    Bound,
    ParsedScript,
    js_name_to_label,
    normalize_text,
    parse_fx,
    rename_vars,
    to_snake,
)

SECTIONS = [
    "Include Documents",
    "Scripts",
    "Equation",
    "Data Input Disclaimer",
    "Calculator",
    "Results",
    "Additional Information",
    "Notes",
    "Calculation Details",
    "References",
    "Disclaimer",
    "Copyright",
]

BOILERPLATE_END = "All Rights Reserved."


# Several corpus PDFs draw their text twice (a shadow layer under the visible
# one). PyMuPDF's default extraction interleaves the two passes and silently
# DROPS characters -- "power((Height/39.37), 2)" came out as "((H i ht/39 37) 2)",
# which produced a spec that parsed but computed nonsense. Preserving whitespace
# keeps the two draws separate so each is individually intact; the resulting
# duplication is harmless because every parser takes the first complete match.
_TEXT_FLAGS = fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_PRESERVE_LIGATURES


def read_pdf_text(path: Path) -> str:
    doc = fitz.open(path)
    try:
        return "\n".join(p.get_text("text", flags=_TEXT_FLAGS) for p in doc)
    finally:
        doc.close()


def read_pdf(path: Path) -> tuple[str, dict]:
    """Text plus document metadata (whose title is usually an export default)."""
    doc = fitz.open(path)
    try:
        return ("\n".join(p.get_text("text", flags=_TEXT_FLAGS) for p in doc),
                dict(doc.metadata or {}))
    finally:
        doc.close()


def strip_boilerplate(text: str) -> str:
    i = text.find(BOILERPLATE_END)
    return text[i + len(BOILERPLATE_END) :] if i >= 0 else text


def split_sections(text: str) -> dict[str, str]:
    """Split on section headings that appear alone on a line."""
    lines = text.split("\n")
    idx: list[tuple[int, str]] = []
    for n, line in enumerate(lines):
        s = line.strip()
        if s in SECTIONS:
            idx.append((n, s))
    out: dict[str, str] = {}
    if not idx:
        return out
    out["_preamble"] = "\n".join(lines[: idx[0][0]]).strip()
    for k, (n, name) in enumerate(idx):
        end = idx[k + 1][0] if k + 1 < len(idx) else len(lines)
        body = "\n".join(lines[n + 1 : end]).strip()
        if name in out:                       # e.g. a second "Disclaimer"
            out[name] += "\n" + body
        else:
            out[name] = body

    return out


def parse_title(preamble: str) -> Optional[str]:
    for line in preamble.split("\n"):
        s = line.strip()
        if s and not s.isdigit():
            return s
    return None


def parse_equation(sec: str) -> Optional[str]:
    """Rejoin the equation block; the PDF wraps long lines mid-expression."""
    if not sec:
        return None
    raw = [l.rstrip() for l in sec.split("\n") if l.strip()]
    joined: list[str] = []
    for line in raw:
        # a continuation never starts a new `LHS =` assignment
        if joined and not re.match(r"^\s*[A-Za-z_]\w*\s*=", line):
            joined[-1] = joined[-1].rstrip() + " " + line.strip()
        else:
            joined.append(line.strip())
    return "\n".join(joined) or None


def parse_input_block(sec_calc: str, labels: list[str]) -> dict[str, dict[str, Any]]:
    """Parse the printed Input table.

    The PDF emits one token per line with no delimiters:

        Input / Age / yr / Patient Temp / 37 / degC / Elevation / 0 / meters ...

    A field is `label [default] unit`, where the default is present only when the
    form ships one. Anchoring on the labels recovered from the script removes all
    the ambiguity -- we know exactly which tokens are labels.
    """
    if not sec_calc:
        return {}
    lines = [l.strip() for l in sec_calc.split("\n") if l.strip()]
    if lines and lines[0] == "Input":
        lines = lines[1:]
    stop = {"Calculate", "Reset", "Calculate  ", "Results"}
    lines = [l for l in lines if l not in stop and l.strip() not in stop]

    label_set = {l.lower(): l for l in labels}
    anchors: list[tuple[int, str]] = []
    for i, l in enumerate(lines):
        canon = label_set.get(l.lower())
        if canon:
            anchors.append((i, canon))

    # Tokens that are never a unit: another field's label, the form's buttons,
    # or a "?" help affordance printed beside the label. Engine-B forms print no
    # units at all, so without this guard every field inherits the NEXT field's
    # label as its unit ("Dose ?", "Weight ?").
    label_tokens = {l.lower() for l in labels}
    NON_UNIT = {"calculate", "reset", "result", "results", "input", "inputs"}

    def is_unit(tok: str) -> bool:
        t = tok.strip().lower()
        if not t or "?" in t:
            return False
        if t in label_tokens or t in NON_UNIT:
            return False
        # a unit is short; a sentence is not
        return len(tok.strip()) <= 24 and " " not in tok.strip().rstrip("?").strip()[:0] or len(tok.strip()) <= 24

    out: dict[str, dict[str, Any]] = {}
    for k, (i, label) in enumerate(anchors):
        end = anchors[k + 1][0] if k + 1 < len(anchors) else len(lines)
        rest = lines[i + 1 : end]
        default, unit = None, None
        for tok in rest:
            if re.fullmatch(r"-?\d+(\.\d+)?", tok) and default is None:
                default = float(tok)
            elif unit is None and is_unit(tok):
                unit = tok.strip()
        out[label] = {"default": default, "unit": unit}
    return out


RESULT_HEADING = re.compile(r"^\s*Results?\b", re.I)


def split_calculator_results(sec_calc: str) -> tuple[str, str]:
    """Separate the input rows from the result rows inside one Calculator block.

    Only about a third of the corpus prints a standalone "Results" section. The
    rest fold the results into the Calculator table behind a bare "Result" line:

        Weight / kg / Calculate / Reset / Result / Ag Clear / mL/min

    Without this split the output's unit -- often the only place it is stated --
    is simply lost.
    """
    if not sec_calc:
        return "", ""
    lines = sec_calc.split("\n")
    for i, line in enumerate(lines):
        if RESULT_HEADING.match(line.strip()):
            return "\n".join(lines[:i]), "\n".join(lines[i + 1:])
    return sec_calc, ""


def parse_results_block(sec: str, labels: list[str]) -> dict[str, dict[str, Any]]:
    if not sec:
        return {}
    lines = [l.strip() for l in sec.split("\n") if l.strip()]
    lines = [l for l in lines if not l.startswith("Decimal Precision")]
    return parse_input_block("\n".join(lines), labels)


def parse_decimal_precision(text: str) -> Optional[int]:
    m = re.search(r"Decimal Precision\s+(\d+)", text)
    return int(m.group(1)) if m else None


def parse_bullets(sec: str) -> list[str]:
    """Rejoin wrapped prose into logical bullets.

    A new bullet starts at a capital letter / digit when the previous line looks
    terminated; otherwise the line is a wrap continuation.
    """
    if not sec:
        return []
    lines = [l.rstrip() for l in sec.split("\n")]
    items: list[str] = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if items and not _starts_new_bullet(items[-1], s):
            items[-1] = items[-1].rstrip() + " " + s
        else:
            items.append(s)
    return [re.sub(r"\s+", " ", i).strip() for i in items if i.strip()]


def _starts_new_bullet(prev: str, cur: str) -> bool:
    # A citation ends on its PubMed bracket, not a full stop -- without "]" and
    # ")" here, consecutive references are glued into a single entry.
    if not prev.endswith((".", "!", "?", ":", "]", ")")):
        return False
    if cur[:1].islower():
        return False
    # "Am J Physiol. 1946 Sep;147:199-216." style continuations
    if re.match(r"^(et al\.|p\.\s*\d|\d{4})", cur):
        return False
    return True


PUBMED = re.compile(r"\[PubMed\s+(\d+)\]")


def _split_on_pubmed(ref: str) -> list[str]:
    """One citation per PubMed bracket.

    A reference ends at its "[PubMed nnnnnnn]", but the id is often glued to
    the DOI before it with no space, so the bullet splitter reads two citations
    as one -- and only the first id survives. Splitting after each bracket
    keeps every reference and every id.
    """
    parts, last = [], 0
    for m in PUBMED.finditer(ref):
        parts.append(ref[last:m.end()].strip())
        last = m.end()
    tail = ref[last:].strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p] or [ref]


def parse_references(sec: str) -> list[dict[str, Any]]:
    out = []
    for bullet in parse_bullets(sec):
        for ref in _split_on_pubmed(bullet):
            m = PUBMED.search(ref)
            pmid = m.group(1) if m else None
            out.append(
                {
                    "text": ref,
                    "pubmed_id": pmid,
                    "url": (
                        f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else None
                    ),
                }
            )
    return out


def slugify(title: str) -> str:
    s = title.lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def extract(path: Path) -> dict[str, Any]:
    raw, meta = read_pdf(path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    text = normalize_text(strip_boilerplate(raw))
    sec = split_sections(text)

    printed_title = parse_title(sec.get("_preamble", ""))
    identity = extract_identity(
        text=text,
        filename=path.name,
        printed_title=printed_title,
        pdf_meta_title=meta.get("title"),
    )
    # Fall back through the signal hierarchy, never to a bare filename stem
    # unless nothing better exists.
    title = printed_title or identity.filename_title or path.stem
    script = parse_fx(sec.get("Scripts", ""))

    in_labels = [js_name_to_label(n) for n in script.inputs]
    out_labels = [js_name_to_label(n) for n in script.outputs]
    calc_inputs, calc_results = split_calculator_results(sec.get("Calculator", ""))
    printed_in = parse_input_block(calc_inputs, in_labels)
    # Prefer a standalone Results section; fall back to the tail of Calculator.
    printed_out = parse_results_block(sec.get("Results", ""), out_labels)
    if not any(v.get("unit") for v in printed_out.values()):
        fallback = parse_results_block(calc_results, out_labels)
        for k, v in fallback.items():
            if v.get("unit") and not (printed_out.get(k) or {}).get("unit"):
                printed_out[k] = v

    inputs = []
    for js_name in script.inputs:
        label = js_name_to_label(js_name)
        b: Bound = script.bounds.get(js_name, Bound())
        printed = printed_in.get(label, {})
        # bounds are expressed in the base unit; the printed unit is the default
        base_unit = b.base_unit or printed.get("unit")
        constraints: dict[str, Any] = {}
        if b.min is not None:
            constraints["min"] = b.min
        if b.max is not None:
            constraints["max"] = b.max
        messages = {}
        if b.min_message:
            messages["min"] = b.min_message
        if b.max_message:
            messages["max"] = b.max_message
        inputs.append(
            {
                "key": to_snake(js_name),
                "js_name": js_name,
                "label": b.label or label,
                "widget": ("select" if js_name in script.select_inputs
                           else "quantity"),
                "required": True,
                "default": printed.get("default"),
                "base_unit": base_unit,
                "display_unit": printed.get("unit"),
                "units": None,          # not recoverable from print; see html_harvest
                "constraints": constraints,
                "messages": messages or None,
            }
        )

    out_set = set(script.outputs)
    # every identifier an expression may legally reference
    varmap = {n: to_snake(n) for n in script.inputs}
    varmap.update({n: to_snake(n) for n, _ in script.steps})
    varmap.update({n: to_snake(n) for n in script.outputs})
    # Applied last so it wins: a local that merely renames an input must resolve
    # to the input, not to a step of its own -- otherwise the empty-field default
    # sitting beside the rename becomes the field's value.
    varmap.update({a: to_snake(f) for a, f in script.input_aliases.items()})
    outputs = []
    for js_name in script.outputs:
        printed = printed_out.get(js_name_to_label(js_name), {})
        expr = next((e for n, e in script.steps if n == js_name), None)
        expr = rename_vars(expr, varmap) if expr else None
        suffix = script.output_ladder.get(js_name)
        if expr and suffix:
            cols = next((t.columns for t in script.lookup_tables
                         if t.suffix == suffix), [])
            expr = rename_vars(expr, {c: c + suffix for c in cols})
        outputs.append(
            {
                "key": to_snake(js_name),
                "js_name": js_name,
                "label": js_name_to_label(js_name),
                "expr": expr,
                "base_unit": printed.get("unit"),
                "units": None,
            }
        )
    # A phantom output with no expression (e.g. RxLevelSI write-back that
    # never got a formula) must not take the whole spec down.
    if any(o.get("expr") for o in outputs):
        outputs = [o for o in outputs if o.get("expr")]

    steps = [
        {"key": to_snake(n), "js_name": n, "expr": rename_vars(e, varmap)}
        for n, e in script.steps
        if n not in out_set
    ]

    return {
        "slug": slugify(title),
        "doc_id": identity.doc_id,
        "title": title,
        "category": None,          # not present in the print view
        "subtitle": None,
        "renderer": "formula" if script.outputs else "score",
        "inputs": inputs,
        "compute": {"engine": "expr", "steps": steps, "outputs": outputs},
        "_printed_outputs": printed_out,
        "ambiguous_constants": script.ambiguous_constants or None,
        "radio_groups": script.radio_groups or None,
        "input_overrides": script.input_overrides or None,
        "unit_modes": script.unit_modes or None,
        "input_aliases": script.input_aliases or None,
        "lookup_table": (
            script.lookup_table.as_dict() if script.lookup_table is not None else None
        ),
        "lookup_tables": [
            {**t.as_dict(),
             "columns": [c + t.suffix for c in t.columns],
             "rows": [{("threshold" if k == "threshold" else k + t.suffix): v
                       for k, v in r.items()} for r in t.rows],
             "fallback": ({k + t.suffix: v for k, v in (t.fallback or {}).items()}
                          if t.fallback else None)}
            for t in script.lookup_tables
        ] or None,
        "display": {
            "decimal_precision": parse_decimal_precision(text),
            "has_precision_control": script.has_decimal_precision,
        },
        "info": {
            "equation": parse_equation(sec.get("Equation", "")),
            "data_input_disclaimer": re.sub(
                r"\s+", " ", sec.get("Data Input Disclaimer", "")
            ).strip()
            or None,
            "notes": parse_bullets(sec.get("Notes", "")) or None,
            "calc_details": parse_bullets(sec.get("Calculation Details", "")) or None,
            "references": parse_references(sec.get("References", "")),
            "disclaimer": re.sub(r"\s+", " ", sec.get("Disclaimer", "")).strip() or None,
            "copyright": re.sub(r"\s+", " ", sec.get("Copyright", "")).strip() or None,
        },
        "provenance": {
            "source_file": path.name,
            "source_sha256": sha,
            "extractor": "pdf",
            "form_name": script.form_name,
            "identity": identity.as_dict(),
        },
    }
