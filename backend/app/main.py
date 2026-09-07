"""Clinical calculator API.

Serves the 178 specs the extractor builds, and evaluates them with the same
evaluator the build validates against. Nothing here computes a clinical number
of its own: the arithmetic comes from the spec, and the spec comes from the
manufacturer's own PDF.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from . import engine, layout
from .catalog import catalog, summary
from .identity import squash
from .config import CORS_ORIGINS
from .schemas import CalculateRequest, CalculateResponse

app = FastAPI(
    title="Clinical Calculators",
    version="1.0.0",
    description=(
        "Read-only catalogue of clinical calculators extracted from their "
        "source PDFs, with server-side evaluation."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def _spec(name: str) -> dict:
    """Resolve a slug or a printed name to its spec, or 404 with suggestions."""
    entry = catalog().get(name)
    if entry is None:
        close = [e.title for e in catalog().search(q=name)[:5]]
        detail = f"no calculator {name!r}"
        if close:
            detail += f". Did you mean: {', '.join(close)}?"
        raise HTTPException(status_code=404, detail=detail)
    return entry.spec


# --------------------------------------------------------------------------
# catalogue
# --------------------------------------------------------------------------

@app.get("/api/health", tags=["meta"], summary="Liveness and build date")
def health() -> dict[str, Any]:
    c = catalog()
    return {
        "status": "ok",
        "calculators": len(c.entries),
        "built": c.generated,
    }


@app.get("/api/catalog", tags=["meta"], summary="Specialties and types, with counts")
def catalog_meta() -> dict[str, Any]:
    c = catalog()
    return {
        "count": len(c.entries),
        "built": c.generated,
        "categories": c.categories(),
        "renderers": c.renderers(),
    }


# ---------------------------------------------------------------------------
# How a calculator is addressed
#
# One identifier, three spellings, and the API accepts all of them: the slug
# ("acc-aha-2013-cardiovascular-risk-assessment"), the printed title ("ACC/AHA
# 2013 Cardiovascular Risk Assessment"), or an unambiguous fragment of either.
# The catalogue resolves them; nothing downstream cares which was used.
#
# The path parameter is `{slug:path}`, not `{slug}`, for one reason: three of
# the corpus's titles contain a "/". A browser encodes that as %2F, uvicorn
# decodes it back before routing, and a single-segment parameter then sees two
# segments and matches nothing -- so ACC/AHA, TIMI (UA/NSTEMI) and Pediatric
# Dosing: Oral Liquid/Parenteral could not be opened at all.
#
# `:path` is greedy, which imposes an ordering rule:
#
#     every fixed suffix -- /schema, /calculate, /run -- and every literal
#     route under /api/calculators -- /names -- MUST be declared ABOVE the
#     bare `{slug:path}` route, which is declared last in this file.
#
# `smoke_test.py` asserts that ordering, so breaking it fails the suite rather
# than silently turning "<name>/schema" into a slug.
# ---------------------------------------------------------------------------

@app.get("/api/calculators", tags=["calculators"], summary="Search and filter")
def list_calculators(
    q: Optional[str] = Query(None, description="Free-text search over title, fields and notes"),
    category: Optional[str] = None,
    renderer: Optional[str] = None,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    found = catalog().search(q=q, category=category, renderer=renderer)
    page = found[offset : offset + limit]
    return {
        "count": len(found),
        "limit": limit,
        "offset": offset,
        "calculators": [summary(e) for e in page],
    }


@app.get("/api/calculators/names", tags=["calculators"],
         summary="List every calculator's name")
def calculator_names() -> dict[str, Any]:
    """Every calculator's name, alphabetically.

    The smallest thing a caller can start from: pick a name here, then send it
    to any other endpoint. Declared before `/{slug}` so the literal path is not
    swallowed by the parameter.
    """
    names = catalog().names()
    return {"count": len(names), "names": names}


@app.get("/api/calculators/{slug:path}/schema", tags=["calculators"],
         summary="Fields, validation rules and the section plan")
def get_schema(slug: str) -> dict[str, Any]:
    """Everything needed to render and validate this calculator's form.

    The section plan is the important half: a client renders sections by their
    `kind`, so a calculator whose shape differs needs no client change.
    """
    return layout.schema(_spec(slug))


# --------------------------------------------------------------------------
# evaluation
# --------------------------------------------------------------------------

@app.post("/api/calculators/{slug:path}/calculate", response_model=CalculateResponse,
          tags=["calculators"], summary="Evaluate (typed response, used by the UI)")
def calculate(slug: str, body: CalculateRequest) -> CalculateResponse:
    spec = _spec(slug)
    renderer = spec.get("renderer") or "unknown"
    warnings: list[str] = []

    # -- shapes that do not take a field vector at all -------------------
    if renderer == "convert":
        if body.pair_index is None or body.value is None:
            return CalculateResponse(
                slug=slug, renderer=renderer, ok=False,
                errors=[{"field": "value", "message": "choose a conversion and enter a value"}],
            )
        try:
            result = engine.convert(spec, body.pair_index, body.value, body.reverse)
        except engine.FieldError as exc:
            return CalculateResponse(
                slug=slug, renderer=renderer, ok=False,
                errors=[{"field": exc.field, "message": exc.message}],
            )
        return CalculateResponse(slug=slug, renderer=renderer, ok=True, conversion=result)

    if renderer == "tree":
        try:
            step = engine.tree_step(spec, body.answers)
        except engine.FieldError as exc:
            return CalculateResponse(
                slug=slug, renderer=renderer, ok=False,
                errors=[{"field": exc.field, "message": exc.message}],
            )
        if step.get("reconstructed"):
            warnings.append(
                "This branch was reconstructed from the source document; confirm it "
                "against the published algorithm before relying on it."
            )
        return CalculateResponse(
            slug=slug, renderer=renderer, ok=True, tree=step, warnings=warnings
        )

    if renderer == "score":
        score, errors = engine.score_result(spec, body.selections)
        return CalculateResponse(
            slug=slug, renderer=renderer, ok=not errors, errors=errors, score=score
        )

    # -- everything else evaluates expressions over a validated vector ---
    vector, errors = engine.validate_inputs(spec, body.inputs, body.units)
    if errors:
        return CalculateResponse(slug=slug, renderer=renderer, ok=False, errors=errors)

    outputs: list[dict] = []
    lookups: list[dict] = []
    if (spec.get("compute") or {}).get("outputs"):
        env: dict = {}
        try:
            results = engine.evaluate_expressions(spec, vector, env_out=env)
        except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
            return CalculateResponse(
                slug=slug, renderer=renderer, ok=False,
                errors=[{"field": "_", "message": str(exc)}],
            )
        outputs = engine._output_rows(spec, results)
        lookups = engine.lookups_used(spec, env)
        for row in outputs:
            if not row["finite"]:
                warnings.append(
                    f"{row['label']} is not a finite number for these inputs; "
                    f"check that every value is inside its stated range."
                )

    table = engine.titration_table(spec, vector)
    drugs = engine.drug_table(spec, vector)
    thresholds = engine.threshold_result(spec, body.inputs)
    if thresholds:
        if thresholds["clamped"]:
            warnings.append(
                "The age entered is outside the printed table; the nearest "
                "printed threshold is shown."
            )
        table = table or {"kind": "thresholds", **thresholds}

    for note in (spec.get("content") or {}).get("conditional_notes") or []:
        text = note if isinstance(note, str) else note.get("text")
        if text:
            warnings.append(text)

    return CalculateResponse(
        slug=slug,
        renderer=renderer,
        ok=True,
        outputs=outputs,
        lookups=lookups,
        table=table,
        drugs=drugs,
        warnings=warnings,
    )


@app.post("/api/calculators/{name:path}/run", tags=["calculators"],
          summary="Ask what it needs, or send values and get the answer")
def run(name: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Ask for a calculator by name; get what it needs, or the answer.

    One call for the whole interaction. Send nothing and it replies with the
    values it needs, each with its units, range and options. Send those values
    -- keyed by field name OR by the label printed on the form -- and it replies
    with the results as well. `status` says which reply you got, so a caller can
    loop on the same endpoint instead of learning two.
    """
    spec = _spec(name)
    payload = dict(body or {})

    # A caller may nest the values or send them flat; both are accepted, since
    # a flat body is what anyone reaches for first.
    raw_values = payload.pop("inputs", None)
    units = payload.pop("units", {}) or {}
    selections = payload.pop("selections", {}) or {}
    answers = payload.pop("answers", []) or []
    pair_index = payload.pop("pair_index", None)
    value = payload.pop("value", None)
    reverse = bool(payload.pop("reverse", False))
    values = dict(raw_values) if isinstance(raw_values, dict) else {}
    values.update({k: v for k, v in payload.items() if not k.startswith("_")})

    resolved, unknown = _resolve_keys(spec, values)
    needs = _needs(spec)

    request = CalculateRequest(
        inputs=resolved,
        units=units,
        selections=selections,
        answers=answers,
        pair_index=pair_index,
        value=value,
        reverse=reverse,
    )
    supplied = bool(resolved or selections or answers or value is not None)
    result = calculate(spec["slug"], request) if supplied else None

    return {
        "slug": spec["slug"],
        "name": spec.get("title"),
        "category": spec.get("category"),
        "type": spec.get("renderer"),
        "status": "answered" if (result and result.ok) else "needs_values",
        # Why it is not answered yet, named per field, so a caller does not have
        # to diff `received` against `needs` to find the gap.
        "errors": [e.model_dump() for e in (result.errors if result else [])],
        "needs": needs,
        "received": resolved,
        "unknown_keys": unknown,
        "formula": (spec.get("content") or {}).get("equation"),
        "result": result.model_dump() if result else None,
        "notes": (spec.get("content") or {}).get("notes") or [],
        "disclaimer": (spec.get("content") or {}).get("data_input_disclaimer"),
    }


