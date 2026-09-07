"""Turn a spec into a section plan the client renders without knowing the slug.

The 178 calculators are not the same page with different labels. A score is a
column of criteria; a growth chart is three measurements and a percentile; a
titration table is one formula down a ladder of doses; a decision tree is one
question at a time. Hard-coding that in the client means a component per
calculator and a deploy every time a spec changes.

So the server describes the page instead: an ordered list of sections, each
with a `kind` the client has one component for, and everything that section
needs inside it. A new calculator shape is a new kind here, not 178 branches
there -- and the mobile app can consume exactly the same plan.
"""

from __future__ import annotations

from typing import Any, Optional

from .identity import squash
from .units import base_code, field_units


# Caveats about how the build went, not about the calculator. A reader has no
# use for "8 dead steps pruned"; it belongs in the build report.
_INTERNAL_CAVEATS = {
    "dead_steps_pruned",
    "reclassified_as_score",
    "inputs_from_printed_form_only",
    "base_unit_disagreement",
}


def field_schema(field: dict) -> dict[str, Any]:
    """One input, described so a form can be built and validated from it alone."""
    constraints = field.get("constraints") or {}
    options = field.get("options") or []
    widget = field.get("widget") or field.get("type")
    if not widget:
        widget = "select" if options else "number"
    units = field_units(field)

    return {
        "key": field["key"],
        "label": field.get("label") or field["key"].replace("_", " ").title(),
        "widget": "select" if options else widget,
        "required": bool(field.get("required")),
        # Which choices actually consume this field, when it is not always
        # needed. The form uses it to stop asking for a value the answer will
        # not touch.
        "required_when": field.get("required_when"),
        "default": field.get("default"),
        "placeholder": None,
        "help": field.get("help") or None,
        "dimension": field.get("dimension"),
        "constraints": {
            "min": constraints.get("min"),
            "max": constraints.get("max"),
            # A script that rejects `weight <= 0` allows anything above zero but
            # not zero itself; without this the form would offer a limit the
            # server then refuses.
            "exclusive_min": bool(constraints.get("exclusive_min")),
            "exclusive_max": bool(constraints.get("exclusive_max")),
            "step": _step_for(field),
        },
        "constraints_by_mode": field.get("constraints_by_mode"),
        "unit": {
            "base": base_code(field),
            "display": field.get("display_unit") or base_code(field),
            "options": units,
            "selectable": len(units) > 1,
        },
        "options": [
            {
                "value": o.get("value"),
                "label": o.get("label"),
                "key": o.get("key"),
                "variant_label": o.get("variant_label"),
                "variant_value": o.get("variant_value"),
            }
            for o in options
        ],
        "options_incomplete": bool(field.get("options_incomplete")),
        "depends_on": field.get("options_depend_on"),
        # The page hides this field unless another control is set a certain
        # way -- a coefficient that only applies to one sex, say.
        "visible_when": field.get("visible_when"),
        "source": (field.get("source") or {}).get("kind"),
    }


def _step_for(field: dict) -> float:
    c = field.get("constraints") or {}
    lo, hi = c.get("min"), c.get("max")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        span = abs(float(hi) - float(lo))
        if span and span <= 10:
            return 0.1
    if field.get("dimension") in ("mass", "volume", "concentration"):
        return 0.1
    return 1 if field.get("widget") == "number" else 0.1


def _grouped_fields(spec: dict) -> list[dict]:
    """Fields in the order the printed form asks for them.

    The form's own headings are carried alongside rather than used to split the
    list: the PDF gives their text but not their positions, so which fields sit
    under which heading is not recoverable. Naming them is honest; guessing a
    split would not be.
    """
    fields = [field_schema(f) for f in (spec.get("inputs") or [])]
    headings = [h.strip().rstrip(":").strip()
                for h in ((spec.get("content") or {}).get("form_sections") or [])]
    return [{"title": None, "fields": fields,
             "headings": [h for h in headings if h] or None}]