def _needs(spec: dict) -> dict[str, Any]:
    """What a caller has to supply, in the terms this calculator uses."""
    renderer = spec.get("renderer")
    if renderer == "score":
        return {
            "kind": "selections",
            "criteria": [
                {
                    "key": g["key"],
                    "label": g.get("label"),
                    "choose": g.get("selection"),
                    "options": [
                        {"key": o["key"], "label": o.get("label"), "points": o.get("points")}
                        for o in g.get("options") or []
                    ],
                }
                for g in ((spec.get("scoring") or {}).get("groups") or [])
            ],
        }
    if renderer == "convert":
        return {
            "kind": "conversion",
            "pairs": [
                {"index": i, "from": p.get("from"), "to": p.get("to")}
                for i, p in enumerate((spec.get("tables") or {}).get("pairs") or [])
            ],
            "send": {"pair_index": "int", "value": "number", "reverse": "bool (optional)"},
        }
    if renderer == "tree":
        return {"kind": "answers", "send": {"answers": ["yes", "no", "..."]}}
    return {
        "kind": "values",
        "fields": [
            {
                "key": f["key"],
                "label": f.get("label"),
                "required": bool(f.get("required")),
                "unit": f.get("display_unit") or f.get("base_unit"),
                "units_accepted": [u.get("code") for u in (f.get("units") or [])] or None,
                "range": {
                    "min": (f.get("constraints") or {}).get("min"),
                    "max": (f.get("constraints") or {}).get("max"),
                },
                "options": [
                    {"value": o.get("value"), "label": o.get("label")}
                    for o in (f.get("options") or [])
                ]
                or None,
                "help": f.get("help"),
            }
            for f in (spec.get("inputs") or [])
        ],
    }


def _resolve_keys(spec: dict, values: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Accept a field by its key or by the label the form prints for it.

    A caller reading the form sends "Serum creatinine"; the spec calls it
    `sr_cr`. Refusing that would make the friendly endpoint unfriendly, and
    silently dropping it would compute on a missing value -- so unmatched keys
    come back named in `unknown_keys`.
    """
    by_key = {f["key"]: f["key"] for f in (spec.get("inputs") or [])}
    by_label = {
        squash(f.get("label") or ""): f["key"]
        for f in (spec.get("inputs") or [])
        if f.get("label")
    }
    out: dict[str, Any] = {}
    unknown: list[str] = []
    for k, v in values.items():
        if k in by_key:
            out[k] = v
        elif squash(k) in by_label:
            out[by_label[squash(k)]] = v
        elif squash(k) in {squash(x) for x in by_key}:
            out[next(x for x in by_key if squash(x) == squash(k))] = v
        else:
            unknown.append(k)
    return out, unknown


# Declared last on purpose. `{slug:path}` is greedy, so this route would
# otherwise swallow "<name>/schema" and "<name>/calculate" whole.
@app.get("/api/calculators/{slug:path}", tags=["calculators"],
         summary="The full spec: formulas, tables, notes, provenance")
def get_calculator(slug: str) -> dict[str, Any]:
    """The full spec, exactly as built -- formulas, tables, notes and provenance."""
    return _spec(slug)