def _primary_output(spec: dict, outputs: list[dict]) -> Optional[str]:
    """Which result the calculator is named for.

    The answer gets stated once at full size, so it has to be the right one:
    Adjusted Body Weight led with "%IBW" purely because that output happened to
    be written first. Matching the title picks the result the reader came for,
    and where nothing matches, the first output is as good a guess as any.
    """
    if not outputs:
        return None
    title = squash(spec.get("title") or "")
    best, score = outputs[0]["key"], 0
    for o in outputs:
        for text in (o.get("label"), o.get("key")):
            s = squash(text or "")
            if not s:
                continue
            if s == title:
                return o["key"]
            if len(s) > 3 and (s in title or title in s) and len(s) > score:
                best, score = o["key"], len(s)
    return best


def plan(spec: dict) -> list[dict[str, Any]]:
    renderer = spec.get("renderer")
    content = spec.get("content") or {}
    tables = spec.get("tables") or {}
    scoring = spec.get("scoring") or {}
    sections: list[dict[str, Any]] = []

    if renderer == "score" and scoring.get("groups"):
        sections.append(
            {
                "id": "criteria",
                "kind": "score_groups",
                "title": "Criteria",
                "groups": scoring["groups"],
                "total": scoring.get("total"),
            }
        )
    elif renderer == "convert" and tables.get("pairs"):
        sections.append(
            {
                "id": "convert",
                "kind": "converter",
                "title": "Convert",
                "pairs": tables["pairs"],
            }
        )
    elif renderer == "tree" and tables.get("nodes"):
        sections.append(
            {
                "id": "tree",
                "kind": "tree",
                "title": "Assessment",
                "nodes": tables["nodes"],
                "outcomes": tables.get("outcomes") or {},
            }
        )
    elif spec.get("inputs"):
        for i, group in enumerate(_grouped_fields(spec)):
            sections.append(
                {
                    "id": f"inputs-{i}",
                    "kind": "fields",
                    "title": group.get("title") or "Inputs",
                    "fields": group["fields"],
                    "headings": group.get("headings"),
                }
            )

    if renderer == "score":
        sections.append(
            {
                "id": "score",
                "kind": "score_result",
                "title": (scoring.get("interpretation") or {}).get("label")
                or "Interpretation",
                "bands": scoring.get("bands") or [],
                "total": scoring.get("total"),
            }
        )
    elif (spec.get("compute") or {}).get("outputs"):
        outs = spec["compute"]["outputs"]
        lead = _primary_output(spec, outs)
        sections.append(
            {
                "id": "results",
                "kind": "results",
                "title": "Results",
                "primary": lead,
                "outputs": [
                    {
                        "key": o["key"],
                        "label": o.get("label") or o["key"],
                        "unit": o.get("base_unit"),
                        "decimals": o.get("decimals"),
                        "kind": o.get("kind") or "number",
                    }
                    for o in spec["compute"]["outputs"]
                ],
            }
        )

    if renderer == "titration_table" and tables.get("dose_ladder"):
        sections.append(
            {
                "id": "titration",
                "kind": "titration",
                "title": "Titration table",
                "truncated_in_pdf": bool(tables["dose_ladder"].get("truncated_in_pdf")),
            }
        )

    drugs = tables.get("drugs")
    if isinstance(drugs, list) and drugs and "routes" in (drugs[0] or {}):
        sections.append(
            {"id": "drugs", "kind": "drug_table", "title": "Drugs", "count": len(drugs)}
        )

    # A lookup ladder IS the calculator's data -- the LMS parameters a growth
    # chart reads, printed in the source document as a table. It arrives here
    # row by row, so it can be shown as a table with the patient's own row
    # marked, instead of as the flattened run of digits the PDF produced.
    lookups = tables.get("lookups") or []
    if lookups:
        sections.append(
            {
                "id": "lookup",
                "kind": "lookup_table",
                "title": "Reference data",
                "tables": [
                    {
                        "key": t.get("key"),
                        "columns": t.get("columns") or [],
                        "operator": t.get("operator"),
                        "row_count": len(t.get("rows") or []),
                        "select_key": t.get("select_key"),
                        "select_value": t.get("select_value"),
                        "variant_label": t.get("variant_label"),
                        "rows": t.get("rows") or [],
                    }
                    for t in lookups
                ],
            }
        )

    if tables.get("thresholds"):
        sections.append(
            {
                "id": "thresholds",
                "kind": "threshold_table",
                "title": "Treatment thresholds",
                "rows": tables["thresholds"],
            }
        )

    for i, table in enumerate(content.get("reference_tables") or []):
        sections.append({
            "id": f"reference-{i}",
            "kind": "reference_table",
            "title": table.get("title") or "Reference values",
            "columns": table.get("columns") or [],
            "column_header": table.get("column_header"),
            "rows": table.get("rows") or [],
        })

    if content.get("stated_limits"):
        sections.append({
            "id": "limits",
            "kind": "stated_limits",
            "title": "Limits the document states",
            "items": content["stated_limits"],
        })

    if content.get("fixed_values"):
        sections.append({
            "id": "fixed",
            "kind": "fixed_values",
            "title": "Fixed by this calculator",
            "items": content["fixed_values"],
        })

    if content.get("equation"):
        sections.append(
            {
                "id": "formula",
                "kind": "formula",
                "title": "Formula",
                "text": content["equation"],
            }
        )

    notes = content.get("notes")
    conditional = content.get("conditional_notes")
    if notes or conditional or content.get("instructions"):
        sections.append(
            {
                "id": "notes",
                "kind": "notes",
                "title": "Notes",
                "notes": notes or [],
                "conditional_notes": conditional or [],
                "instructions": content.get("instructions"),
            }
        )

    refs = content.get("references")
    if refs:
        sections.append(
            {"id": "references", "kind": "references", "title": "References", "items": refs}
        )

    legal = [
        ("data_input_disclaimer", content.get("data_input_disclaimer")),
        ("disclaimer", content.get("disclaimer")),
        ("copyright", content.get("copyright")),
    ]
    legal = [(k, v) for k, v in legal if v]
    if legal:
        sections.append(
            {
                "id": "legal",
                "kind": "legal",
                "title": "Disclaimer",
                "items": [{"key": k, "text": v} for k, v in legal],
            }
        )
    return sections


def schema(spec: dict) -> dict[str, Any]:
    """The whole contract a client needs to render and validate one calculator."""
    scoring = spec.get("scoring") or {}
    # What the extraction is unsure about, in the clinician's own terms. These
    # were recorded per calculator and then read by nobody: a dose ladder the
    # source PDF cut off, or an option list that holds only the printed
    # default, changes how far a result can be trusted, and the person acting
    # on the number is the one who needs to know.
    caveats = [
        {
            "kind": c.get("kind"),
            "severity": c.get("severity"),
            "detail": c.get("detail"),
            "field": c.get("field"),
        }
        for c in ((spec.get("provenance") or {}).get("conflicts") or [])
        if c.get("severity") in ("critical", "high")
        and c.get("kind") not in _INTERNAL_CAVEATS
    ]

    return {
        "slug": spec["slug"],
        "title": spec.get("title"),
        "caveats": caveats,
        "subtitle": spec.get("subtitle"),
        "category": spec.get("category"),
        "renderer": spec.get("renderer"),
        "status": spec.get("status"),
        "version": spec.get("version"),
        "display": spec.get("display") or {},
        "fields": [field_schema(f) for f in (spec.get("inputs") or [])],
        "outputs": [
            {
                "key": o["key"],
                "label": o.get("label") or o["key"],
                "unit": o.get("base_unit"),
                "decimals": o.get("decimals"),
                "kind": o.get("kind") or "number",
            }
            for o in ((spec.get("compute") or {}).get("outputs") or [])
        ],
        "scoring": {
            "groups": scoring.get("groups") or [],
            "bands": scoring.get("bands") or [],
            "total": scoring.get("total"),
            "interpretation": scoring.get("interpretation"),
        }
        if scoring
        else None,
        "sections": plan(spec),
        "help": spec.get("help") or {},
        "completeness": spec.get("completeness") or {},
        # Where these numbers come from. A clinical page that will not name its
        # source is asking to be trusted on nothing.
        "source": ((spec.get("provenance") or {}).get("pdf") or {}).get("source_file"),
    }
